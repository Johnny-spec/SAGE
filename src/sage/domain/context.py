"""Identity and run context contracts for governed execution."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

RunMode = Literal["direct", "agent"]
RunLifecycle = Literal["interactive", "durable", "scheduled", "event-driven"]
AuthenticationMethod = Literal["anonymous", "local", "api-key", "entra-id"]

DEFAULT_WORKSPACE_ID = "ws_default"
STARTUP_GENERATION_ID = "gen_startup"


def _require_identifier(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated caller identity used for authorization and data scoping."""

    workspace_id: str
    principal_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    authentication_method: AuthenticationMethod = "anonymous"

    def __post_init__(self) -> None:
        _require_identifier("workspace_id", self.workspace_id)
        _require_identifier("principal_id", self.principal_id)
        object.__setattr__(self, "roles", frozenset(self.roles))

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls(
            workspace_id=DEFAULT_WORKSPACE_ID,
            principal_id="anonymous",
            roles=frozenset({"user"}),
        )

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Immutable identity and version scope for one user-visible run."""

    run_id: str
    thread_id: str
    principal: Principal
    mode: RunMode
    lifecycle: RunLifecycle = "interactive"
    generation_id: str = STARTUP_GENERATION_ID
    solution_id: str | None = None
    solution_release_id: str | None = None
    agent_id: str | None = None
    skill_id: str | None = None
    root_run_id: str | None = None
    parent_run_id: str | None = None
    approved_capability_ids: frozenset[str] = field(default_factory=frozenset)
    request_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_identifier("run_id", self.run_id)
        _require_identifier("thread_id", self.thread_id)
        _require_identifier("generation_id", self.generation_id)
        if self.solution_id is not None:
            _require_identifier("solution_id", self.solution_id)
        if self.solution_release_id is not None:
            _require_identifier("solution_release_id", self.solution_release_id)
        if self.agent_id is not None:
            _require_identifier("agent_id", self.agent_id)
        if self.root_run_id is not None:
            _require_identifier("root_run_id", self.root_run_id)
        if self.parent_run_id is not None:
            _require_identifier("parent_run_id", self.parent_run_id)
        object.__setattr__(
            self,
            "approved_capability_ids",
            frozenset(self.approved_capability_ids),
        )
        if self.request_id is not None:
            _require_identifier("request_id", self.request_id)

    @classmethod
    def create(
        cls,
        *,
        mode: RunMode,
        principal: Principal,
        thread_id: str | None = None,
        run_id: str | None = None,
        generation_id: str = STARTUP_GENERATION_ID,
        lifecycle: RunLifecycle = "interactive",
        solution_id: str | None = None,
        solution_release_id: str | None = None,
        agent_id: str | None = None,
        skill_id: str | None = None,
        root_run_id: str | None = None,
        parent_run_id: str | None = None,
        approved_capability_ids: frozenset[str] = frozenset(),
        request_id: str | None = None,
    ) -> "RuntimeContext":
        resolved_run_id = run_id or f"run_{uuid4().hex}"
        return cls(
            run_id=resolved_run_id,
            thread_id=thread_id or f"thr_{uuid4().hex}",
            principal=principal,
            mode=mode,
            lifecycle=lifecycle,
            generation_id=generation_id,
            solution_id=solution_id,
            solution_release_id=solution_release_id,
            agent_id=agent_id,
            skill_id=skill_id,
            root_run_id=root_run_id or resolved_run_id,
            parent_run_id=parent_run_id,
            approved_capability_ids=approved_capability_ids,
            request_id=request_id,
        )
