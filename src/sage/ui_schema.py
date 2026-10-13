"""Safe schema-driven UI contracts for Solution workspaces."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class UiAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["invoke", "start-workflow", "submit-human-task", "navigate"]
    target: str = Field(min_length=1, max_length=255)
    confirmation: str | None = Field(default=None, max_length=500)


class UiWidget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    widget_id: str = Field(min_length=1, max_length=100)
    kind: Literal[
        "input-form",
        "chat",
        "artifact-list",
        "artifact-viewer",
        "result-table",
        "result-detail",
        "citation-list",
        "run-timeline",
        "human-task-list",
    ]
    title: str | None = Field(default=None, max_length=200)
    data_source: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)
    actions: tuple[UiAction, ...] = ()

    @model_validator(mode="after")
    def restrict_data_source(self) -> "UiWidget":
        if self.data_source is not None and not self.data_source.startswith("/v1/"):
            raise ValueError("UI data sources must use a platform /v1/ endpoint")
        return self


class SolutionUiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    solution_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    layout: Literal["workspace", "conversation", "dashboard"] = "workspace"
    widgets: tuple[UiWidget, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_unique_ids(self) -> "SolutionUiSchema":
        widget_ids = [widget.widget_id for widget in self.widgets]
        if len(widget_ids) != len(set(widget_ids)):
            raise ValueError("UI widget ids must be unique")
        action_ids = [
            action.action_id
            for widget in self.widgets
            for action in widget.actions
        ]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("UI action ids must be unique")
        return self


def load_solution_ui_schema(path: Path) -> SolutionUiSchema:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Solution UI Schema must be an object")
    return SolutionUiSchema.model_validate(raw)