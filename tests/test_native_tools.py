import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError, PolicyDeniedError
from sage.gateways import McpGatewayError, McpToolCall
from sage.tools import McpToolBinding, McpToolDefinition, ToolExecutor


@pytest.mark.parametrize("risk", ["read-only", "write-high"])
@pytest.mark.parametrize("durable", [False, True])
def test_agent_model_receives_and_invokes_bound_native_tool(risk: str, durable: bool) -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from sage.agent_model import DemoAgentModel
    from sage.agent_runtime import DeepAgentRuntime
    from sage.app import DEFAULT_SKILLS_ROOT
    from sage.capabilities import CapabilityViewResolver
    from sage.domain import AgentDefinition
    from sage.executor import SkillExecutor
    from sage.policy import PolicyEnforcer
    from sage.registry import SkillRegistry

    observed = []

    class NativeModel(DemoAgentModel):
        def _next_message(self, messages):
            native_names = [name for name in self.bound_tool_names if name.startswith("mcp_")]
            assert len(native_names) == 1
            system_text = "\n".join(str(item.content) for item in messages if item.type == "system")
            assert "contracts/search" in system_text
            observed.append(native_names[0])
            if any(isinstance(item, ToolMessage) and item.name == native_names[0]
                   for item in messages):
                return AIMessage(content="Retrieved result: found")
            return self._tool_call(native_names[0], {"arguments": {"query": "board resolution"}})

    definition = tool_definition(risk=risk)
    gateway = RecordingGateway()
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    runtime = DeepAgentRuntime(
        registry, SkillExecutor(),
        AgentDefinition(
            agent_id="search", name="search", version="1.0.0",
            instructions="Use contracts/search for legal retrieval.",
            capability_ids=(definition.tool_id,),
        ),
        model=NativeModel(),
        capabilities=CapabilityViewResolver(registry, PolicyEnforcer(), [definition]),
        tool_executor=ToolExecutor(
            [definition], generation_id="gen_native", gateway=gateway,
            allowed_tool_ids=[definition.tool_id],
        ),
    )
    runtime_context = replace(
        context(), mode="agent", lifecycle="durable" if durable else "interactive",
    )
    invocation = (
        runtime.durable_turn("Find board resolution clauses", runtime_context)
        if durable else runtime.run("Find board resolution clauses", runtime_context, ())
    )
    if risk == "write-high":
        with pytest.raises(ExecutionError, match="no accessible bound capabilities"):
            asyncio.run(invocation)
        assert not observed
        assert not gateway.calls
        return
    result = asyncio.run(invocation)
    assert observed
    assert len(gateway.calls) == 1
    assert (result["answer"] if durable else result.answer) == "Retrieved result: found"


class RecordingGateway:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[McpToolCall] = []
        self.result = result if result is not None else {"answer": "found"}

    async def invoke(self, call: McpToolCall) -> dict[str, Any]:
        self.calls.append(call)
        return self.result


def tool_definition(**overrides: Any) -> McpToolDefinition:
    values = {
        "version": "1.0.0",
        "description": "Query clauses",
        "binding": McpToolBinding(
            server_id="mcp_contracts",
            server_alias="contracts",
            tool_name="search",
            schema_digest="sha256:" + "a" * 64,
            server_config_digest="sha256:" + "b" * 64,
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    }
    values.update(overrides)
    return McpToolDefinition.model_validate(values)


def context(generation_id: str = "gen_native") -> RuntimeContext:
    return RuntimeContext.create(
        mode="direct", principal=Principal.anonymous(), generation_id=generation_id
    )


def test_native_tool_executes_without_skill_package() -> None:
    definition = tool_definition()
    gateway = RecordingGateway()
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=gateway,
        allowed_tool_ids=[definition.tool_id],
    )
    runtime = context()
    assert asyncio.run(executor.execute(
        definition.tool_id, {"query": "liability"}, context=runtime
    )) == {"answer": "found"}
    assert gateway.calls[0].context is runtime
    assert gateway.calls[0].expected_schema_digest == definition.binding.schema_digest
    assert gateway.calls[0].expected_server_config_digest == definition.binding.server_config_digest


@pytest.mark.parametrize("failure", ["generation", "allowlist", "policy", "input", "disabled"])
def test_native_tool_rejects_before_gateway(failure: str) -> None:
    definition = tool_definition(
        risk="write-low" if failure == "policy" else "read-only",
        enabled=failure != "disabled",
    )
    gateway = RecordingGateway()
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=gateway,
        allowed_tool_ids=[] if failure == "allowlist" else [definition.tool_id],
    )
    with pytest.raises((ExecutionError, PolicyDeniedError)):
        asyncio.run(executor.execute(
            definition.tool_id,
            {} if failure == "input" else {"query": "liability"},
            context=context("other" if failure == "generation" else "gen_native"),
        ))
    assert gateway.calls == []


