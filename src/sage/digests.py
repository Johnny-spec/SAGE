"""Canonical content digests used by control-plane records."""

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def generation_digest(
    skill_digests: dict[str, str],
    agent_definitions: dict[str, Any],
    default_agent_id: str,
    solution_releases: dict[str, str] | None = None,
    tool_digests: dict[str, str] | None = None,
) -> str:
    return canonical_digest(
        {
            "skills": skill_digests,
            "agents": agent_definitions,
            "default_agent_id": default_agent_id,
            "solution_releases": solution_releases or {},
            **({"tools": tool_digests} if tool_digests else {}),
        }
    )


def solution_release_digest(
    *,
    solution_id: str,
    version: str,
    entry_agent_id: str | None,
    allowed_roles: tuple[str, ...],
    supported_profiles: tuple[str, ...],
    components: list[dict[str, Any]],
    component_paths: dict[str, str] | None = None,
    package_metadata: dict[str, str] | None = None,
) -> str:
    return canonical_digest(
        {
            "solution_id": solution_id,
            "version": version,
            "entry_agent_id": entry_agent_id,
            "allowed_roles": allowed_roles,
            "supported_profiles": supported_profiles,
            "components": components,
            "component_paths": component_paths or {},
            "package_metadata": package_metadata or {},
        }
    )
