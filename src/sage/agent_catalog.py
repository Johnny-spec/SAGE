"""Immutable catalog of published Agent definitions."""

from collections.abc import Iterable, Iterator
from types import MappingProxyType

from sage.domain import AgentDefinition
from sage.errors import TargetNotFoundError


class AgentCatalog:
    def __init__(
        self,
        definitions: Iterable[AgentDefinition],
        *,
        default_agent_id: str | None,
    ) -> None:
        by_id: dict[str, AgentDefinition] = {}
        for definition in definitions:
            if definition.agent_id in by_id:
                raise ValueError(f"duplicate Agent id: {definition.agent_id}")
            by_id[definition.agent_id] = definition
        if default_agent_id is not None and default_agent_id not in by_id:
            raise ValueError(f"default Agent is not registered: {default_agent_id}")
        known_ids = set(by_id)
        for definition in by_id.values():
            missing = {
                delegation.target_agent_id
                for delegation in definition.delegations
                if delegation.target_agent_id not in known_ids
            }
            if missing:
                raise ValueError(
                    f"Agent {definition.agent_id} delegates to missing Agents: "
                    f"{', '.join(sorted(missing))}"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(agent_id: str) -> None:
            if agent_id in visiting:
                raise ValueError("Agent delegation graph contains a cycle")
            if agent_id in visited:
                return
            visiting.add(agent_id)
            for delegation in by_id[agent_id].delegations:
                visit(delegation.target_agent_id)
            visiting.remove(agent_id)
            visited.add(agent_id)

        for agent_id in by_id:
            visit(agent_id)
        self._definitions = MappingProxyType(by_id)
        self.default_agent_id = default_agent_id

    def get(self, agent_id: str | None = None) -> AgentDefinition:
        resolved_id = agent_id or self.default_agent_id
        if resolved_id is None:
            raise TargetNotFoundError("Agent not found: this Solution has no entry Agent")
        definition = self._definitions.get(resolved_id)
        if definition is None:
            raise TargetNotFoundError(f"Agent not found: {resolved_id}")
        return definition

    def __iter__(self) -> Iterator[AgentDefinition]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)
