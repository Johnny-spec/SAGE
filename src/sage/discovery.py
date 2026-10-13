"""Client-facing discovery projections for policy-scoped runtime capabilities."""

from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from sage.agent_catalog import AgentCatalog
from sage.capabilities import CapabilityView
from sage.control_models import GenerationRecord, SolutionReleaseRecord
from sage.digests import canonical_digest
from sage.domain import AgentDefinition

ClientExecutionProfile = Literal["direct", "interactive", "durable"]
CapabilityKind = Literal["skill", "mcp-tool", "agent"]
CapabilityRisk = Literal["read-only", "write-low", "write-high", "destructive"]

_RISK_ORDER: dict[CapabilityRisk, int] = {
    "read-only": 0,
    "write-low": 1,
    "write-high": 2,
    "destructive": 3,
}


class CapabilityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: Literal["invoke", "message-stream", "start-operation", "durable-run"]
    profile: ClientExecutionProfile
    method: Literal["POST"] = "POST"
    path: str
    request_input_field: Literal["input", "arguments", "message", "payload"]
    streaming: bool = False
    asynchronous: bool = False
    idempotency_key: Literal["optional", "required", "not-supported"] = "optional"
    cancellation_supported: bool = False
    runtime_available: bool = True
    restart_recoverable: bool = False


class CapabilityDescriptorView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    capability_id: str
    kind: CapabilityKind
    name: str
    description: str
    version: str
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    risk: CapabilityRisk
    execution_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_output_bytes: int | None = Field(default=None, ge=1)
    approval_required: bool = False
    agent_usable: bool = False
    accessible_capability_ids: tuple[str, ...] = ()
    operations: tuple[CapabilityOperation, ...] = ()
    links: dict[str, str]


def accessible_agent_capability_ids(
    definition: AgentDefinition,
    capabilities: CapabilityView,
) -> tuple[str, ...]:
    accessible_ids = {descriptor.capability_id for descriptor in capabilities.descriptors}
    return tuple(
        capability_id
        for capability_id in definition.capability_ids
        if capability_id in accessible_ids
    )


def agent_is_discoverable(
    definition: AgentDefinition,
    capabilities: CapabilityView,
) -> bool:
    return not definition.capability_ids or bool(
        accessible_agent_capability_ids(definition, capabilities)
    )


