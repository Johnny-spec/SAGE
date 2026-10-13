"""Catalog Candidate, approval, Generation activation, and rollback lifecycle."""

import hashlib
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

import yaml

from sage.agent_drafts import AgentDraftManager, agent_from_dict
from sage.control_models import (
    ApprovalRecord,
    CatalogCandidateRecord,
    GenerationRecord,
    ValidationIssue,
    ValidationReport,
)
from sage.control_store import LocalControlStore
from sage.digests import (
    canonical_digest,
    directory_digest,
    generation_digest,
    solution_release_digest,
)
from sage.evaluations import evaluation_report_digest
from sage.mcp_control import McpControlError, McpServerManager
from sage.registry import SkillRegistry
from sage.runtime_manager import RuntimeManager
from sage.skill_packages import SkillPackageManager
from sage.solution_packages import SolutionPackageError, validate_release_assets
from sage.tools import McpToolDefinition


class CatalogError(ValueError):
    """Raised when a Catalog lifecycle transition is invalid."""


CATALOG_BUILDER_VERSION = 1
_PROMOTION_ATTEMPTS = 5
_PROMOTION_DELAY_SECONDS = 0.05


def _promote_directory(staging_root: Path, generation_root: Path) -> None:
    if generation_root.exists():
        raise FileExistsError(f"Generation directory already exists: {generation_root}")
    for attempt in range(_PROMOTION_ATTEMPTS):
        try:
            staging_root.replace(generation_root)
            return
        except PermissionError:
            if (
                attempt == _PROMOTION_ATTEMPTS - 1
                or generation_root.exists()
                or not staging_root.exists()
            ):
                raise
            time.sleep(_PROMOTION_DELAY_SECONDS * (attempt + 1))


def initial_generation(
    skills_root: Path,
    agent_definitions: dict[str, dict[str, object]],
    default_agent_id: str,
) -> GenerationRecord:
    registry = SkillRegistry.load(skills_root)
    skill_digests = {item.name: directory_digest(item.package_root) for item in registry}
    return GenerationRecord(
        generation_id="gen_startup",
        content_digest=generation_digest(
            skill_digests,
            agent_definitions,
            default_agent_id,
        ),
        skills_root=str(skills_root.resolve()),
        skill_versions={item.name: item.version for item in registry},
        skill_digests=skill_digests,
        agent_definitions=agent_definitions,
        default_agent_id=default_agent_id,
    )


