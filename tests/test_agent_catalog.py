import pytest

from sage.agent_catalog import AgentCatalog
from sage.agent_definitions import default_diagnostic_agent
from sage.domain import AgentDefinition, DelegationDefinition
from sage.errors import TargetNotFoundError


def test_agent_catalog_resolves_default_and_named_definition() -> None:
    definition = default_diagnostic_agent()
    catalog = AgentCatalog((definition,), default_agent_id=definition.agent_id)

    assert catalog.get() is definition
    assert catalog.get("diagnostic-agent") is definition
    assert list(catalog) == [definition]


def test_agent_catalog_rejects_unknown_default() -> None:
    with pytest.raises(ValueError, match="default Agent is not registered"):
        AgentCatalog((default_diagnostic_agent(),), default_agent_id="missing")


def test_agent_catalog_hides_unknown_agent_as_not_found() -> None:
    catalog = AgentCatalog(
        (default_diagnostic_agent(),),
        default_agent_id="diagnostic-agent",
    )

    with pytest.raises(TargetNotFoundError, match="Agent not found"):
        catalog.get("missing")


def _agent(agent_id: str, target_agent_id: str | None = None) -> AgentDefinition:
    delegations = (
        DelegationDefinition(
            alias=f"delegate-{target_agent_id}",
            target_agent_id=target_agent_id,
            description="Delegate work.",
        ),
    ) if target_agent_id is not None else ()
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id,
        version="1.0.0",
        instructions="Complete assigned work.",
        capability_ids=(),
        delegations=delegations,
    )


def test_agent_catalog_rejects_missing_delegation_target() -> None:
    with pytest.raises(ValueError, match="missing Agents"):
        AgentCatalog((_agent("coordinator", "missing"),), default_agent_id="coordinator")


def test_agent_catalog_rejects_delegation_cycle() -> None:
    with pytest.raises(ValueError, match="contains a cycle"):
        AgentCatalog(
            (_agent("first", "second"), _agent("second", "first")),
            default_agent_id="first",
        )
