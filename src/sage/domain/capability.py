"""Capability and policy decision contracts."""

from dataclasses import dataclass
from typing import Literal

CapabilityKind = Literal["skill", "mcp-tool", "http-tool"]
CapabilityRisk = Literal["read-only", "write-low", "write-high", "destructive"]
PolicyAction = Literal["discover", "execute"]
PolicyOutcome = Literal["allow", "deny", "require-approval"]


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    name: str
    kind: CapabilityKind
    version: str
    description: str
    risk: CapabilityRisk

    def __post_init__(self) -> None:
        for field_name in ("capability_id", "name", "version", "description"):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    code: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"
