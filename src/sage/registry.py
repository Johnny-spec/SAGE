"""Read-only discovery of trusted Skill packages."""

import re
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import ValidationError

from sage.models import SkillDefinition, SkillManifest
from sage.workflows import load_workflow_template

_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class SkillPackageError(ValueError):
    """Raised when a Skill package cannot be trusted or normalized."""


def _parse_skill_frontmatter(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        raise SkillPackageError(f"{path}: missing YAML frontmatter")
    try:
        closing_index = next(
            index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillPackageError(f"{path}: unterminated YAML frontmatter") from exc
    data = yaml.safe_load("\n".join(lines[1:closing_index]))
    if not isinstance(data, dict):
        raise SkillPackageError(f"{path}: frontmatter must be an object")
    return data


def _discover_packages(root: Path) -> Iterator[Path]:
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_symlink():
            raise SkillPackageError(f"{child}: symbolic-link packages are not allowed")
        if child.is_dir() and (child / "SKILL.md").is_file():
            yield child


class SkillRegistry:
    """An immutable name-to-SkillDefinition mapping."""

    def __init__(self, definitions: dict[str, SkillDefinition]) -> None:
        self._definitions = MappingProxyType(dict(definitions))

    @classmethod
    def from_definitions(cls, definitions: Iterator[SkillDefinition]) -> "SkillRegistry":
        by_name: dict[str, SkillDefinition] = {}
        for definition in definitions:
            if definition.name in by_name:
                raise SkillPackageError(f"duplicate Skill name: {definition.name}")
            by_name[definition.name] = definition
        return cls(by_name)

    @classmethod
    def load(cls, root: Path) -> "SkillRegistry":
        root = root.resolve(strict=True)
        definitions: dict[str, SkillDefinition] = {}
        for package_root in _discover_packages(root):
            definition = cls._load_package(package_root)
            if definition.name in definitions:
                raise SkillPackageError(f"duplicate Skill name: {definition.name}")
            definitions[definition.name] = definition
        return cls(definitions)

    @staticmethod
    def _load_package(package_root: Path) -> SkillDefinition:
        skill_path = package_root / "SKILL.md"
        manifest_path = package_root / "skill.yaml"
        if not manifest_path.is_file():
            raise SkillPackageError(f"{package_root}: missing skill.yaml")

        frontmatter = _parse_skill_frontmatter(skill_path)
        name = frontmatter.get("name") or frontmatter.get("id")
        description = frontmatter.get("description")
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
            raise SkillPackageError(f"{skill_path}: invalid Skill name")
        if name != package_root.name:
            raise SkillPackageError(f"{skill_path}: name must match parent directory")
        if not isinstance(description, str) or not description.strip():
            raise SkillPackageError(f"{skill_path}: description is required")

        try:
            raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = SkillManifest.model_validate(raw_manifest)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise SkillPackageError(f"{manifest_path}: invalid manifest: {exc}") from exc
        if manifest.metadata.name != name:
            raise SkillPackageError(f"{manifest_path}: metadata.name must match SKILL.md")

        execution = manifest.spec.execution
        if f"skill:{name}" in execution.capability_ids:
            raise SkillPackageError("Orchestrated Skills cannot invoke themselves")
        if execution.type == "workflow":
            workflow_path = (package_root / (execution.workflow or "")).resolve()
            if (
                not workflow_path.is_relative_to(package_root.resolve())
                or not workflow_path.is_file()
            ):
                raise SkillPackageError("Workflow file must exist inside its Skill package")
            try:
                workflow = load_workflow_template(workflow_path)
            except ValueError as exc:
                raise SkillPackageError(str(exc)) from exc
            for step in workflow.steps:
                if step.kind not in {"skill", "tool"} or step.failure_policy == "escalate":
                    raise SkillPackageError(
                        "Skill workflows support only Skill/Tool execution nodes"
                    )
                target = f"skill:{step.target}" if step.kind == "skill" else step.target
                if target not in execution.capability_ids:
                    raise SkillPackageError(f"Workflow target is not declared: {target}")

        return SkillDefinition(
            name=name,
            description=description.strip(),
            version=manifest.metadata.version,
            package_root=package_root,
            manifest=manifest,
        )

    @classmethod
    def load_definition(cls, package_root: Path) -> SkillDefinition:
        return cls._load_package(package_root.resolve(strict=True))

    def get(self, name: str) -> SkillDefinition | None:
        return self._definitions.get(name)

    def __iter__(self) -> Iterator[SkillDefinition]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)