class CatalogManager:
    def __init__(
        self,
        store: LocalControlStore,
        agents: AgentDraftManager,
        skills: SkillPackageManager,
        mcp_servers: McpServerManager,
        runtimes: RuntimeManager,
        evaluation_gate_mode: Literal["advisory", "required"] = "advisory",
    ) -> None:
        self._store = store
        self._agents = agents
        self._skills = skills
        self._mcp_servers = mcp_servers
        self._runtimes = runtimes
        self._evaluation_gate_mode = evaluation_gate_mode
        self._activation_lock = RLock()

    def create_candidate(
        self,
        *,
        title: str,
        description: str,
        agent_draft_ids: tuple[str, ...],
        skill_package_ids: tuple[str, ...],
        solution_release_ids: tuple[str, ...] = (),
        tool_registration_ids: tuple[str, ...] = (),
        default_agent_id: str | None,
    ) -> CatalogCandidateRecord:
        if not title.strip():
            raise CatalogError("Candidate title is required")
        if (
            not agent_draft_ids
            and not skill_package_ids
            and not solution_release_ids
            and not tool_registration_ids
        ):
            raise CatalogError(
                "Candidate must include at least one Agent, Skill, Tool, or Solution Release change"
            )
        state = self._store.snapshot()
        selected_agents = list(agent_draft_ids)
        selected_skills = list(skill_package_ids)
        selected_tools = list(tool_registration_ids)
        base = state.generations[state.active_generation_id]
        for release_id in solution_release_ids:
            release = state.solution_releases.get(release_id)
            if release is None:
                continue
            for component in release.components:
                if component.kind == "agent":
                    active = base.agent_definitions.get(component.component_id)
                    if active and canonical_digest(active) == component.content_digest:
                        continue
                    matching = [
                        record.draft_id
                        for record in state.agents.values()
                        if record.status in {"valid", "published"}
                        and record.definition.get("agent_id") == component.component_id
                        and record.definition.get("version") == component.version
                        and record.validation.content_digest == component.content_digest
                    ]
                    if matching and not set(matching).intersection(selected_agents):
                        selected_agents.append(sorted(matching)[0])
                elif component.kind == "skill":
                    if base.skill_digests.get(component.component_id) == component.content_digest:
                        continue
                    matching = [
                        record.package_id
                        for record in state.skills.values()
                        if record.status in {"valid", "published"}
                        and record.name == component.component_id
                        and record.version == component.version
                        and record.content_digest == component.content_digest
                    ]
                    if matching and not set(matching).intersection(selected_skills):
                        selected_skills.append(sorted(matching)[0])
                elif component.kind == "mcp-tool":
                    active_tool = base.tool_definitions.get(component.component_id)
                    if active_tool and active_tool.content_digest == component.content_digest:
                        continue
                    matching = [
                        record.registration_id
                        for record in state.tool_registrations.values()
                        if record.tool_id == component.component_id
                        and record.definition.version == component.version
                        and record.content_digest == component.content_digest
                    ]
                    if matching and not set(matching).intersection(selected_tools):
                        selected_tools.append(sorted(matching)[0])
        candidate = CatalogCandidateRecord(
            candidate_id=f"cand_{uuid4().hex}",
            base_generation_id=state.active_generation_id,
            title=title.strip(),
            description=description.strip(),
            agent_draft_ids=tuple(selected_agents),
            skill_package_ids=tuple(selected_skills),
            tool_registration_ids=tuple(selected_tools),
            solution_release_ids=solution_release_ids,
            default_agent_id=default_agent_id,
        )

        def save(current: object) -> None:
            current.candidates[candidate.candidate_id] = candidate  # type: ignore[attr-defined]

        self._store.update(save)
        return candidate

    async def refresh_candidate_mcp(self, candidate_id: str) -> None:
        state = self._store.snapshot()
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        server_ids = {
            state.tool_registrations[identifier].definition.binding.server_id
            for identifier in candidate.tool_registration_ids
            if identifier in state.tool_registrations
        }
        server_ids.update(
            source_ref
            for identifier in candidate.skill_package_ids
            if identifier in state.skills and state.skills[identifier].source == "mcp"
            if (source_ref := state.skills[identifier].source_ref) is not None
        )
        for server_id in sorted(identifier for identifier in server_ids if identifier):
            try:
                await self._mcp_servers.ensure_fresh_discovery(server_id)
            except (KeyError, McpControlError, TimeoutError):
                continue

    def validate(self, candidate_id: str) -> CatalogCandidateRecord:
        def apply(current: object) -> CatalogCandidateRecord:
            item = current.candidates.get(candidate_id)  # type: ignore[attr-defined]
            if item is None:
                raise KeyError(candidate_id)
            if item.status not in {"draft", "invalid", "valid"}:
                raise CatalogError("Candidate cannot be validated in its current state")
            issues, diff_digest = self._evaluate(item, current)
            status = "invalid" if any(issue.severity == "error" for issue in issues) else "valid"
            report = ValidationReport(
                status=status,
                checked_at=datetime.now(UTC),
                content_digest=diff_digest,
                issues=tuple(issues),
            )
            item.status = status
            item.validation = report
            item.diff_digest = diff_digest
            item.approval_id = None
            item.updated_at = datetime.now(UTC)
            return item.model_copy(deep=True)

        return self._store.update(apply)

    def submit_approval(self, candidate_id: str) -> ApprovalRecord:
        candidate = self.get_candidate(candidate_id)
        if candidate.status != "valid" or not candidate.diff_digest:
            raise CatalogError("Candidate must pass validation before approval")
        approval = ApprovalRecord(
            approval_id=f"apr_{uuid4().hex}",
            kind="candidate",
            target_id=candidate_id,
            diff_digest=candidate.diff_digest,
        )

        def apply(state: object) -> None:
            current = state.candidates[candidate_id]  # type: ignore[attr-defined]
            current.status = "pending-approval"
            current.approval_id = approval.approval_id
            current.updated_at = datetime.now(UTC)
            state.approvals[approval.approval_id] = approval  # type: ignore[attr-defined]

        self._store.update(apply)
        return approval

    def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        actor_fingerprint: str,
        reason: str,
    ) -> ApprovalRecord:
        if not reason.strip():
            raise CatalogError("Approval decision requires a reason")

        def apply(state: object) -> ApprovalRecord:
            approval = state.approvals.get(approval_id)  # type: ignore[attr-defined]
            if approval is None:
                raise KeyError(approval_id)
            if approval.status != "pending":
                raise CatalogError("Approval has already been decided")
            approval.status = "approved" if approve else "rejected"
            approval.actor_fingerprint = actor_fingerprint
            approval.reason = reason.strip()
            approval.decided_at = datetime.now(UTC)
            if approval.kind == "candidate":
                candidate = state.candidates[approval.target_id]  # type: ignore[attr-defined]
                candidate.status = "approved" if approve else "rejected"
                candidate.updated_at = datetime.now(UTC)
            return approval.model_copy(deep=True)

        return self._store.update(apply)

    def activate(self, candidate_id: str) -> GenerationRecord:
        with self._activation_lock:
            return self._activate(candidate_id)

    def publish_development(
        self, candidate_id: str, *, environment: str
    ) -> GenerationRecord:
        if environment != "development":
            raise CatalogError("Quick publish is only available in development")
        with self._activation_lock:
            candidate = self.get_candidate(candidate_id)
            if candidate.status == "active" and candidate.generation_id:
                return self._store.snapshot().generations[candidate.generation_id]
            if candidate.status in {"draft", "invalid", "valid"}:
                candidate = self.validate(candidate_id)
                if candidate.status != "valid":
                    raise CatalogError("Candidate validation failed; review validation issues")
            elif candidate.status not in {"valid", "approved"}:
                raise CatalogError("Candidate cannot be published in its current state")
            return self._activate(candidate_id, development=True)

    def _activate(
        self, candidate_id: str, *, development: bool = False
    ) -> GenerationRecord:
        state = self._store.snapshot()
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        approval = state.approvals.get(candidate.approval_id) if candidate.approval_id else None
        if development:
            if candidate.status not in {"valid", "approved"}:
                raise CatalogError("Development Candidate must pass validation before activation")
        elif candidate.status != "approved" or approval is None or approval.status != "approved":
            raise CatalogError("Candidate requires an approved approval request")
        if state.active_generation_id != candidate.base_generation_id:
            raise CatalogError("Active Generation changed; rebuild the Candidate")
        issues, current_diff = self._evaluate(candidate, state)
        if any(item.severity == "error" for item in issues):
            raise CatalogError("Candidate is no longer valid")
        if current_diff != candidate.diff_digest or (
            approval is not None and approval.diff_digest != current_diff
        ):
            raise CatalogError("Candidate content changed after approval")

        generation_id = f"gen_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        generation = self._build_generation(generation_id, candidate, state)

        def stage(current: object) -> None:
            current.generations[generation_id] = generation  # type: ignore[attr-defined]

        self._store.update(stage)
        try:
            self._runtimes.activate(generation_id)
        except Exception:

            def fail(current: object) -> None:
                current.candidates[candidate_id].status = "activation-failed"  # type: ignore[attr-defined]

            self._store.update(fail)
            raise

        def complete(current: object) -> None:
            item = current.candidates[candidate_id]  # type: ignore[attr-defined]
            item.status = "active"
            item.generation_id = generation_id
            item.updated_at = datetime.now(UTC)
            if item.approval_id:
                current.approvals[item.approval_id].status = "consumed"  # type: ignore[attr-defined]
            for draft_id in item.agent_draft_ids:
                current.agents[draft_id].status = "published"  # type: ignore[attr-defined]
            for package_id in item.skill_package_ids:
                current.skills[package_id].status = "published"  # type: ignore[attr-defined]
            for registration_id in item.tool_registration_ids:
                record = current.tool_registrations[registration_id]  # type: ignore[attr-defined]
                current.tool_registrations[registration_id] = record.model_copy(  # type: ignore[attr-defined]
                    update={"status": "published"}
                )

        self._store.update(complete)
        return generation

    def request_rollback(self, generation_id: str) -> ApprovalRecord:
        state = self._store.snapshot()
        target = state.generations.get(generation_id)
        if target is None:
            raise KeyError(generation_id)
        if generation_id == state.active_generation_id:
            raise CatalogError("Target Generation is already active")
        digest = canonical_digest(
            {
                "from": state.active_generation_id,
                "to": generation_id,
                "target": target.content_digest,
            }
        )
        approval = ApprovalRecord(
            approval_id=f"apr_{uuid4().hex}",
            kind="rollback",
            target_id=generation_id,
            diff_digest=digest,
        )

        def save(current: object) -> None:
            current.approvals[approval.approval_id] = approval  # type: ignore[attr-defined]

        self._store.update(save)
        return approval

    def rollback(self, approval_id: str) -> GenerationRecord:
        with self._activation_lock:
            return self._rollback(approval_id)

    def _rollback(self, approval_id: str) -> GenerationRecord:
        state = self._store.snapshot()
        approval = state.approvals.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval.kind != "rollback" or approval.status != "approved":
            raise CatalogError("Rollback requires an approved rollback request")
        target = state.generations.get(approval.target_id)
        if target is None:
            raise CatalogError("Rollback target no longer exists")
        expected = canonical_digest(
            {
                "from": state.active_generation_id,
                "to": target.generation_id,
                "target": target.content_digest,
            }
        )
        if expected != approval.diff_digest:
            raise CatalogError("Active Generation changed after rollback approval")
        self._runtimes.activate(target.generation_id)

        def consume(current: object) -> None:
            current.approvals[approval_id].status = "consumed"  # type: ignore[attr-defined]

        self._store.update(consume)
        return target

    def list_candidates(self) -> list[CatalogCandidateRecord]:
        return sorted(
            self._store.snapshot().candidates.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def get_candidate(self, candidate_id: str) -> CatalogCandidateRecord:
        item = self._store.snapshot().candidates.get(candidate_id)
        if item is None:
            raise KeyError(candidate_id)
        return item

    def list_generations(self) -> list[GenerationRecord]:
        return sorted(
            self._store.snapshot().generations.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def list_approvals(self) -> list[ApprovalRecord]:
        return sorted(
            self._store.snapshot().approvals.values(),
            key=lambda item: item.requested_at,
            reverse=True,
        )

    def _evaluate(
        self,
        candidate: CatalogCandidateRecord,
        state: object,
    ) -> tuple[list[ValidationIssue], str]:
        issues: list[ValidationIssue] = []
        base = state.generations.get(candidate.base_generation_id)  # type: ignore[attr-defined]
        if base is None:
            issues.append(
                ValidationIssue(
                    severity="error", code="missing_base", message="Base Generation is missing."
                )
            )
            return issues, canonical_digest({"candidate": candidate.candidate_id, "missing": True})
        desired_skills = dict(base.skill_versions)
        desired_digests = dict(base.skill_digests)
        actual_base_digest = generation_digest(
            base.skill_digests,
            base.agent_definitions,
            base.default_agent_id,
            base.solution_releases,
            {tool_id: tool.content_digest for tool_id, tool in base.tool_definitions.items()},
        )
        if actual_base_digest != base.content_digest:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="base_generation_tampered",
                    message="Base Generation content no longer matches its immutable digest.",
                )
            )
        skill_definitions = {item.name: item for item in SkillRegistry.load(Path(base.skills_root))}
        executable_capabilities = {
            f"skill:{item.name}"
            for item in skill_definitions.values()
            if item.manifest.spec.policy.execution_enabled
        }
        base_executable_capabilities = executable_capabilities | {
            tool_id for tool_id, tool in base.tool_definitions.items() if tool.enabled
        }
        selected_names: set[str] = set()
        selected_skill_digests: dict[str, str] = {}
        effective_changes = 0
        for package_id in candidate.skill_package_ids:
            package = state.skills.get(package_id)  # type: ignore[attr-defined]
            if package is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_skill",
                        message=f"Skill package not found: {package_id}",
                    )
                )
                continue
            if (
                package.status not in {"valid", "published"}
                or not package.name
                or not package.version
            ):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="skill_not_valid",
                        message=f"Skill package is not valid: {package_id}",
                    )
                )
                continue
            package_root = self._store.resolve_path(package.package_relpath)
            actual_package_digest = directory_digest(package_root)
            if (
                actual_package_digest != package.content_digest
                or package.validation.content_digest != package.content_digest
            ):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="skill_content_changed",
                        message=f"Skill package changed after validation: {package_id}",
                    )
                )
                continue
            if package.name in selected_names:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_skill",
                        message=f"Candidate selects multiple versions of {package.name}",
                    )
                )
                continue
            selected_names.add(package.name)
            if desired_skills.get(package.name) == package.version and desired_digests.get(
                package.name
            ) not in {None, package.content_digest}:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="immutable_version_changed",
                        message=(
                            f"Skill {package.name}@{package.version} already exists with "
                            "different content."
                        ),
                    )
                )
            if (
                desired_skills.get(package.name) != package.version
                or desired_digests.get(package.name) != package.content_digest
            ):
                effective_changes += 1
            desired_skills[package.name] = package.version
            desired_digests[package.name] = package.content_digest
            selected_skill_digests[package_id] = package.content_digest
            skill_definitions[package.name] = SkillRegistry.load_definition(package_root)
            capability_id = f"skill:{package.name}"
            if package.executable:
                executable_capabilities.add(capability_id)
            else:
                executable_capabilities.discard(capability_id)
            if package.source == "mcp":
                server = state.mcp_servers.get(package.source_ref or "")  # type: ignore[attr-defined]
                if server is None:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="missing_mcp_server",
                            message=f"MCP server missing for {package.name}",
                        )
                    )
                else:
                    try:
                        self._mcp_servers.require_fresh_discovery(server)
                    except McpControlError as exc:
                        issues.append(
                            ValidationIssue(severity="error", code="stale_mcp", message=str(exc))
                        )
        desired_tools = dict(base.tool_definitions)
        selected_tool_digests: dict[str, str] = {}
        for registration_id in candidate.tool_registration_ids:
            registration = state.tool_registrations.get(registration_id)  # type: ignore[attr-defined]
            if registration is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_tool",
                        message=f"Tool registration missing: {registration_id}",
                    )
                )
                continue
            definition = registration.definition
            tool_id = definition.tool_id
            if tool_id in selected_tool_digests:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_tool",
                        message=f"Multiple versions selected: {tool_id}",
                    )
                )
                continue
            selected_tool_digests[tool_id] = definition.content_digest
            if (
                registration.content_digest != definition.content_digest
                or registration.tool_id != tool_id
            ):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="tool_content_changed",
                        message=f"Tool changed after registration: {tool_id}",
                    )
                )
                continue
            previous = desired_tools.get(tool_id)
            if (
                previous
                and previous.version == definition.version
                and previous.content_digest != definition.content_digest
            ):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="immutable_tool_version_changed",
                        message=f"Tool version content changed: {tool_id}",
                    )
                )
            if previous is None or previous.content_digest != definition.content_digest:
                effective_changes += 1
            try:
                server = self._mcp_servers.get(definition.binding.server_id)
                self._mcp_servers.require_fresh_discovery(server)
                if server.config_digest != definition.binding.server_config_digest:
                    raise McpControlError(
                        "MCP server configuration changed; register a new tool version"
                    )
                discovered = next(
                    (tool for tool in server.tools if tool.name == definition.binding.tool_name),
                    None,
                )
                if (
                    discovered is None
                    or discovered.schema_digest != definition.binding.schema_digest
                ):
                    raise McpControlError("MCP tool schema changed; register a new tool version")
            except (KeyError, McpControlError) as exc:
                issues.append(ValidationIssue(severity="error", code="stale_mcp", message=str(exc)))
            desired_tools[tool_id] = definition
        executable_capabilities.update(
            tool_id for tool_id, tool in desired_tools.items() if tool.enabled
        )
        for skill in skill_definitions.values():
            dependencies = set(skill.manifest.spec.execution.capability_ids)
            missing = dependencies - executable_capabilities
            nested = {
                dependency
                for dependency in dependencies
                if dependency.startswith("skill:")
                and dependency.removeprefix("skill:") in skill_definitions
                and skill_definitions[
                    dependency.removeprefix("skill:")
                ].manifest.spec.execution.type
                in {"workflow", "model"}
            }
            if missing or nested:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="skill_dependency_unavailable",
                        message=f"Skill {skill.name} has unavailable/nested dependencies: "
                        f"{', '.join(sorted(missing | nested))}",
                    )
                )
        desired_agents = dict(base.agent_definitions)
        selected_agent_digests: dict[str, str] = {}
        for draft_id in candidate.agent_draft_ids:
            draft = state.agents.get(draft_id)  # type: ignore[attr-defined]
            if draft is None or draft.status not in {"valid", "published"}:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="agent_not_valid",
                        message=f"Agent draft is not valid: {draft_id}",
                    )
                )
                continue
            try:
                definition = agent_from_dict(draft.definition)
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(
                    ValidationIssue(severity="error", code="invalid_agent", message=str(exc))
                )
                continue
            actual_agent_digest = canonical_digest(draft.definition)
            if draft.validation.content_digest != actual_agent_digest:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="agent_content_changed",
                        message=f"Agent draft changed after validation: {draft_id}",
                    )
                )
                continue
            existing = desired_agents.get(definition.agent_id)
            if (
                existing is not None
                and existing.get("version") == definition.version
                and canonical_digest(existing) != canonical_digest(draft.definition)
            ):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="immutable_agent_version_changed",
                        message=(
                            f"Agent {definition.agent_id}@{definition.version} already exists "
                            "with different content."
                        ),
                    )
                )
            if existing is None or canonical_digest(existing) != actual_agent_digest:
                effective_changes += 1
            missing = set(definition.capability_ids) - executable_capabilities
            if missing:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="agent_uses_unavailable_capability",
                        message=(
                            f"Agent {definition.agent_id} references unavailable capabilities: "
                            f"{', '.join(sorted(missing))}"
                        ),
                    )
                )
            desired_agents[definition.agent_id] = dict(draft.definition)
            selected_agent_digests[draft_id] = canonical_digest(draft.definition)
        default_agent_id = candidate.default_agent_id or base.default_agent_id
        for agent_id, definition in desired_agents.items():
            missing = set(definition.get("capability_ids", ())) - executable_capabilities
            if definition == base.agent_definitions.get(agent_id):
                missing &= base_executable_capabilities
            if missing:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="agent_uses_unavailable_capability",
                        message=f"Agent {agent_id} references unavailable capabilities: "
                        f"{', '.join(sorted(missing))}",
                    )
                )
        if default_agent_id not in desired_agents:
            available_agents = ", ".join(sorted(desired_agents)) or "none"
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing_default_agent",
                    message=(
                        f"Default Agent '{default_agent_id}' is not present in the Candidate. "
                        f"Available Agent IDs: {available_agents}."
                    ),
                )
            )
        if default_agent_id != base.default_agent_id:
            effective_changes += 1
        desired_solution_releases = dict(base.solution_releases)
        selected_release_digests: dict[str, str] = {}
        selected_solution_ids: set[str] = set()
        for release_id in candidate.solution_release_ids:
            release = state.solution_releases.get(release_id)  # type: ignore[attr-defined]
            if release is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="missing_solution_release",
                        message=f"Solution Release not found: {release_id}",
                    )
                )
                continue
            if release.solution_id in selected_solution_ids:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_solution_release",
                        message=(
                            "Candidate selects multiple Releases for "
                            f"Solution {release.solution_id}"
                        ),
                    )
                )
                continue
            selected_solution_ids.add(release.solution_id)
            actual_release_digest = solution_release_digest(
                solution_id=release.solution_id,
                version=release.version,
                entry_agent_id=release.entry_agent_id,
                allowed_roles=release.allowed_roles,
                supported_profiles=release.supported_profiles,
                components=[item.model_dump(mode="json") for item in release.components],
                component_paths=release.component_paths,
                package_metadata=release.package_metadata,
            )
            if actual_release_digest != release.content_digest:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="solution_release_changed",
                        message=f"Solution Release content changed: {release_id}",
                    )
                )
                continue
            if desired_solution_releases.get(release.solution_id) != release.release_id:
                effective_changes += 1
            desired_solution_releases[release.solution_id] = release.release_id
            selected_release_digests[release.release_id] = release.content_digest
        self._validate_solution_releases(
            desired_solution_releases,
            desired_skills,
            desired_digests,
            desired_agents,
            state,
            issues,
            desired_tools,
            {
                name: definition.manifest.spec.execution.capability_ids
                for name, definition in skill_definitions.items()
            },
        )
        if effective_changes == 0:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="no_effective_changes",
                    message="Candidate does not change the active Catalog.",
                )
            )
        diff_digest = canonical_digest(
            {
                "builder_version": CATALOG_BUILDER_VERSION,
                "base_generation_id": candidate.base_generation_id,
                "skills": selected_skill_digests,
                "agents": selected_agent_digests,
                "solution_releases": selected_release_digests,
                "default_agent_id": default_agent_id,
                **({"tools": selected_tool_digests} if selected_tool_digests else {}),
            }
        )
        return issues, diff_digest

    def _build_generation(
        self,
        generation_id: str,
        candidate: CatalogCandidateRecord,
        state: object,
    ) -> GenerationRecord:
        base = state.generations[candidate.base_generation_id]  # type: ignore[attr-defined]
        generation_root = self._store.generations_root / generation_id
        staging_root = self._store.generations_root / f".{generation_id}.tmp"
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.copytree(Path(base.skills_root), staging_root)
        skill_digests = dict(base.skill_digests)
        try:
            for package_id in candidate.skill_package_ids:
                package = state.skills[package_id]  # type: ignore[attr-defined]
                source = self._store.resolve_path(package.package_relpath)
                destination = staging_root / package.name
                shutil.rmtree(destination, ignore_errors=True)
                shutil.copytree(source, destination)
                if not package.executable:
                    self._disable_execution(destination / "skill.yaml")
                skill_digests[package.name] = directory_digest(destination)
            registry = SkillRegistry.load(staging_root)
            desired_agents = dict(base.agent_definitions)
            for draft_id in candidate.agent_draft_ids:
                draft = state.agents[draft_id]  # type: ignore[attr-defined]
                definition = agent_from_dict(draft.definition)
                desired_agents[definition.agent_id] = dict(draft.definition)
            default_agent_id = candidate.default_agent_id or base.default_agent_id
            desired_tools = dict(base.tool_definitions)
            for registration_id in candidate.tool_registration_ids:
                record = state.tool_registrations[registration_id]  # type: ignore[attr-defined]
                desired_tools[record.tool_id] = record.definition
            content_digest = generation_digest(
                skill_digests,
                desired_agents,
                default_agent_id,
                {
                    **base.solution_releases,
                    **{
                        state.solution_releases[release_id].solution_id: release_id  # type: ignore[attr-defined]
                        for release_id in candidate.solution_release_ids
                    },
                },
                {tool_id: tool.content_digest for tool_id, tool in desired_tools.items()},
            )
            skill_versions = {item.name: item.version for item in registry}
            solution_releases = dict(base.solution_releases)
            for release_id in candidate.solution_release_ids:
                release = state.solution_releases[release_id]  # type: ignore[attr-defined]
                solution_releases[release.solution_id] = release.release_id
            _promote_directory(staging_root, generation_root)
        except BaseException:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        return GenerationRecord(
            generation_id=generation_id,
            parent_generation_id=base.generation_id,
            content_digest=content_digest,
            skills_root=str(generation_root.resolve()),
            skill_versions=skill_versions,
            skill_digests=skill_digests,
            agent_definitions=desired_agents,
            tool_definitions=desired_tools,
            solution_releases=solution_releases,
            default_agent_id=default_agent_id,
            candidate_id=candidate.candidate_id,
        )

    def _validate_solution_releases(
        self,
        desired_releases: dict[str, str],
        skill_versions: dict[str, str],
        skill_digests: dict[str, str],
        agent_definitions: dict[str, dict[str, object]],
        state: object,
        issues: list[ValidationIssue],
        tool_definitions: dict[str, McpToolDefinition],
        skill_dependencies: dict[str, tuple[str, ...]],
    ) -> None:
        for solution_id, release_id in desired_releases.items():
            release = state.solution_releases.get(release_id)  # type: ignore[attr-defined]
            if release is None or release.solution_id != solution_id:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="invalid_solution_release",
                        message=f"Generation references invalid Solution Release: {release_id}",
                    )
                )
                continue
            try:
                validate_release_assets(self._store, release)
            except SolutionPackageError as exc:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="solution_asset_changed",
                        message=str(exc),
                    )
                )
            locked_skills = {
                item.component_id: item for item in release.components if item.kind == "skill"
            }
            locked_agents = {
                item.component_id: item for item in release.components if item.kind == "agent"
            }
            locked_evaluations = {
                item.component_id: item
                for item in release.components
                if item.kind == "evaluation-suite"
            }
            locked_tools = {
                item.component_id: item for item in release.components if item.kind == "mcp-tool"
            }
            for tool_id, component in locked_tools.items():
                tool = tool_definitions.get(tool_id)
                if (
                    tool is None
                    or tool.version != component.version
                    or tool.content_digest != component.content_digest
                ):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="solution_tool_lock_mismatch",
                            message=f"Solution Release {release_id} does not match Tool {tool_id}",
                        )
                    )
            for skill_id, component in locked_skills.items():
                missing = set(skill_dependencies.get(skill_id, ())) - (
                    {f"skill:{name}" for name in locked_skills} | set(locked_tools)
                )
                if missing:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="solution_skill_capability_not_locked",
                            message=f"Solution Release {release_id} Skill {skill_id} uses "
                            f"unlocked capabilities: {', '.join(sorted(missing))}",
                        )
                    )
                if (
                    skill_versions.get(skill_id) != component.version
                    or skill_digests.get(skill_id) != component.content_digest
                ):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="solution_skill_lock_mismatch",
                            message=(
                                f"Solution Release {release_id} does not match "
                                f"Skill {skill_id}@{component.version}."
                            ),
                        )
                    )
            for suite_id, component in locked_evaluations.items():
                passed = any(
                    report.release_id == release.release_id
                    and report.release_digest == release.content_digest
                    and report.suite_id == suite_id
                    and report.suite_version == component.version
                    and report.suite_digest == component.content_digest
                    and report.status == "passed"
                    and evaluation_report_digest(report) == report.content_digest
                    for report in state.evaluations.values()  # type: ignore[attr-defined]
                )
                if not passed:
                    issues.append(
                        ValidationIssue(
                            severity=(
                                "error"
                                if self._evaluation_gate_mode == "required"
                                else "warning"
                            ),
                            code="solution_evaluation_not_passed",
                            message=(
                                f"Solution Release {release_id} has no passing report for "
                                f"Evaluation Suite {suite_id}@{component.version}."
                            ),
                        )
                    )
            for agent_id, component in locked_agents.items():
                definition = agent_definitions.get(agent_id)
                if (
                    definition is None
                    or definition.get("version") != component.version
                    or canonical_digest(definition) != component.content_digest
                ):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="solution_agent_lock_mismatch",
                            message=(
                                f"Solution Release {release_id} does not match "
                                f"Agent {agent_id}@{component.version}."
                            ),
                        )
                    )
                    continue
                raw_capability_ids = definition.get("capability_ids", ())
                capability_ids = (
                    {item for item in raw_capability_ids if isinstance(item, str)}
                    if isinstance(raw_capability_ids, (list, tuple, set, frozenset))
                    else set()
                )
                missing = capability_ids - (
                    {f"skill:{name}" for name in locked_skills} | set(locked_tools)
                )
                if missing:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="solution_agent_capability_not_locked",
                            message=(
                                f"Solution Release {release_id} Agent {agent_id} uses "
                                f"unlocked capabilities: {', '.join(sorted(missing))}"
                            ),
                        )
                    )

    @staticmethod
    def _disable_execution(manifest_path: Path) -> None:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("spec", {}).setdefault("policy", {})["executionEnabled"] = False
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def admin_token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
