"""Atomic local persistence for the pre-database control plane."""

import json
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import TypeVar

from sage.control_models import ControlPlaneState, GenerationRecord, ValidationReport
from sage.digests import (
    canonical_digest,
    directory_digest,
    generation_digest,
    solution_release_digest,
)
from sage.registry import SkillRegistry

T = TypeVar("T")
_CONTENT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class LocalControlStore:
    def __init__(
        self,
        root: Path,
        initial_generation: GenerationRecord | None = None,
    ) -> None:
        self.root = root.resolve()
        self.artifacts_root = self.root / "artifacts"
        self.uploads_root = self.root / "uploads"
        self.generations_root = self.root / "generations"
        self._state_path = self.root / "control-plane.json"
        self._lock = RLock()
        self._in_transaction = False
        for path in (self.root, self.artifacts_root, self.uploads_root, self.generations_root):
            path.mkdir(parents=True, exist_ok=True)
        if self._state_path.is_file():
            raw_state = json.loads(self._state_path.read_text(encoding="utf-8"))
            migrated = self._migrate(raw_state)
            self._state = ControlPlaneState.model_validate(migrated)
            if migrated is not raw_state:
                self._persist(self._state)
        else:
            if initial_generation is None:
                raise ValueError("initial_generation is required for a new control store")
            self._state = ControlPlaneState(
                active_generation_id=initial_generation.generation_id,
                generations={initial_generation.generation_id: initial_generation},
            )
            self._persist(self._state)

    def snapshot(self) -> ControlPlaneState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def read(self, reader: Callable[[ControlPlaneState], T]) -> T:
        with self._lock:
            return reader(self._state.model_copy(deep=True))

    def update(self, mutation: Callable[[ControlPlaneState], T]) -> T:
        with self._lock:
            candidate = self._state.model_copy(deep=True)
            result = mutation(candidate)
            if not self._in_transaction:
                self._persist(candidate)
            self._state = candidate
            return result

    @contextmanager
    def transaction(self):
        """Commit multiple manager mutations atomically in this single-process store."""
        with self._lock:
            if self._in_transaction:
                raise RuntimeError("Nested control transactions are unsupported")
            original = self._state
            self._state = original.model_copy(deep=True)
            self._in_transaction = True
            try:
                yield
                self._persist(self._state)
            except BaseException:
                self._state = original
                raise
            finally:
                self._in_transaction = False

    def resolve_path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("control-plane artifact path escapes the data root")
        return candidate

    def relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("control-plane artifact is outside the data root")
        return resolved.relative_to(self.root).as_posix()

    def _persist(self, state: ControlPlaneState) -> None:
        temporary_path = self._state_path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8") as stream:
            stream.write(state.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, self._state_path)

    @staticmethod
    def _migrate(raw_state: dict[str, object]) -> dict[str, object]:
        migrated = LocalControlStore._migrate_legacy(raw_state)
        if migrated.get("schema_version") == 12:
            return {**migrated, "schema_version": 13, "workflow_drafts": {}}
        return migrated

    @staticmethod
    def _migrate_legacy(raw_state: dict[str, object]) -> dict[str, object]:
        version = raw_state.get("schema_version")
        if version in {10, 11}:
            return {
                **raw_state,
                "schema_version": 12,
                "tool_registrations": raw_state.get("tool_registrations", {}),
                "mcp_test_reviews": raw_state.get("mcp_test_reviews", {}),
                "mcp_tests": raw_state.get("mcp_tests", {}),
            }
        if version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
            return raw_state
        migrated = dict(raw_state)
        if version in {1, 2, 3, 4}:
            generations = dict(migrated.get("generations") or {})
            repaired: dict[str, dict[str, object]] = {}

            def repair_generation(generation_id: str) -> dict[str, object]:
                if generation_id in repaired:
                    return repaired[generation_id]
                raw_generation = generations[generation_id]
                generation = dict(raw_generation)
                parent_id = generation.get("parent_generation_id")
                parent = (
                    repair_generation(str(parent_id))
                    if parent_id and str(parent_id) in generations
                    else None
                )
                skills_root = Path(str(generation["skills_root"]))
                registry = SkillRegistry.load(skills_root)
                existing_digests = dict(generation.get("skill_digests") or {})
                parent_versions = dict(parent.get("skill_versions") or {}) if parent else {}
                parent_digests = dict(parent.get("skill_digests") or {}) if parent else {}
                empty_digest = (
                    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                )
                skill_digests: dict[str, str] = {}
                for item in registry:
                    current = existing_digests.get(item.name)
                    if (
                        isinstance(current, str)
                        and _CONTENT_DIGEST.fullmatch(current)
                        and current != empty_digest
                    ):
                        skill_digests[item.name] = str(current)
                    elif parent_versions.get(item.name) == item.version and parent_digests.get(
                        item.name
                    ):
                        skill_digests[item.name] = str(parent_digests[item.name])
                    else:
                        skill_digests[item.name] = directory_digest(item.package_root)
                agents = dict(generation.get("agent_definitions") or {})
                default_agent_id = str(generation.get("default_agent_id") or "diagnostic-agent")
                generation["skill_digests"] = skill_digests
                generation["default_agent_id"] = default_agent_id
                generation["content_digest"] = generation_digest(
                    skill_digests,
                    agents,
                    default_agent_id,
                    {},
                )
                repaired[generation_id] = generation
                return generation

            for generation_id in generations:
                repair_generation(generation_id)
            migrated["generations"] = repaired
            if version == 1:
                candidates = dict(migrated.get("candidates") or {})
                for candidate_id, raw_candidate in candidates.items():
                    candidate = dict(raw_candidate)
                    if candidate.get("status") not in {"active", "rejected"}:
                        candidate["status"] = "draft"
                        candidate["validation"] = ValidationReport().model_dump(mode="json")
                        candidate["diff_digest"] = None
                        candidate["approval_id"] = None
                    candidates[candidate_id] = candidate
                migrated["candidates"] = candidates
                approvals = dict(migrated.get("approvals") or {})
                for approval_id, raw_approval in approvals.items():
                    approval = dict(raw_approval)
                    if approval.get("status") in {"pending", "approved"}:
                        approval["status"] = "rejected"
                        approval["reason"] = "Control-plane schema changed; revalidation required."
                    approvals[approval_id] = approval
                migrated["approvals"] = approvals
        migrated.setdefault("solutions", {})
        migrated.setdefault("solution_releases", {})
        solutions = dict(migrated["solutions"])
        for solution_id, raw_solution in solutions.items():
            solution = dict(raw_solution)
            solution.setdefault("allowed_roles", ["user"])
            solutions[solution_id] = solution
        migrated["solutions"] = solutions
        releases = dict(migrated["solution_releases"])
        for release_id, raw_release in releases.items():
            release = dict(raw_release)
            solution = solutions.get(str(release.get("solution_id")), {})
            allowed_roles = tuple(
                release.get("allowed_roles") or solution.get("allowed_roles") or ("user",)
            )
            release["allowed_roles"] = list(allowed_roles)
            component_paths = dict(release.get("component_paths") or {})
            release["component_paths"] = component_paths
            release.setdefault("package_relpath", None)
            package_metadata = dict(release.get("package_metadata") or {})
            release["package_metadata"] = package_metadata
            release["content_digest"] = solution_release_digest(
                solution_id=str(release["solution_id"]),
                version=str(release["version"]),
                entry_agent_id=(
                    str(release["entry_agent_id"])
                    if release.get("entry_agent_id") is not None
                    else None
                ),
                allowed_roles=allowed_roles,
                supported_profiles=tuple(release.get("supported_profiles") or ()),
                components=list(release.get("components") or ()),
                component_paths=component_paths,
                package_metadata=package_metadata,
            )
            releases[release_id] = release
        migrated["solution_releases"] = releases
        generations = dict(migrated.get("generations") or {})
        for generation_id, raw_generation in generations.items():
            generation = dict(raw_generation)
            solution_releases = dict(generation.get("solution_releases") or {})
            generation["solution_releases"] = solution_releases
            generation["content_digest"] = generation_digest(
                dict(generation.get("skill_digests") or {}),
                dict(generation.get("agent_definitions") or {}),
                str(generation.get("default_agent_id") or "diagnostic-agent"),
                solution_releases,
            )
            generations[generation_id] = generation
        migrated["generations"] = generations
        evaluations = dict(migrated.get("evaluations") or {})
        for report_id, raw_report in evaluations.items():
            report = dict(raw_report)
            release = releases.get(str(report.get("release_id")))
            if release is not None:
                report["release_digest"] = release["content_digest"]
                report["content_digest"] = canonical_digest(
                    {
                        "release_id": report["release_id"],
                        "release_digest": report["release_digest"],
                        "suite_id": report["suite_id"],
                        "suite_version": report["suite_version"],
                        "suite_digest": report["suite_digest"],
                        "status": report["status"],
                        "metrics": report.get("metrics") or {},
                        "summary": report.get("summary") or "",
                        "actor_fingerprint": report["actor_fingerprint"],
                    }
                )
            evaluations[report_id] = report
        migrated["evaluations"] = evaluations
        migrated["schema_version"] = 12
        migrated["tool_registrations"] = {}
        return migrated
