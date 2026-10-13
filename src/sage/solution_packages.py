"""Versioned Solution Bundle package contract and local validation."""

import asyncio
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

import yaml
from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sage.control_models import (
    SolutionComponentKind,
    SolutionComponentLock,
    SolutionExecutionProfile,
    SolutionReleaseRecord,
)
from sage.control_store import LocalControlStore
from sage.digests import file_digest
from sage.evaluations import load_evaluation_suite
from sage.scanning import AllowAllContentScanner, ContentScanError, ContentScanner
from sage.supply_chain import SolutionTrustStore, SupplyChainError
from sage.triggers import TriggerDefinition
from sage.ui_schema import load_solution_ui_schema
from sage.workflows import load_workflow_template

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
MAX_SOLUTION_ZIP_BYTES = 20 * 1024 * 1024
MAX_SOLUTION_FILES = 200
MAX_SOLUTION_FILE_BYTES = 10 * 1024 * 1024
MAX_SOLUTION_EXTRACTED_BYTES = 50 * 1024 * 1024
_ALLOWED_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml"}


class SolutionPackageError(ValueError):
    """Raised when a Solution Bundle package is invalid."""


class SolutionBundleMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)


class SolutionBundleComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: SolutionComponentKind
    component_id: str = Field(alias="id", min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    content_digest: str = Field(
        alias="contentDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    path: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_path(self) -> "SolutionBundleComponent":
        if self.path is None:
            return self
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or "\\" in self.path
        ):
            raise ValueError("Solution component path must be a safe relative POSIX path")
        return self


class SolutionBundleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    owner: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    entry_agent_id: str | None = Field(default=None, alias="entryAgentId", max_length=64)
    allowed_roles: tuple[str, ...] = Field(default=("user",), alias="allowedRoles")
    supported_profiles: tuple[SolutionExecutionProfile, ...] = Field(
        default=("direct", "interactive"),
        alias="supportedProfiles",
    )
    components: tuple[SolutionBundleComponent, ...] = Field(min_length=1)


class SolutionBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["sage.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["SolutionBundle"]
    metadata: SolutionBundleMetadata
    spec: SolutionBundleSpec

    @model_validator(mode="after")
    def validate_contract(self) -> "SolutionBundleManifest":
        if not _IDENTIFIER.fullmatch(self.metadata.name):
            raise ValueError("Solution name must use lowercase letters, digits, and hyphens")
        if not _VERSION.fullmatch(self.metadata.version):
            raise ValueError("Solution version must use semantic version syntax")
        keys = [(item.kind, item.component_id) for item in self.spec.components]
        if len(keys) != len(set(keys)):
            raise ValueError("Solution components must be unique by kind and id")
        roles = [role.strip() for role in self.spec.allowed_roles if role.strip()]
        if not roles:
            raise ValueError("Solution must allow at least one role")
        if not self.spec.supported_profiles:
            raise ValueError("Solution must support at least one execution profile")
        if self.spec.entry_agent_id and (
            "agent",
            self.spec.entry_agent_id,
        ) not in keys:
            raise ValueError("Solution entry Agent must be locked as a component")
        return self

    def component_locks(self) -> tuple[SolutionComponentLock, ...]:
        return tuple(
            SolutionComponentLock(
                kind=component.kind,
                component_id=component.component_id,
                version=component.version,
                content_digest=component.content_digest,
            )
            for component in self.spec.components
        )

    def component_paths(self) -> dict[str, str]:
        return {
            f"{component.kind}:{component.component_id}": component.path
            for component in self.spec.components
            if component.path is not None
        }


def load_solution_bundle(path: Path) -> SolutionBundleManifest:
    manifest_path = path / "solution.yaml" if path.is_dir() else path
    if not manifest_path.is_file():
        raise SolutionPackageError(f"Solution manifest not found: {manifest_path}")
    try:
        return parse_solution_bundle(manifest_path.read_text(encoding="utf-8"))
    except SolutionPackageError:
        raise
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SolutionPackageError(f"Invalid Solution manifest: {exc}") from exc


def parse_solution_bundle(content: str) -> SolutionBundleManifest:
    try:
        raw = yaml.safe_load(content)
        if not isinstance(raw, dict):
            raise SolutionPackageError("Solution manifest must be an object")
        return SolutionBundleManifest.model_validate(raw)
    except SolutionPackageError:
        raise
    except (yaml.YAMLError, ValidationError) as exc:
        raise SolutionPackageError(f"Invalid Solution manifest: {exc}") from exc


def validate_solution_bundle_directory(path: Path) -> SolutionBundleManifest:
    root = path.resolve()
    manifest = load_solution_bundle(root)
    SolutionBundlePackageManager._validate_component_files(manifest, root)
    return manifest


def write_solution_bundle_zip(path: Path, destination: Path) -> Path:
    root = path.resolve()
    validate_solution_bundle_directory(root)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = file_path.relative_to(root)
                if file_path.resolve() == destination or file_path.resolve() == temporary:
                    continue
                if file_path.suffix.casefold() not in _ALLOWED_SUFFIXES:
                    raise SolutionPackageError(
                        f"Solution package file type is not allowed: {relative.as_posix()}"
                    )
                archive.write(file_path, relative.as_posix())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


class SolutionBundlePackageManager:
    def __init__(
        self,
        store: LocalControlStore,
        content_scanner: ContentScanner | None = None,
        trust_store: SolutionTrustStore | None = None,
        signature_required: bool = False,
    ) -> None:
        self._store = store
        self._content_scanner = content_scanner or AllowAllContentScanner()
        self._trust_store = trust_store or SolutionTrustStore()
        self._signature_required = signature_required

    async def import_zip(
        self,
        upload: UploadFile,
    ) -> tuple[SolutionBundleManifest, str, dict[str, str]]:
        filename = Path(upload.filename or "solution.zip").name
        if Path(filename).suffix.casefold() != ".zip":
            await upload.close()
            raise SolutionPackageError("Solution Bundle must be a ZIP archive")
        package_id = f"solpkg_{uuid4().hex}"
        upload_root = self._store.uploads_root / package_id
        package_root = self._store.artifacts_root / "solutions" / package_id
        original_path = upload_root / "original.zip"
        upload_root.mkdir(parents=True)
        package_root.mkdir(parents=True)
        size = 0
        try:
            with original_path.open("wb") as destination:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_SOLUTION_ZIP_BYTES:
                        raise SolutionPackageError("Solution ZIP exceeds the 20 MiB limit")
                    destination.write(chunk)
            try:
                await asyncio.to_thread(self._content_scanner.require_clean, original_path)
            except ContentScanError as exc:
                raise SolutionPackageError(str(exc)) from exc
            extracted_root = self._extract(original_path, package_root)
            manifest = load_solution_bundle(extracted_root)
            self._validate_component_files(manifest, extracted_root)
            try:
                verification = self._trust_store.verify(
                    extracted_root,
                    manifest,
                    signature_required=self._signature_required,
                )
            except SupplyChainError as exc:
                raise SolutionPackageError(str(exc)) from exc
        except BaseException:
            shutil.rmtree(upload_root, ignore_errors=True)
            shutil.rmtree(package_root, ignore_errors=True)
            raise
        finally:
            await upload.close()
        return (
            manifest,
            self._store.relative_path(extracted_root),
            verification.release_metadata(),
        )

    def _extract(self, archive_path: Path, package_root: Path) -> Path:
        incoming = package_root / "incoming"
        incoming.mkdir()
        extracted_bytes = 0
        normalized_names: set[str] = set()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if not files or len(files) > MAX_SOLUTION_FILES:
                    raise SolutionPackageError(
                        f"Solution ZIP must contain between 1 and {MAX_SOLUTION_FILES} files"
                    )
                paths = [self._validate_member(item) for item in files]
                common_root = self._common_wrapper(paths)
                for member, path in zip(files, paths, strict=True):
                    relative = PurePosixPath(*path.parts[1:]) if common_root else path
                    normalized = relative.as_posix().casefold()
                    if normalized in normalized_names:
                        raise SolutionPackageError(
                            f"Solution ZIP contains duplicate paths: {relative.as_posix()}"
                        )
                    normalized_names.add(normalized)
                    destination = incoming.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    file_bytes = 0
                    with archive.open(member) as source, destination.open("wb") as target:
                        while chunk := source.read(64 * 1024):
                            file_bytes += len(chunk)
                            extracted_bytes += len(chunk)
                            if file_bytes > MAX_SOLUTION_FILE_BYTES:
                                raise SolutionPackageError(
                                    f"Solution file exceeds 10 MiB: {relative.as_posix()}"
                                )
                            if extracted_bytes > MAX_SOLUTION_EXTRACTED_BYTES:
                                raise SolutionPackageError(
                                    "Solution ZIP exceeds the 50 MiB extracted limit"
                                )
                            target.write(chunk)
        except SolutionPackageError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise SolutionPackageError(f"Invalid Solution ZIP: {exc}") from exc
        if not (incoming / "solution.yaml").is_file():
            raise SolutionPackageError("Solution ZIP must contain solution.yaml at its root")
        return incoming

    @staticmethod
    def _validate_member(member: zipfile.ZipInfo) -> PurePosixPath:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or stat.S_ISLNK(mode)
        ):
            raise SolutionPackageError(
                f"Solution ZIP contains an unsafe path: {member.filename}"
            )
        if member.flag_bits & 0x1:
            raise SolutionPackageError("Encrypted Solution ZIP files are not supported")
        if Path(path.name).suffix.casefold() not in _ALLOWED_SUFFIXES:
            raise SolutionPackageError(
                f"Solution ZIP file type is not allowed: {member.filename}"
            )
        return path

    @staticmethod
    def _common_wrapper(paths: list[PurePosixPath]) -> bool:
        roots = {path.parts[0].casefold() for path in paths if path.parts}
        return len(roots) == 1 and all(len(path.parts) > 1 for path in paths)

    @staticmethod
    def _validate_component_files(
        manifest: SolutionBundleManifest,
        package_root: Path,
    ) -> None:
        locked = {
            (component.kind, component.component_id)
            for component in manifest.spec.components
        }
        for component in manifest.spec.components:
            if component.path is None:
                continue
            path = (package_root / component.path).resolve()
            if not path.is_relative_to(package_root.resolve()) or not path.is_file():
                raise SolutionPackageError(
                    f"Solution component file not found: {component.path}"
                )
            if file_digest(path) != component.content_digest:
                raise SolutionPackageError(
                    f"Solution component digest mismatch: {component.component_id}"
                )
            try:
                if component.kind == "workflow":
                    workflow = load_workflow_template(path)
                    if (
                        workflow.workflow_id != component.component_id
                        or workflow.version != component.version
                    ):
                        raise ValueError("Workflow identity does not match its component lock")
                    for step in workflow.steps:
                        if step.kind in {"skill", "agent", "workflow"} and (
                            step.kind,
                            step.target,
                        ) not in locked:
                            raise ValueError(
                                f"Workflow target is not locked: {step.kind}:{step.target}"
                            )
                elif component.kind == "trigger":
                    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                    trigger = TriggerDefinition.model_validate(raw)
                    if (
                        trigger.trigger_id != component.component_id
                        or trigger.solution_id != manifest.metadata.name
                    ):
                        raise ValueError("Trigger identity does not match its component lock")
                    if ("workflow", trigger.workflow_type) not in locked:
                        raise ValueError(
                            f"Trigger Workflow is not locked: {trigger.workflow_type}"
                        )
                elif component.kind == "ui-schema":
                    ui_schema = load_solution_ui_schema(path)
                    if ui_schema.solution_id != manifest.metadata.name:
                        raise ValueError("UI Schema Solution id does not match the Bundle")
                    for widget in ui_schema.widgets:
                        for action in widget.actions:
                            if action.kind == "invoke":
                                target = action.target.removeprefix("skill:")
                                if ("skill", target) not in locked:
                                    raise ValueError(
                                        f"UI action Skill is not locked: {target}"
                                    )
                            elif action.kind == "start-workflow" and (
                                "workflow",
                                action.target,
                            ) not in locked:
                                raise ValueError(
                                    f"UI action Workflow is not locked: {action.target}"
                                )
                elif component.kind == "evaluation-suite":
                    suite = load_evaluation_suite(path)
                    if (
                        suite.suite_id != component.component_id
                        or suite.version != component.version
                    ):
                        raise ValueError(
                            "Evaluation Suite identity does not match its component lock"
                        )
            except (OSError, ValueError) as exc:
                raise SolutionPackageError(
                    f"Invalid {component.kind} component {component.component_id}: {exc}"
                ) from exc


def validate_release_assets(
    store: LocalControlStore,
    release: SolutionReleaseRecord,
) -> None:
    if not release.component_paths:
        return
    if release.package_relpath is None:
        raise SolutionPackageError("Solution Release has component paths without a package")
    package_root = store.resolve_path(release.package_relpath)
    for key, relative_path in release.component_paths.items():
        kind, separator, component_id = key.partition(":")
        if not separator:
            raise SolutionPackageError(f"Invalid component path key: {key}")
        component = next(
            (
                item
                for item in release.components
                if item.kind == kind and item.component_id == component_id
            ),
            None,
        )
        if component is None:
            raise SolutionPackageError(f"Component path is not locked: {key}")
        path = (package_root / relative_path).resolve()
        if not path.is_relative_to(package_root.resolve()) or not path.is_file():
            raise SolutionPackageError(f"Solution component file not found: {relative_path}")
        if file_digest(path) != component.content_digest:
            raise SolutionPackageError(
                f"Solution component digest mismatch: {component.component_id}"
            )