@pytest.mark.parametrize("result,overrides,message", [
    ({"wrong": True}, {}, "Invalid Tool result"),
    ({"answer": "large"}, {"max_output_bytes": 1}, "too large"),
    ({"answer": float("nan")}, {}, "not valid JSON"),
])
def test_native_tool_validates_results(
    result: dict[str, Any], overrides: dict[str, Any], message: str,
) -> None:
    definition = tool_definition(**overrides)
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=RecordingGateway(result),
        allowed_tool_ids=[definition.tool_id],
    )
    with pytest.raises(ExecutionError, match=message):
        asyncio.run(executor.execute(definition.tool_id, {"query": "x"}, context=context()))


def test_native_tool_binding_identity_and_schema_validation() -> None:
    definition = tool_definition()
    renamed = definition.binding.model_copy(update={"server_alias": "renamed"})
    other = definition.binding.model_copy(update={"server_id": "mcp_other"})
    assert renamed.tool_id == definition.tool_id
    assert other.tool_id != definition.tool_id
    assert definition.binding.model_copy(update={"tool_name": "a:b"}).tool_id.endswith("a%3Ab")
    with pytest.raises(ValidationError, match="Invalid tool schema"):
        tool_definition(input_schema={"type": "invalid"})


def test_executor_copies_mutable_schema_into_snapshot() -> None:
    definition = tool_definition()
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=RecordingGateway(),
        allowed_tool_ids=[definition.tool_id],
    )
    definition.input_schema.clear()
    with pytest.raises(ExecutionError, match="Invalid Tool arguments"):
        asyncio.run(executor.execute(definition.tool_id, {}, context=context()))


def test_high_risk_tool_requires_verified_capability_approval() -> None:
    definition = tool_definition(risk="write-high")
    gateway = RecordingGateway()
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=gateway,
        allowed_tool_ids=[definition.tool_id],
    )
    runtime = replace(context(), principal=Principal(
        workspace_id="ws_test", principal_id="admin", roles=frozenset({"platform_admin"}),
    ))
    for approved_ids in (frozenset(), frozenset({"tool:mcp:other:search"})):
        with pytest.raises(PolicyDeniedError, match="approval_required"):
            asyncio.run(executor.execute(
                definition.tool_id, {"query": "x"},
                context=replace(runtime, approved_capability_ids=approved_ids),
            ))
    assert gateway.calls == []
    approved = replace(runtime, approved_capability_ids=frozenset({definition.tool_id}))
    assert asyncio.run(executor.execute(
        definition.tool_id, {"query": "x"}, context=approved,
    )) == {"answer": "found"}


def test_native_tool_timeout_cancels_gateway() -> None:
    cancelled: list[bool] = []

    class BlockingGateway:
        async def invoke(self, call: McpToolCall) -> dict[str, Any]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
            return {}

    definition = tool_definition(timeout_seconds=0.01)
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=BlockingGateway(),
        allowed_tool_ids=[definition.tool_id],
    )
    with pytest.raises(ExecutionError, match="timed out"):
        asyncio.run(executor.execute(definition.tool_id, {"query": "x"}, context=context()))
    assert cancelled == [True]


def test_native_tool_gateway_failure_is_not_success() -> None:
    class FailedGateway:
        async def invoke(self, call: McpToolCall) -> dict[str, Any]:
            raise McpGatewayError("MCP Tool schema changed")

    definition = tool_definition()
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=FailedGateway(),
        allowed_tool_ids=[definition.tool_id],
    )
    with pytest.raises(ExecutionError, match="schema changed"):
        asyncio.run(executor.execute(definition.tool_id, {"query": "x"}, context=context()))


def test_native_tool_without_output_schema_accepts_content_blocks() -> None:
    definition = tool_definition(output_schema=None)
    content = {"content": [{"type": "text", "text": "Clause found"}]}
    executor = ToolExecutor(
        [definition], generation_id="gen_native", gateway=RecordingGateway(content),
        allowed_tool_ids=[definition.tool_id],
    )
    assert asyncio.run(executor.execute(
        definition.tool_id, {"query": "x"}, context=context(),
    )) == content


def test_native_snapshot_rejects_duplicate_or_missing_tools() -> None:
    definition = tool_definition()
    with pytest.raises(ValueError, match="Duplicate tool"):
        ToolExecutor(
            [definition, definition], generation_id="gen_native", gateway=RecordingGateway(),
            allowed_tool_ids=[definition.tool_id],
        )
    with pytest.raises(ValueError, match="must be present"):
        ToolExecutor(
            [], generation_id="gen_native", gateway=RecordingGateway(),
            allowed_tool_ids=[definition.tool_id],
        )