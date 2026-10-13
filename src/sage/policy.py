"""Central capability authorization decisions."""

from sage.domain import CapabilityDescriptor, PolicyDecision, RuntimeContext
from sage.domain.capability import PolicyAction
from sage.errors import PolicyDeniedError

_ADMIN_ROLES = frozenset({"platform_admin", "capability_admin"})


class PolicyEnforcer:
    def evaluate(
        self,
        context: RuntimeContext,
        capability: CapabilityDescriptor,
        action: PolicyAction,
    ) -> PolicyDecision:
        if capability.risk == "read-only":
            return PolicyDecision(
                outcome="allow",
                code="read_only_allowed",
                reason="Read-only capabilities are available to authenticated users.",
            )

        is_admin = bool(context.principal.roles & _ADMIN_ROLES)
        if action == "discover":
            if is_admin:
                return PolicyDecision(
                    outcome="allow",
                    code="admin_discovery_allowed",
                    reason="Administrative roles may discover governed write capabilities.",
                )
            return PolicyDecision(
                outcome="deny",
                code="write_capability_hidden",
                reason="Write capabilities are not visible to this principal.",
            )

        if capability.risk == "write-low" and is_admin:
            return PolicyDecision(
                outcome="allow",
                code="low_risk_write_allowed",
                reason="Administrative roles may execute low-risk write capabilities.",
            )
        if capability.risk in {"write-high", "destructive"} and is_admin:
            if capability.capability_id in context.approved_capability_ids:
                return PolicyDecision(
                    outcome="allow",
                    code="approved_high_risk_execution",
                    reason="A verified runtime HumanTask approved this capability.",
                )
            return PolicyDecision(
                outcome="require-approval",
                code="approval_required",
                reason="High-risk capability execution requires explicit approval.",
            )
        return PolicyDecision(
            outcome="deny",
            code="write_capability_denied",
            reason="This principal cannot execute the requested write capability.",
        )

    def require(
        self,
        context: RuntimeContext,
        capability: CapabilityDescriptor,
        action: PolicyAction,
    ) -> PolicyDecision:
        decision = self.evaluate(context, capability, action)
        if not decision.allowed:
            raise PolicyDeniedError(f"{decision.code}: {decision.reason}")
        return decision
