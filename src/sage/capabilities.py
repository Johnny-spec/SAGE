"""Policy-filtered runtime views over registered capabilities."""

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from types import MappingProxyType

from sage.domain import CapabilityDescriptor, RuntimeContext
from sage.models import SkillDefinition
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry
from sage.tools import McpToolDefinition


def skill_descriptor(definition: SkillDefinition) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"skill:{definition.name}",
        name=definition.name,
        kind="skill",
        version=definition.version,
        description=definition.description,
        risk=definition.manifest.spec.policy.risk,
    )


@dataclass(frozen=True, slots=True)
class SkillCapability:
    descriptor: CapabilityDescriptor
    definition: SkillDefinition


class CapabilityView:
    def __init__(
        self,
        generation_id: str,
        capabilities: Collection[SkillCapability],
        tools: Collection[McpToolDefinition] = (),
    ) -> None:
        by_name: dict[str, SkillCapability] = {}
        by_id: dict[str, SkillCapability] = {}
        for capability in capabilities:
            name = capability.definition.name
            capability_id = capability.descriptor.capability_id
            if name in by_name or capability_id in by_id:
                raise ValueError(f"duplicate capability: {capability_id}")
            by_name[name] = capability
            by_id[capability_id] = capability
        self.generation_id = generation_id
        self._by_name = MappingProxyType(by_name)
        self._by_id = MappingProxyType(by_id)
        self._tools = MappingProxyType({tool.tool_id: tool.model_copy(deep=True) for tool in tools})

    @property
    def tools(self) -> tuple[McpToolDefinition, ...]:
        return tuple(self._tools.values())

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(capability.definition for capability in self._by_name.values())

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return (*tuple(capability.descriptor for capability in self._by_name.values()),
            *tuple(tool.descriptor for tool in self._tools.values()))

    def get_skill(self, name: str) -> SkillDefinition | None:
        capability = self._by_name.get(name)
        return capability.definition if capability is not None else None

    def contains(self, capability_id: str) -> bool:
        return capability_id in self._by_id or capability_id in self._tools

    def __iter__(self) -> Iterator[SkillCapability]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name) + len(self._tools)


class CapabilityViewResolver:
    def __init__(
        self, registry: SkillRegistry, policy: PolicyEnforcer,
        tools: Collection[McpToolDefinition] = (),
        enabled_execution_types: Collection[str] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._tools = tuple(tool.model_copy(deep=True) for tool in tools)
        self._enabled_execution_types = (
            frozenset(enabled_execution_types)
            if enabled_execution_types is not None
            else None
        )

    def resolve(
        self,
        context: RuntimeContext,
        allowed_capability_ids: Collection[str] | None = None,
    ) -> CapabilityView:
        selected_ids = set(allowed_capability_ids) if allowed_capability_ids is not None else None
        capabilities: list[SkillCapability] = []
        for definition in self._registry:
            if not definition.manifest.spec.policy.execution_enabled:
                continue
            if (
                self._enabled_execution_types is not None
                and definition.manifest.spec.execution.type not in self._enabled_execution_types
            ):
                continue
            descriptor = skill_descriptor(definition)
            if selected_ids is not None and descriptor.capability_id not in selected_ids:
                continue
            decision = self._policy.evaluate(context, descriptor, "discover")
            if decision.allowed:
                capabilities.append(SkillCapability(descriptor=descriptor, definition=definition))
        tools = [tool for tool in self._tools if tool.enabled
             and (selected_ids is None or tool.tool_id in selected_ids)
             and self._policy.evaluate(context, tool.descriptor, "discover").allowed]
        return CapabilityView(context.generation_id, capabilities, tools)
