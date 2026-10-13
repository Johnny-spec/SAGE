"""Core domain contracts shared by SAGE application services."""

from sage.domain.agent import AgentDefinition, DelegationDefinition, SubagentDefinition
from sage.domain.capability import CapabilityDescriptor, PolicyDecision
from sage.domain.context import Principal, RuntimeContext

__all__ = [
    "AgentDefinition",
    "CapabilityDescriptor",
    "DelegationDefinition",
    "PolicyDecision",
    "Principal",
    "RuntimeContext",
    "SubagentDefinition",
]
