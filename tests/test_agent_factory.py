import pytest

from sage.agent_catalog import AgentCatalog
from sage.agent_definitions import default_diagnostic_agent
from sage.agent_factory import AgentRuntimeFactory
from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT
from sage.capabilities import CapabilityViewResolver
from sage.errors import TargetNotFoundError
from sage.executor import SkillExecutor
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry


def test_agent_runtime_factory_caches_published_version() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    policy = PolicyEnforcer()
    capabilities = CapabilityViewResolver(registry, policy)
    definition = default_diagnostic_agent()
    factory = AgentRuntimeFactory(
        registry,
        SkillExecutor(policy_enforcer=policy),
        AgentCatalog((definition,), default_agent_id=definition.agent_id),
        capabilities,
        model=DemoAgentModel(),
        model_name="test-model",
    )

    first = factory.get()
    second = factory.get("diagnostic-agent")

    assert first is second
    assert first.definition is definition


def test_agent_runtime_factory_rejects_unpublished_agent() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    policy = PolicyEnforcer()
    definition = default_diagnostic_agent()
    factory = AgentRuntimeFactory(
        registry,
        SkillExecutor(policy_enforcer=policy),
        AgentCatalog((definition,), default_agent_id=definition.agent_id),
        CapabilityViewResolver(registry, policy),
        model=DemoAgentModel(),
    )

    with pytest.raises(TargetNotFoundError, match="Agent not found"):
        factory.get("draft-agent")
