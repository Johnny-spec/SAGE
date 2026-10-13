"""Quarantined Skill ZIP import, validation, and artifact packaging."""

import asyncio
import re
import shutil
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml
from fastapi import UploadFile

from sage.control_models import (
    SkillPackageRecord,
    ValidationIssue,
    ValidationReport,
)
from sage.control_store import LocalControlStore
from sage.digests import directory_digest
from sage.registry import SkillPackageError, SkillRegistry
from sage.scanning import AllowAllContentScanner, ContentScanError, ContentScanner

MAX_SKILL_ZIP_BYTES = 50 * 1024 * 1024
MAX_SKILL_FILES = 200
MAX_SKILL_FILE_BYTES = 20 * 1024 * 1024
MAX_SKILL_EXTRACTED_BYTES = 100 * 1024 * 1024

_FORBIDDEN_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".msi",
    ".pyd",
    ".ps1",
    ".so",
    ".whl",
}
_DEPENDENCY_FILES = {
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
}
_WINDOWS_DEVICE_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class SkillImportError(ValueError):
    """Raised when an uploaded Skill archive violates quarantine policy."""


class SkillPackageManager:
    def __init__(
        self,
        store: LocalControlStore,
        content_scanner: ContentScanner | None = None,
        *,
        python_runner_enabled: bool = False,
    ) -> None:
        self._store = store
        self._content_scanner = content_scanner or AllowAllContentScanner()
        self._python_runner_enabled = python_runner_enabled

    async def upload(self, upload: UploadFile) -> SkillPackageRecord:
        filename = Path(upload.filename or "skill.zip").name
        if Path(filename).suffix.casefold() != ".zip":
            await upload.close()
            raise SkillImportError("Skill package must be a ZIP archive")
        package_id = f"pkg_{uuid4().hex}"
        upload_root = self._store.uploads_root / package_id
        artifact_root = self._store.artifacts_root / "skills" / package_id
        original_path = upload_root / "original.zip"
        upload_root.mkdir(parents=True)
        artifact_root.mkdir(parents=True)
        size = 0
        try:
            with original_path.open("wb") as destination:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_SKILL_ZIP_BYTES:
                        raise SkillImportError("Skill ZIP exceeds the 50 MiB limit")
                    destination.write(chunk)
            try:
                await asyncio.to_thread(self._content_scanner.require_clean, original_path)
            except ContentScanError as exc:
                raise SkillImportError(str(exc)) from exc
            package_root = self._extract(original_path, artifact_root)
            definition = SkillRegistry.load_definition(package_root)
            digest = directory_digest(package_root)
            normalized_path = upload_root / "normalized.zip"
            self._write_normalized_zip(package_root, normalized_path)
        except BaseException:
            shutil.rmtree(upload_root, ignore_errors=True)
            shutil.rmtree(artifact_root, ignore_errors=True)
            raise
        finally:
            await upload.close()

        record = SkillPackageRecord(
            package_id=package_id,
            source="zip",
            original_filename=filename,
            original_relpath=self._store.relative_path(original_path),
            package_relpath=self._store.relative_path(package_root),
            name=definition.name,
            version=definition.version,
            content_digest=digest,
            execution_type=definition.manifest.spec.execution.type,
        )

        def save(state: object) -> None:
            state.skills[package_id] = record  # type: ignore[attr-defined]

        self._store.update(save)
        return record

    def register_generated_mcp(
        self,
        *,
        server_id: str,
        server_alias: str,
        server_config_digest: str,
        tool_name: str,
        schema_digest: str,
        skill_name: str,
        version: str,
        description: str,
        instructions: str,
        risk: str,
        input_schema: dict[str, object],
        output_schema: dict[str, object],
    ) -> SkillPackageRecord:
        package_id = f"pkg_{uuid4().hex}"
        package_root = self._store.artifacts_root / "skills" / package_id / skill_name
        package_root.mkdir(parents=True)
        frontmatter = yaml.safe_dump(
            {"name": skill_name, "description": description},
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        (package_root / "SKILL.md").write_text(
            f"---\n{frontmatter}\n---\n\n{instructions.strip()}\n",
            encoding="utf-8",
        )
        manifest = {
            "apiVersion": "sage.dev/v1alpha1",
            "kind": "SkillBinding",
            "metadata": {"name": skill_name, "version": version},
            "spec": {
                "execution": {
                    "type": "mcp",
                    "serverAlias": server_alias,
                    "toolName": tool_name,
                    "schemaDigest": schema_digest,
                    "serverConfigDigest": server_config_digest,
                },
                "inputSchema": input_schema,
                "outputSchema": output_schema,
                "routing": {"keywords": [], "attachmentTypes": []},
                "policy": {
                    "risk": risk,
                    "timeoutSeconds": 60,
                    "maxOutputBytes": 262_144,
                },
            },
        }
        (package_root / "skill.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        upload_root = self._store.uploads_root / package_id
        upload_root.mkdir(parents=True)
        normalized_path = upload_root / "normalized.zip"
        self._write_normalized_zip(package_root, normalized_path)
        record = SkillPackageRecord(
            package_id=package_id,
            source="mcp",
            source_ref=server_id,
            original_filename=f"{skill_name}-{version}.zip",
            package_relpath=self._store.relative_path(package_root),
            name=skill_name,
            version=version,
            content_digest=directory_digest(package_root),
            execution_type="mcp",
        )

        def save(state: object) -> None:
            state.skills[package_id] = record  # type: ignore[attr-defined]

        self._store.update(save)
        return self.validate(package_id)

    def validate(self, package_id: str) -> SkillPackageRecord:
        state = self._store.snapshot()
        record = state.skills.get(package_id)
        if record is None:
            raise KeyError(package_id)
        package_root = self._store.resolve_path(record.package_relpath)
        issues: list[ValidationIssue] = []
        try:
            definition = SkillRegistry.load_definition(package_root)
            binding = definition.manifest.spec.execution
            if binding.type == "python-subprocess":
                self._validate_python_entrypoint(package_root, binding.entrypoint)
                issues.append(
                    ValidationIssue(
                        severity="info" if self._python_runner_enabled else "warning",
                        code=(
                            "isolated_runner_enabled"
                            if self._python_runner_enabled
                            else "uploaded_code_execution_disabled"
                        ),
                        message=(
                            "Python code will execute through the configured isolated Runner."
                            if self._python_runner_enabled
                            else "Python subprocess code is valid but will not be exposed to the "
                            "runtime until an isolated Runner is configured."
                        ),
                    )
                )
            elif binding.type in {"builtin", "prompt"}:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="execution_binding_disabled",
                        message=f"Uploaded {binding.type} execution is not enabled.",
                    )
                )
            status = "valid"
        except (OSError, SkillPackageError, SkillImportError, ValueError) as exc:
            definition = None
            status = "invalid"
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid_skill_package",
                    message=str(exc),
                )
            )
        report = ValidationReport(
            status=status,
            checked_at=datetime.now(UTC),
            content_digest=directory_digest(package_root) if package_root.is_dir() else None,
            issues=tuple(issues),
        )

        def apply(current: object) -> SkillPackageRecord:
            item = current.skills[package_id]  # type: ignore[attr-defined]
            item.status = status
            item.validation = report
            item.updated_at = datetime.now(UTC)
            if definition is not None:
                item.name = definition.name
                item.version = definition.version
                item.execution_type = definition.manifest.spec.execution.type
                item.executable = (
                    item.source == "mcp" and definition.manifest.spec.execution.type == "mcp"
                ) or (
                    self._python_runner_enabled
                    and definition.manifest.spec.execution.type == "python-subprocess"
                )
            return item.model_copy(deep=True)

        return self._store.update(apply)

    def list_packages(self) -> list[SkillPackageRecord]:
        return sorted(
            self._store.snapshot().skills.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def get(self, package_id: str) -> SkillPackageRecord:
        record = self._store.snapshot().skills.get(package_id)
        if record is None:
            raise KeyError(package_id)
        return record

    def download_path(self, package_id: str, kind: str) -> Path:
        record = self.get(package_id)
        if kind == "original" and record.original_relpath:
            return self._store.resolve_path(record.original_relpath)
        if kind == "normalized":
            path = self._store.uploads_root / package_id / "normalized.zip"
            if path.is_file():
                return path
        raise KeyError(f"{package_id}:{kind}")

    def _extract(self, archive_path: Path, artifact_root: Path) -> Path:
        incoming_root = artifact_root / "incoming"
        incoming_root.mkdir()
        extracted_bytes = 0
        normalized_names: set[str] = set()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if not files or len(files) > MAX_SKILL_FILES:
                    raise SkillImportError(
                        f"Skill ZIP must contain between 1 and {MAX_SKILL_FILES} files"
                    )
                paths = [self._validate_member(item) for item in files]
                common_root = self._common_wrapper(paths)
                for member, path in zip(files, paths, strict=True):
                    relative = PurePosixPath(*path.parts[1:]) if common_root else path
                    if not relative.parts:
                        raise SkillImportError("Skill ZIP contains an empty package path")
                    normalized_key = relative.as_posix().casefold()
                    if normalized_key in normalized_names:
                        raise SkillImportError(
                            f"Skill ZIP contains duplicate paths: {relative.as_posix()}"
                        )
                    normalized_names.add(normalized_key)
                    destination = incoming_root.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as target:
                        file_bytes = 0
                        while chunk := source.read(64 * 1024):
                            file_bytes += len(chunk)
                            extracted_bytes += len(chunk)
                            if file_bytes > MAX_SKILL_FILE_BYTES:
                                raise SkillImportError(
                                    f"Skill file exceeds the 20 MiB limit: {member.filename}"
                                )
                            if extracted_bytes > MAX_SKILL_EXTRACTED_BYTES:
                                raise SkillImportError(
                                    "Skill ZIP exceeds the 100 MiB extracted limit"
                                )
                            target.write(chunk)
        except SkillImportError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise SkillImportError(f"Invalid Skill ZIP: {exc}") from exc

        manifest_path = incoming_root / "skill.yaml"
        instructions_path = incoming_root / "SKILL.md"
        if not manifest_path.is_file() or not instructions_path.is_file():
            raise SkillImportError(
                "Skill ZIP must contain one package root with SKILL.md and skill.yaml"
            )
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            name = manifest["metadata"]["name"]
        except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
            raise SkillImportError("skill.yaml does not contain a valid metadata.name") from exc
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
            raise SkillImportError("skill.yaml contains an invalid metadata.name")
        package_root = artifact_root / name
        incoming_root.replace(package_root)
        return package_root

    @staticmethod
    def _validate_member(member: zipfile.ZipInfo) -> PurePosixPath:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or stat.S_ISLNK(mode)
            or any(
                PurePosixPath(part).stem.casefold() in _WINDOWS_DEVICE_NAMES for part in path.parts
            )
        ):
            raise SkillImportError(f"Skill ZIP contains an unsafe path: {member.filename}")
        if member.flag_bits & 0x1:
            raise SkillImportError("Encrypted Skill ZIPs are not supported")
        if path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise SkillImportError(f"Skill ZIP contains a forbidden file: {member.filename}")
        if path.name.casefold() in _DEPENDENCY_FILES:
            raise SkillImportError(f"Dependency installation is not supported: {member.filename}")
        return path

    @staticmethod
    def _common_wrapper(paths: list[PurePosixPath]) -> bool:
        roots = {path.parts[0] for path in paths}
        return len(roots) == 1 and all(len(path.parts) > 1 for path in paths)

    @staticmethod
    def _validate_python_entrypoint(package_root: Path, entrypoint: str | None) -> None:
        if not entrypoint:
            raise SkillImportError("Python Skill is missing an entrypoint")
        resolved = (package_root / entrypoint).resolve()
        if not resolved.is_relative_to(package_root.resolve()) or not resolved.is_file():
            raise SkillImportError("Python Skill entrypoint is invalid")
        if resolved.suffix.casefold() != ".py":
            raise SkillImportError("Python Skill entrypoint must be a .py file")

    @staticmethod
    def _write_normalized_zip(package_root: Path, destination: Path) -> None:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
                archive.write(
                    path, f"{package_root.name}/{path.relative_to(package_root).as_posix()}"
                )
