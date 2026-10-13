"""Creation and validation of immutable-version Agent drafts."""

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sage.control_models import AgentDraftRecord, ValidationIssue, ValidationReport
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.domain import AgentDefinition, DelegationDefinition, SubagentDefinition

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def agent_from_dict(value: dict[str, Any]) -> AgentDefinition:
    subagents = tuple(
        SubagentDefinition(
            name=item["name"],
            description=item["description"],
            instructions=item["instructions"],
            capability_ids=tuple(item.get("capability_ids", ())),
            exclusive_capability_ids=tuple(item.get("exclusive_capability_ids", ())),
        )
        for item in value.get("subagents", ())
    )
    delegations = tuple(
        DelegationDefinition(
            alias=item["alias"],
            target_agent_id=item["target_agent_id"],
            description=item["description"],
            max_calls=item.get("max_calls", 1),
        )
        for item in value.get("delegations", ())
    )
    return AgentDefinition(
        agent_id=value["agent_id"],
        name=value["name"],
        version=value["version"],
        instructions=value["instructions"],
        capability_ids=tuple(value.get("capability_ids", ())),
        subagents=subagents,
        delegations=delegations,
        memory_policy=value.get("memory_policy", "thread"),
        max_steps=value.get("max_steps", 40),
        max_model_calls=value.get("max_model_calls", 100),
        max_tool_calls=value.get("max_tool_calls", 100),
        max_subagent_calls=value.get("max_subagent_calls", 20),
    )


def agent_to_dict(definition: AgentDefinition) -> dict[str, Any]:
    value = asdict(definition)
    if not definition.delegations:
        value.pop("delegations")
    return value


class AgentDraftManager:
    def __init__(self, store: LocalControlStore) -> None:
        self._store = store

    def create(self, definition: dict[str, Any]) -> AgentDraftRecord:
        draft = AgentDraftRecord(
            draft_id=f"agd_{uuid4().hex}",
            definition=definition,
        )

        def save(state: object) -> None:
            state.agents[draft.draft_id] = draft  # type: ignore[attr-defined]

        self._store.update(save)
        return draft

    def ensure_published(self, definitions: dict[str, dict[str, Any]]) -> None:
        def apply(state: object) -> None:
            existing = {
                (
                    item.definition.get("agent_id"),
                    item.definition.get("version"),
                )
                for item in state.agents.values()  # type: ignore[attr-defined]
            }
            for definition in definitions.values():
                key = (definition.get("agent_id"), definition.get("version"))
                if key in existing:
                    continue
                digest = canonical_digest(definition)
                draft_id = f"agd_seed_{digest.split(':', 1)[1][:16]}"
                state.agents[draft_id] = AgentDraftRecord(  # type: ignore[attr-defined]
                    draft_id=draft_id,
                    definition=definition,
                    status="published",
                    validation=ValidationReport(
                        status="valid",
                        checked_at=datetime.now(UTC),
                        content_digest=digest,
                    ),
                )

        self._store.update(apply)

    def update(self, draft_id: str, definition: dict[str, Any]) -> AgentDraftRecord:
        def apply(state: object) -> AgentDraftRecord:
            draft = state.agents.get(draft_id)  # type: ignore[attr-defined]
            if draft is None:
                raise KeyError(draft_id)
            if draft.status == "published":
                raise ValueError("Published Agent versions are immutable; clone a new draft")
            draft.definition = definition
            draft.status = "draft"
            draft.validation = ValidationReport()
            draft.updated_at = datetime.now(UTC)
            return draft.model_copy(deep=True)

        return self._store.update(apply)

    def clone(self, draft_id: str, version: str) -> AgentDraftRecord:
        source = self.get(draft_id)
        definition = dict(source.definition)
        definition["version"] = version
        return self.create(definition)

    def validate(
        self,
        draft_id: str,
        available_capability_ids: set[str],
        published_versions: set[tuple[str, str]],
    ) -> AgentDraftRecord:
        draft = self.get(draft_id)
        issues: list[ValidationIssue] = []
        definition: AgentDefinition | None = None
        try:
            definition = agent_from_dict(draft.definition)
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(ValidationIssue(severity="error", code="invalid_agent", message=str(exc)))
        if definition is not None:
            if not _IDENTIFIER.fullmatch(definition.agent_id):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="invalid_agent_id",
                        message="Agent id must use lowercase letters, digits, and hyphens.",
                    )
                )
            if not _VERSION.fullmatch(definition.version):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="invalid_agent_version",
                        message="Agent version must be semantic version syntax.",
                    )
                )
            if (definition.agent_id, definition.version) in published_versions:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="published_version_exists",
                        message="This Agent id and version are already published and immutable.",
                    )
                )
            missing = set(definition.capability_ids) - available_capability_ids
            if missing:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="unknown_capability",
                        message=f"Unknown capabilities: {', '.join(sorted(missing))}",
                    )
                )
            published_agent_ids = {agent_id for agent_id, _ in published_versions}
            missing_agents = {
                delegation.target_agent_id
                for delegation in definition.delegations
                if delegation.target_agent_id not in published_agent_ids
            }
            if missing_agents:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="unknown_delegation_target",
                        message=(
                            "Delegation targets must be published Agents: "
                            f"{', '.join(sorted(missing_agents))}"
                        ),
                    )
                )
        status = "invalid" if any(item.severity == "error" for item in issues) else "valid"
        report = ValidationReport(
            status=status,
            checked_at=datetime.now(UTC),
            content_digest=canonical_digest(draft.definition),
            issues=tuple(issues),
        )

        def apply(state: object) -> AgentDraftRecord:
            current = state.agents[draft_id]  # type: ignore[attr-defined]
            current.status = status
            current.validation = report
            current.updated_at = datetime.now(UTC)
            return current.model_copy(deep=True)

        return self._store.update(apply)

    def get(self, draft_id: str) -> AgentDraftRecord:
        record = self._store.snapshot().agents.get(draft_id)
        if record is None:
            raise KeyError(draft_id)
        return record

    def list_drafts(self) -> list[AgentDraftRecord]:
        return sorted(
            self._store.snapshot().agents.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )
