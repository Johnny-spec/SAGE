import pytest

from sage.domain import CapabilityDescriptor, Principal, RuntimeContext
from sage.errors import PolicyDeniedError
from sage.policy import PolicyEnforcer


def _capability(risk: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id="skill:test",
        name="test",
        kind="skill",
        version="1.0.0",
        description="Test capability",
        risk=risk,  # type: ignore[arg-type]
    )


def _context(*roles: str) -> RuntimeContext:
    return RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_default",
            principal_id="usr_test",
            roles=frozenset(roles),
            authentication_method="local",
        ),
    )


def test_read_only_capability_is_visible_and_executable() -> None:
    policy = PolicyEnforcer()
    context = _context("user")
    capability = _capability("read-only")

    assert policy.evaluate(context, capability, "discover").allowed is True
    assert policy.evaluate(context, capability, "execute").allowed is True


def test_write_capability_is_hidden_from_regular_user() -> None:
    decision = PolicyEnforcer().evaluate(
        _context("user"),
        _capability("write-low"),
        "discover",
    )

    assert decision.outcome == "deny"
    assert decision.code == "write_capability_hidden"


def test_capability_admin_can_execute_low_risk_write() -> None:
    decision = PolicyEnforcer().evaluate(
        _context("capability_admin"),
        _capability("write-low"),
        "execute",
    )

    assert decision.outcome == "allow"


def test_high_risk_execution_requires_approval() -> None:
    policy = PolicyEnforcer()
    context = _context("platform_admin")
    capability = _capability("write-high")

    decision = policy.evaluate(context, capability, "execute")

    assert decision.outcome == "require-approval"
    with pytest.raises(PolicyDeniedError, match="approval_required"):
        policy.require(context, capability, "execute")


def test_high_risk_execution_allows_only_verified_capability_context() -> None:
    policy = PolicyEnforcer()
    capability = _capability("write-high")
    approved = RuntimeContext.create(
        mode="direct",
        principal=_context("platform_admin").principal,
        parent_run_id="run_parent",
        approved_capability_ids=frozenset({"skill:test"}),
    )

    decision = policy.require(approved, capability, "execute")

    assert decision.outcome == "allow"
    assert decision.code == "approved_high_risk_execution"
