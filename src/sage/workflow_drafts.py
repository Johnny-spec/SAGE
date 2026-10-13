"""Revision-controlled, workspace-scoped draft editing without publication."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from sage.control_models import ControlPlaneState
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.workflow_draft_models import WorkflowDraftInput, WorkflowDraftRecord
from sage.workflows import WorkflowTemplate


class DraftConflict(ValueError):
    pass


class WorkflowDraftManager:
    def __init__(
        self, store: LocalControlStore, unavailable_skills: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self._unavailable_skills = unavailable_skills

    def list(self, workspace_id: str) -> list[WorkflowDraftRecord]:
        return sorted(
            (draft for draft in self._store.snapshot().workflow_drafts.values()
             if draft.workspace_id == workspace_id),
            key=lambda draft: draft.updated_at,
            reverse=True,
        )

    def get(self, draft_id: str, workspace_id: str) -> WorkflowDraftRecord:
        return self._find(self._store.snapshot(), draft_id, workspace_id)

    @staticmethod
    def _find(
        state: ControlPlaneState, draft_id: str, workspace_id: str,
    ) -> WorkflowDraftRecord:
        draft = state.workflow_drafts.get(draft_id)
        if draft is None or draft.workspace_id != workspace_id:
            raise KeyError(draft_id)
        return draft

    def create(
        self, payload: WorkflowDraftInput, workspace_id: str, actor: str,
    ) -> WorkflowDraftRecord:
        record = WorkflowDraftRecord(
            **payload.model_dump(), draft_id=f"wfd_{uuid4().hex}",
            workspace_id=workspace_id, created_by=actor,
        )

        def save(state: ControlPlaneState) -> WorkflowDraftRecord:
            if record.solution_id not in state.solutions:
                raise ValueError("Select an existing Solution")
            state.workflow_drafts[record.draft_id] = record.model_copy(deep=True)
            return record.model_copy(deep=True)

        return self._store.update(save)

    def update(
        self, draft_id: str, workspace_id: str, revision: int, payload: WorkflowDraftInput,
    ) -> WorkflowDraftRecord:
        def save(state: ControlPlaneState) -> WorkflowDraftRecord:
            previous = self._find(state, draft_id, workspace_id)
            if previous.revision != revision:
                raise DraftConflict("Draft changed; reload before saving")
            if payload.solution_id != previous.solution_id:
                raise ValueError("Draft Solution cannot be changed")
            record = WorkflowDraftRecord(
                **previous.model_dump(exclude={"revision", "updated_at",
                                               *WorkflowDraftInput.model_fields}),
                **payload.model_dump(), revision=revision + 1, updated_at=datetime.now(UTC),
            )
            state.workflow_drafts[draft_id] = record
            return record.model_copy(deep=True)

        return self._store.update(save)

    def delete(self, draft_id: str, workspace_id: str, revision: int) -> None:
        def remove(state: ControlPlaneState) -> None:
            draft = self._find(state, draft_id, workspace_id)
            if draft.revision != revision:
                raise DraftConflict("Draft changed; reload before deleting")
            del state.workflow_drafts[draft_id]

        self._store.update(remove)

    def validate(self, draft_id: str, workspace_id: str, revision: int) -> dict[str, Any]:
        state = self._store.snapshot()
        draft = self._find(state, draft_id, workspace_id)
        if draft.revision != revision:
            raise DraftConflict("Draft changed; reload before validating")
        issues: list[dict[str, Any]] = []
        compiled = None
        try:
            template = WorkflowTemplate.model_validate(draft.definition)
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_context=False):
                issues.append({"code": "invalid_workflow", "severity": "error",
                               "field_path": ".".join(map(str, error["loc"])),
                               "message": error["msg"]})
        else:
            active = state.generations[state.active_generation_id]
            for step in template.steps:
                issue = None
                if step.kind in {"workflow", "human-task"}:
                    issue = "This node kind is not available in the initial designer"
                elif step.concurrency_key or step.failure_policy == "escalate":
                    issue = "Concurrency keys and automatic escalation are not implemented"
                elif step.kind == "agent" and step.target not in active.agent_definitions:
                    issue = "Agent is not in the active Generation"
                elif step.kind == "skill" and (
                    step.target not in active.skill_versions
                    or step.target in self._unavailable_skills
                ):
                    issue = "Skill is unavailable or requires unsupported nested orchestration"
                elif step.kind == "tool" and not any(
                    tool.tool_id == step.target and tool.enabled
                    for tool in active.tool_definitions.values()
                ):
                    issue = "Tool is not enabled in the active Generation"
                if issue:
                    issues.append({"code": "unavailable_target_or_feature", "severity": "error",
                                   "node_id": step.step_id, "field_path": "steps",
                                   "message": issue})
            compiled = template.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {
            "draft_revision": revision, "generation_id": state.active_generation_id,
            "status": "invalid" if issues else "valid", "issues": issues,
            "definition_digest": canonical_digest(compiled or draft.definition),
            "compiled": compiled, "scope": "structure-and-active-targets",
            "publication_ready": False,
        }

    def export(self, draft_id: str, workspace_id: str, revision: int) -> str:
        report = self.validate(draft_id, workspace_id, revision)
        if report["status"] != "valid":
            raise ValueError("Fix validation errors before exporting executable YAML")
        return yaml.safe_dump(report["compiled"], sort_keys=False, allow_unicode=True)