def build_capability_descriptors(
    *,
    generation: GenerationRecord,
    release: SolutionReleaseRecord | None,
    capabilities: CapabilityView,
    agents: AgentCatalog,
    durable_available: bool,
) -> tuple[CapabilityDescriptorView, ...]:
    profiles = _client_profiles(release)
    solution_id = release.solution_id if release is not None else None
    release_id = release.release_id if release is not None else None
    descriptors: list[CapabilityDescriptorView] = []

    for definition in capabilities.skills:
        policy = definition.manifest.spec.policy
        execution = definition.manifest.spec.execution
        content_digest = generation.skill_digests.get(definition.name)
        if content_digest is None:
            continue
        operations: list[CapabilityOperation] = []
        if execution.type in {"workflow", "model"}:
            if "durable" in profiles:
                operations.append(
                    CapabilityOperation(
                        operation_id="durable-run",
                        profile="durable",
                        path="/v1/durable-runs",
                        request_input_field="payload",
                        asynchronous=True,
                        idempotency_key="required",
                        cancellation_supported=True,
                        runtime_available=durable_available,
                        restart_recoverable=True,
                    )
                )
        elif "direct" in profiles:
            idempotency = (
                "required" if policy.risk in {"write-high", "destructive"} else "optional"
            )
            operations.extend(
                (
                    CapabilityOperation(
                        operation_id="invoke",
                        profile="direct",
                        path="/v1/invocations",
                        request_input_field="input",
                        asynchronous=policy.risk in {"write-high", "destructive"},
                        idempotency_key=idempotency,
                        cancellation_supported=policy.risk in {"write-high", "destructive"},
                        runtime_available=(
                            durable_available
                            if policy.risk in {"write-high", "destructive"}
                            else True
                        ),
                    ),
                    CapabilityOperation(
                        operation_id="message-stream",
                        profile="direct",
                        path="/v1/messages/stream",
                        request_input_field="arguments",
                        streaming=True,
                        idempotency_key="not-supported",
                    ),
                )
            )
            if policy.risk == "read-only":
                operations.append(
                    CapabilityOperation(
                        operation_id="start-operation",
                        profile="direct",
                        path="/v1/operations",
                        request_input_field="input",
                        asynchronous=True,
                        idempotency_key="required",
                        cancellation_supported=True,
                    )
                )
        descriptors.append(
            CapabilityDescriptorView(
                capability_id=f"skill:{definition.name}",
                kind="skill",
                name=definition.name,
                description=definition.description,
                version=definition.version,
                content_digest=content_digest,
                generation_id=generation.generation_id,
                solution_id=solution_id,
                solution_release_id=release_id,
                risk=policy.risk,
                execution_type=execution.type,
                input_schema=definition.manifest.spec.input_schema,
                output_schema=definition.manifest.spec.output_schema,
                timeout_seconds=policy.timeout_seconds,
                max_output_bytes=policy.max_output_bytes,
                approval_required=policy.risk in {"write-high", "destructive"},
                agent_usable="interactive" in profiles,
                operations=tuple(operations),
                links=_links(solution_id),
            )
        )

    for tool in capabilities.tools:
        operations = ()
        if "direct" in profiles:
            high_risk = tool.risk in {"write-high", "destructive"}
            operations = (
                CapabilityOperation(
                    operation_id="invoke",
                    profile="direct",
                    path="/v1/invocations",
                    request_input_field="input",
                    asynchronous=high_risk,
                    idempotency_key="required" if high_risk else "optional",
                    cancellation_supported=high_risk,
                    runtime_available=durable_available if high_risk else True,
                    restart_recoverable=high_risk,
                ),
            )
        descriptors.append(
            CapabilityDescriptorView(
                capability_id=tool.tool_id,
                kind="mcp-tool",
                name=tool.binding.tool_name,
                description=tool.description,
                version=tool.version,
                content_digest=tool.content_digest,
                generation_id=generation.generation_id,
                solution_id=solution_id,
                solution_release_id=release_id,
                risk=tool.risk,
                execution_type="mcp",
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                timeout_seconds=tool.timeout_seconds,
                max_output_bytes=tool.max_output_bytes,
                approval_required=tool.risk in {"write-high", "destructive"},
                agent_usable="interactive" in profiles,
                operations=operations,
                links=_links(solution_id),
            )
        )

    risk_by_id = {
        descriptor.capability_id: descriptor.risk for descriptor in capabilities.descriptors
    }
    for definition in agents:
        accessible = accessible_agent_capability_ids(definition, capabilities)
        if not agent_is_discoverable(definition, capabilities):
            continue
        source = generation.agent_definitions.get(definition.agent_id)
        if source is None:
            continue
        risk = _highest_risk(risk_by_id.get(capability_id) for capability_id in accessible)
        operations: list[CapabilityOperation] = []
        if "interactive" in profiles:
            operations.append(
                CapabilityOperation(
                    operation_id="message-stream",
                    profile="interactive",
                    path="/v1/messages/stream",
                    request_input_field="message",
                    streaming=True,
                    idempotency_key="not-supported",
                )
            )
            if risk == "read-only":
                operations.append(
                    CapabilityOperation(
                        operation_id="start-operation",
                        profile="interactive",
                        path="/v1/operations",
                        request_input_field="input",
                        asynchronous=True,
                        idempotency_key="required",
                        cancellation_supported=True,
                    )
                )
        if "durable" in profiles:
            operations.append(
                CapabilityOperation(
                    operation_id="durable-run",
                    profile="durable",
                    path="/v1/durable-runs",
                    request_input_field="payload",
                    asynchronous=True,
                    idempotency_key="required",
                    cancellation_supported=True,
                    runtime_available=durable_available,
                    restart_recoverable=True,
                )
            )
        descriptors.append(
            CapabilityDescriptorView(
                capability_id=f"agent:{definition.agent_id}",
                kind="agent",
                name=definition.name,
                description=f"Published Agent {definition.name}",
                version=definition.version,
                content_digest=canonical_digest(source),
                generation_id=generation.generation_id,
                solution_id=solution_id,
                solution_release_id=release_id,
                risk=risk,
                execution_type="agent",
                input_schema=_agent_input_schema(),
                output_schema={"type": "object"},
                approval_required=risk in {"write-high", "destructive"},
                accessible_capability_ids=accessible,
                operations=tuple(operations),
                links=_links(solution_id),
            )
        )

    return tuple(sorted(descriptors, key=lambda item: (item.kind, item.capability_id)))


def _client_profiles(
    release: SolutionReleaseRecord | None,
) -> frozenset[ClientExecutionProfile]:
    if release is None:
        return frozenset({"direct", "interactive"})
    return frozenset(
        profile
        for profile in release.supported_profiles
        if profile in {"direct", "interactive", "durable"}
    )


def _highest_risk(values: Iterable[CapabilityRisk | None]) -> CapabilityRisk:
    present = [value for value in values if value is not None]
    return max(present, key=_RISK_ORDER.__getitem__) if present else "read-only"


def _links(solution_id: str | None) -> dict[str, str]:
    suffix = f"?solution_id={quote(solution_id, safe='')}" if solution_id else ""
    return {
        "events": "/v1/runs/{run_id}/events/stream",
        "result": "/v1/runs/{run_id}/result",
        "feedback": "/v1/runs/{run_id}/feedback",
        "discovery": f"/v1/capabilities{suffix}",
    }


def _agent_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["message"],
        "additionalProperties": False,
        "properties": {
            "message": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "thread_id": {"type": "string", "maxLength": 128},
            "artifact_ids": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string"},
            },
        },
    }