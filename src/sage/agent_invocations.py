"""Framework-neutral contracts for governed Agent-to-Agent invocation."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sage.domain import Principal


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_run_id: str = Field(min_length=1, max_length=128)
    target_agent_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=255)
    turn_index: int = Field(default=0, ge=0, le=100)


class AgentTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    run_id: str
    parent_run_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    agent_id: str
    output: dict[str, Any]
    usage: dict[str, int]
    status: Literal["completed", "awaiting-approval"] = "completed"
    tasks: list[dict[str, Any]] = Field(default_factory=list)


AgentInvoke = Callable[[AgentTask, Principal], Awaitable[AgentTaskResult]]