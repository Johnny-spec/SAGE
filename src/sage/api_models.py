"""Stable HTTP request and response contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RouteMethod = Literal["explicit", "rule", "llm"]


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    mode: Literal["direct", "agent"] = "direct"
    solution_id: str | None = Field(default=None, max_length=64)
    agent_id: str | None = Field(default=None, max_length=128)
    skill: str | None = None
    arguments: dict[str, Any] | None = None
    thread_id: str | None = Field(default=None, max_length=128)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)


class AttachmentResponse(BaseModel):
    attachment_id: str
    artifact_id: str
    filename: str
    size: int
    media_type: str
    content_digest: str
    solution_id: str | None = None


class UserMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)
    sensitivity: Literal["normal", "sensitive"] = "normal"
    source_run_id: str | None = Field(default=None, max_length=128)
    expires_at: datetime | None = None


class DurableInvocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str | None = Field(default=None, min_length=1, max_length=64)
    workflow_type: str | None = Field(default=None, min_length=1, max_length=255)
    skill: str | None = Field(default=None, min_length=1, max_length=64)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["direct", "agent"] = "agent"
    idempotency_key: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=2_592_000)


class DurableInvocationResponse(BaseModel):
    run_id: str
    workflow_id: str
    temporal_run_id: str | None = None
    status: str
    solution_id: str
    solution_release_id: str


class HumanTaskDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: dict[str, Any] = Field(default_factory=dict)
    approve: bool | None = None
    reason: str = Field(default="", max_length=4000)


class SkillSummary(BaseModel):
    name: str
    description: str
    version: str
    risk: Literal["read-only", "write-low", "write-high", "destructive"]


class SolutionSummary(BaseModel):
    solution_id: str
    title: str
    description: str
    release_id: str
    version: str
    entry_agent_id: str | None
    supported_profiles: list[str]


class AgentModelConfiguration(BaseModel):
    env_file_present: bool
    authentication: Literal["offline", "api-key", "entra-id"]
    token_scope: str | None
    temperature: float | None
    max_tokens: int | None
    timeout_seconds: float
    max_retries: int
    recursion_limit: int


class AgentStatus(BaseModel):
    agent_id: str
    version: str
    runtime: str
    model: str
    subagents: list[str]
    capabilities: list[str]
    memory: str
    configuration: AgentModelConfiguration


class AgentSummary(BaseModel):
    agent_id: str
    name: str
    version: str
    subagents: list[str]
    capabilities: list[str]
    memory: str
    max_steps: int
    max_model_calls: int
    max_tool_calls: int
    max_subagent_calls: int


class RouteInfo(BaseModel):
    skill: str
    method: RouteMethod
    reason: str


class UsageInfo(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    subagent_calls: int = 0
    duration_ms: int = 0


class MessageResponse(BaseModel):
    status: Literal["completed", "clarification_required"]
    message: str
    run_id: str | None = None
    thread_id: str | None = None
    route: RouteInfo | None = None
    result: dict[str, Any] | None = None
    usage: UsageInfo = Field(default_factory=UsageInfo)


class InvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str | None = Field(default=None, max_length=64)
    capability_id: str = Field(min_length=1, max_length=2000)
    input: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=255)
    parent_run_id: str | None = Field(default=None, max_length=128)
    approval_task_id: str | None = Field(default=None, max_length=128)


class InvocationResult(BaseModel):
    status: Literal["completed", "awaiting-approval"]
    invocation_id: str
    run_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    capability_id: str
    output: dict[str, Any]
    usage: UsageInfo
    approval_task_id: str | None = None


class AgentInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_run_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=255)
    turn_index: int = Field(default=0, ge=0, le=100)


class AgentInvocationResult(BaseModel):
    status: Literal["completed", "awaiting-approval"]
    invocation_id: str
    run_id: str
    parent_run_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    agent_id: str
    output: dict[str, Any]
    usage: UsageInfo
    tasks: list[dict[str, Any]] = Field(default_factory=list)
