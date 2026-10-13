import pytest

from sage.agent_definitions import default_diagnostic_agent
from sage.domain import AgentDefinition, DelegationDefinition, SubagentDefinition


def test_default_agent_declares_tools_and_delegations() -> None:
    definition = default_diagnostic_agent()

    assert definition.agent_id == "diagnostic-agent"
    assert "skill:report-summary" in definition.capability_ids
    assert definition.exclusive_delegations == {
        "skill:iis-logs": "incident-analyst",
        "skill:netlog": "incident-analyst",
    }


def test_agent_rejects_duplicate_capability_bindings() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        AgentDefinition(
            agent_id="agent",
            name="agent",
            version="1.0.0",
            instructions="Run tools.",
            capability_ids=("skill:echo", "skill:echo"),
        )


def test_subagent_exclusive_capability_must_be_bound() -> None:
    with pytest.raises(ValueError, match="must also be bound"):
        SubagentDefinition(
            name="worker",
            description="Worker",
            instructions="Run tools.",
            capability_ids=("skill:echo",),
            exclusive_capability_ids=("skill:netlog",),
        )


def test_agent_supports_published_delegation_reference() -> None:
    definition = AgentDefinition(
        agent_id="coordinator",
        name="Coordinator",
        version="1.0.0",
        instructions="Delegate independent verification.",
        capability_ids=(),
        delegations=(
            DelegationDefinition(
                alias="reviewer",
                target_agent_id="review-agent",
                description="Independently verify the proposed answer.",
                max_calls=2,
            ),
        ),
    )

    assert definition.worker_names == ("reviewer",)
    assert definition.delegations[0].target_agent_id == "review-agent"


def test_agent_rejects_self_delegation() -> None:
    with pytest.raises(ValueError, match="cannot delegate to itself"):
        AgentDefinition(
            agent_id="coordinator",
            name="Coordinator",
            version="1.0.0",
            instructions="Delegate work.",
            capability_ids=(),
            delegations=(
                DelegationDefinition(
                    alias="self",
                    target_agent_id="coordinator",
                    description="Invalid delegation.",
                ),
            ),
        )
