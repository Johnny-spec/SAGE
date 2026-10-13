"""Persisted, unpublished MCP test intents and reviewed risk classifications."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolRisk = Literal["read-only", "write-low", "write-high", "destructive"]


class ToolRiskReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=255)
    expected_server_config_digest: str
    expected_schema_digest: str
    risk: ToolRisk


class ToolRiskReview(ToolRiskReviewInput):
    review_id: str
    server_id: str
    actor_id: str
    workspace_id: str


class ToolTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=255)
    arguments: dict[str, Any]
    expected_server_config_digest: str
    expected_schema_digest: str
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirm_write: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    max_output_bytes: int = Field(default=262144, ge=1024, le=262144)


class ToolTestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_id: str
    server_id: str
    tool_name: str
    actor_id: str
    workspace_id: str
    request: ToolTestInput | None
    request_digest: str
    review_id: str
    risk: ToolRisk
    status: Literal[
        "prepared",
        "awaiting-approval",
        "running",
        "completed",
        "rejected",
        "failed",
        "outcome-uncertain",
        "tool-error",
        "invalid-output",
    ] = "prepared"
    approval_task_id: str | None = None
    workflow_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    elapsed_ms: int | None = None
    result: dict[str, Any] | None = None
