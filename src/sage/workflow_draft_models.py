"""Editable workflow drafts, kept separate from published execution templates."""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage.sensitive_data import contains_sensitive


class WorkflowPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=6000, allow_inf_nan=False)
    y: float = Field(ge=0, le=6000, allow_inf_nan=False)


class WorkflowDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: dict[str, WorkflowPosition] = Field(default_factory=dict, max_length=500)
    labels: dict[str, str] = Field(default_factory=dict, max_length=500)


class WorkflowDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    solution_id: str = Field(min_length=1, max_length=64)
    definition: dict[str, Any] = Field(default_factory=dict)
    design: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_content(self) -> "WorkflowDraftInput":
        WorkflowDesign.model_validate(self.design)
        content = {"title": self.title, "solution_id": self.solution_id,
                   "definition": self.definition, "design": self.design}
        pending = [(content, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                raise ValueError("Workflow nesting exceeds 32 levels")
            if isinstance(value, dict):
                if any(key in {"__proto__", "constructor", "prototype"} for key in value):
                    raise ValueError("Reserved object keys are not allowed in drafts")
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        try:
            encoded = json.dumps(content, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Draft must contain finite JSON data") from exc
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise ValueError("Workflow draft exceeds 512 KiB")
        if contains_sensitive(content):
            raise ValueError("Sensitive fields cannot be stored in workflow drafts")
        return self


class WorkflowDraftRecord(WorkflowDraftInput):
    draft_id: str
    workspace_id: str
    created_by: str
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))