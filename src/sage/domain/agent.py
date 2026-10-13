"""Declarative Agent and subagent definitions."""

from dataclasses import dataclass
from typing import Literal

MemoryPolicy = Literal["none", "thread", "thread-and-user"]


def _require_text(field_name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_unique(field_name: str, values: tuple[str, ...]) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    name: str
    description: str
    instructions: str
    capability_ids: tuple[str, ...]
    exclusive_capability_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("name", self.name)
        _require_text("description", self.description)
        _require_text("instructions", self.instructions)
        _require_unique("capability_ids", self.capability_ids)
        _require_unique("exclusive_capability_ids", self.exclusive_capability_ids)
        unknown = set(self.exclusive_capability_ids) - set(self.capability_ids)
        if unknown:
            raise ValueError(
                "exclusive capabilities must also be bound to the subagent: "
                f"{', '.join(sorted(unknown))}"
            )


@dataclass(frozen=True, slots=True)
class DelegationDefinition:
    alias: str
    target_agent_id: str
    description: str
    max_calls: int = 1

    def __post_init__(self) -> None:
        _require_text("alias", self.alias)
        _require_text("target_agent_id", self.target_agent_id)
        _require_text("description", self.description)
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    version: str
    instructions: str
    capability_ids: tuple[str, ...]
    subagents: tuple[SubagentDefinition, ...] = ()
    delegations: tuple[DelegationDefinition, ...] = ()
    memory_policy: MemoryPolicy = "thread"
    max_steps: int = 40
    max_model_calls: int = 100
    max_tool_calls: int = 100
    max_subagent_calls: int = 20

    def __post_init__(self) -> None:
        for field_name in ("agent_id", "name", "version", "instructions"):
            _require_text(field_name, getattr(self, field_name))
        _require_unique("capability_ids", self.capability_ids)
        subagent_names = tuple(subagent.name for subagent in self.subagents)
        _require_unique("subagent names", subagent_names)
        delegation_aliases = tuple(delegation.alias for delegation in self.delegations)
        _require_unique("delegation aliases", delegation_aliases)
        _require_unique("Agent worker aliases", subagent_names + delegation_aliases)
        for delegation in self.delegations:
            if delegation.target_agent_id == self.agent_id:
                raise ValueError("Agent cannot delegate to itself")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        for field_name in ("max_model_calls", "max_tool_calls", "max_subagent_calls"):
            if getattr(self, field_name) < 1:
                raise ValueError(f"{field_name} must be at least 1")
        available = set(self.capability_ids)
        for subagent in self.subagents:
            unknown = set(subagent.capability_ids) - available
            if unknown:
                raise ValueError(
                    f"subagent {subagent.name} references unbound capabilities: "
                    f"{', '.join(sorted(unknown))}"
                )

    @property
    def exclusive_delegations(self) -> dict[str, str]:
        return {
            capability_id: subagent.name
            for subagent in self.subagents
            for capability_id in subagent.exclusive_capability_ids
        }

    @property
    def worker_names(self) -> tuple[str, ...]:
        return tuple(subagent.name for subagent in self.subagents) + tuple(
            delegation.alias for delegation in self.delegations
        )
