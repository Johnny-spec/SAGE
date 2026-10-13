"""Solution definitions and immutable component release locks."""

import re
from datetime import UTC, datetime

from sage.control_models import (
    SolutionComponentLock,
    SolutionDefinitionRecord,
    SolutionExecutionProfile,
    SolutionReleaseRecord,
)
from sage.control_store import LocalControlStore
from sage.digests import solution_release_digest

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class SolutionError(ValueError):
    """Raised when a Solution or Release violates the catalog contract."""


class SolutionManager:
    def __init__(self, store: LocalControlStore) -> None:
        self._store = store

    def create(
        self,
        *,
        solution_id: str,
        title: str,
        description: str,
        owner: str,
        entry_agent_id: str | None,
        allowed_roles: tuple[str, ...] = ("user",),
        supported_profiles: tuple[SolutionExecutionProfile, ...],
    ) -> SolutionDefinitionRecord:
        normalized_id = solution_id.strip()
        if not _IDENTIFIER.fullmatch(normalized_id):
            raise SolutionError(
                "Solution id must use lowercase letters, digits, and hyphens."
            )
        if not title.strip():
            raise SolutionError("Solution title is required")
        if not owner.strip():
            raise SolutionError("Solution owner is required")
        roles = _unique_roles(allowed_roles)
        profiles = _unique_profiles(supported_profiles)
        solution = SolutionDefinitionRecord(
            solution_id=normalized_id,
            title=title.strip(),
            description=description.strip(),
            owner=owner.strip(),
            entry_agent_id=entry_agent_id.strip() if entry_agent_id else None,
            allowed_roles=roles,
            supported_profiles=profiles,
        )

        def save(state: object) -> None:
            if solution.solution_id in state.solutions:  # type: ignore[attr-defined]
                raise SolutionError(f"Solution already exists: {solution.solution_id}")
            state.solutions[solution.solution_id] = solution  # type: ignore[attr-defined]

        self._store.update(save)
        return solution

    def create_release(
        self,
        solution_id: str,
        *,
        version: str,
        components: tuple[SolutionComponentLock, ...],
        entry_agent_id: str | None = None,
        allowed_roles: tuple[str, ...] | None = None,
        supported_profiles: tuple[SolutionExecutionProfile, ...] | None = None,
        source_generation_id: str | None = None,
        component_paths: dict[str, str] | None = None,
        package_relpath: str | None = None,
        package_metadata: dict[str, str] | None = None,
    ) -> SolutionReleaseRecord:
        state = self._store.snapshot()
        solution = state.solutions.get(solution_id)
        if solution is None:
            raise KeyError(solution_id)
        normalized_version = version.strip()
        if not _VERSION.fullmatch(normalized_version):
            raise SolutionError("Solution Release version must use semantic version syntax.")
        if not components:
            raise SolutionError("Solution Release must lock at least one component")
        ordered_components = tuple(
            sorted(components, key=lambda item: (item.kind, item.component_id))
        )
        component_keys = [(item.kind, item.component_id) for item in ordered_components]
        if len(component_keys) != len(set(component_keys)):
            raise SolutionError("Solution Release contains duplicate component locks")
        resolved_entry_agent_id = entry_agent_id or solution.entry_agent_id
        if resolved_entry_agent_id and (
            "agent",
            resolved_entry_agent_id,
        ) not in component_keys:
            raise SolutionError("Solution entry Agent must be present in the component lock")
        profiles = _unique_profiles(supported_profiles or solution.supported_profiles)
        roles = _unique_roles(allowed_roles or solution.allowed_roles)
        paths = dict(sorted((component_paths or {}).items()))
        metadata = dict(sorted((package_metadata or {}).items()))
        component_key_strings = {f"{kind}:{component_id}" for kind, component_id in component_keys}
        unknown_paths = set(paths) - component_key_strings
        if unknown_paths:
            raise SolutionError(
                f"Component paths reference unlocked components: {', '.join(sorted(unknown_paths))}"
            )
        if source_generation_id and source_generation_id not in state.generations:
            raise SolutionError(f"Source Generation not found: {source_generation_id}")
        release_id = f"{solution_id}@{normalized_version}"
        if release_id in state.solution_releases:
            raise SolutionError(f"Solution Release already exists: {release_id}")
        digest = solution_release_digest(
            solution_id=solution_id,
            version=normalized_version,
            entry_agent_id=resolved_entry_agent_id,
            allowed_roles=roles,
            supported_profiles=profiles,
            components=[component.model_dump(mode="json") for component in ordered_components],
            component_paths=paths,
            package_metadata=metadata,
        )
        release = SolutionReleaseRecord(
            release_id=release_id,
            solution_id=solution_id,
            version=normalized_version,
            content_digest=digest,
            components=ordered_components,
            component_paths=paths,
            package_relpath=package_relpath,
            package_metadata=metadata,
            entry_agent_id=resolved_entry_agent_id,
            allowed_roles=roles,
            supported_profiles=profiles,
            source_generation_id=source_generation_id,
        )

        def save(current: object) -> None:
            if release.release_id in current.solution_releases:  # type: ignore[attr-defined]
                raise SolutionError(f"Solution Release already exists: {release.release_id}")
            current.solution_releases[release.release_id] = release  # type: ignore[attr-defined]
            item = current.solutions[solution_id]  # type: ignore[attr-defined]
            item.updated_at = datetime.now(UTC)

        self._store.update(save)
        return release

    def get(self, solution_id: str) -> SolutionDefinitionRecord:
        solution = self._store.snapshot().solutions.get(solution_id)
        if solution is None:
            raise KeyError(solution_id)
        return solution

    def list_solutions(self) -> list[SolutionDefinitionRecord]:
        return sorted(
            self._store.snapshot().solutions.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def list_releases(self, solution_id: str) -> list[SolutionReleaseRecord]:
        state = self._store.snapshot()
        if solution_id not in state.solutions:
            raise KeyError(solution_id)
        return sorted(
            (
                release
                for release in state.solution_releases.values()
                if release.solution_id == solution_id
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def list_all_releases(self) -> list[SolutionReleaseRecord]:
        return sorted(
            self._store.snapshot().solution_releases.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )


def _unique_profiles(
    profiles: tuple[SolutionExecutionProfile, ...],
) -> tuple[SolutionExecutionProfile, ...]:
    if not profiles:
        raise SolutionError("Solution must support at least one execution profile")
    return tuple(dict.fromkeys(profiles))


def _unique_roles(roles: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(role.strip() for role in roles if role.strip()))
    if not normalized:
        raise SolutionError("Solution must allow at least one role")
    return normalized