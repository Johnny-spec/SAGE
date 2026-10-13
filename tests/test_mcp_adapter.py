import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sage.adapters import AdapterRegistry, McpAdapter
from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError
from sage.executor import SkillExecutor
from sage.gateways import HttpMcpGatewayClient, McpGatewayError, McpToolCall
from sage.mcp_gateway_app import (
    McpServerRegistration,
    McpServerRegistry,
    create_mcp_gateway_app,
)
from sage.models import SkillDefinition, SkillManifest
from sage.settings import AgentModelSettings


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[McpToolCall] = []

    async def invoke(self, call: McpToolCall) -> dict[str, Any]:
        self.calls.append(call)
        return {"answer": f"found:{call.arguments['query']}"}


class StaleGateway:
    async def invoke(self, call: McpToolCall) -> dict[str, Any]:
        raise McpGatewayError("MCP discovery is older than the configured freshness window")


class FakeUpstream:
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"answer": f"{tool_name}:{arguments['query']}"}


def _definition(tmp_path: Path) -> SkillDefinition:
    manifest = SkillManifest.model_validate(
        {
            "apiVersion": "sage.dev/v1alpha1",
            "kind": "SkillBinding",
            "metadata": {"name": "docs-search", "version": "1.0.0"},
            "spec": {
                "execution": {
                    "type": "mcp",
                    "serverAlias": "learn",
                    "toolName": "search",
                    "schemaDigest": "sha256:test",
                },
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        }
    )
    return SkillDefinition(
        name="docs-search",
        description="Search documentation",
        version="1.0.0",
        package_root=tmp_path,
        manifest=manifest,
    )


def test_mcp_adapter_invokes_bound_gateway_tool(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    executor = SkillExecutor(adapter_registry=AdapterRegistry({"mcp": McpAdapter(gateway)}))
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        generation_id="gen_mcp",
    )

    result = asyncio.run(
        executor.execute(_definition(tmp_path), {"query": "SAGE"}, context=context)
    )

    assert result == {"answer": "found:SAGE"}
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call.server_alias == "learn"
    assert call.tool_name == "search"
    assert call.expected_schema_digest == "sha256:test"
    assert call.context is context


def test_mcp_adapter_preserves_safe_governance_failure_reason(tmp_path: Path) -> None:
    executor = SkillExecutor(adapter_registry=AdapterRegistry({"mcp": McpAdapter(StaleGateway())}))
    context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())

    with pytest.raises(ExecutionError, match="discovery is older"):
        asyncio.run(
            executor.execute(
                _definition(tmp_path),
                {"query": "SAGE"},
                context=context,
            )
        )


def test_mcp_binding_requires_server_alias_and_tool_name() -> None:
    with pytest.raises(ValidationError, match="serverAlias and toolName"):
        SkillManifest.model_validate(
            {
                "apiVersion": "sage.dev/v1alpha1",
                "kind": "SkillBinding",
                "metadata": {"name": "invalid", "version": "1.0.0"},
                "spec": {
                    "execution": {"type": "mcp"},
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                },
            }
        )


def test_app_executes_registered_mcp_skill_through_gateway(tmp_path: Path) -> None:
    package = tmp_path / "docs-search"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: docs-search\ndescription: Search documentation.\n---\n",
        encoding="utf-8",
    )
    (package / "skill.yaml").write_text(
        """apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata:
  name: docs-search
  version: 1.0.0
spec:
  execution:
    type: mcp
    serverAlias: learn
    toolName: search
    schemaDigest: sha256:test
  inputSchema:
    type: object
    properties:
      query: {type: string}
    required: [query]
    additionalProperties: false
  outputSchema:
    type: object
    properties:
      answer: {type: string}
    required: [answer]
    additionalProperties: false
""",
        encoding="utf-8",
    )
    gateway = RecordingGateway()
    app = create_app(
        tmp_path,
        agent_model=DemoAgentModel(),
        agent_settings=AgentModelSettings(),
        mcp_gateway=gateway,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "message": "search docs",
                "skill": "docs-search",
                "arguments": {"query": "MCP"},
            },
        )

    assert response.status_code == 200
    assert response.json()["result"] == {"answer": "found:MCP"}
    assert gateway.calls[0].server_alias == "learn"


def test_http_gateway_client_calls_independent_gateway_app() -> None:
    async def scenario() -> dict[str, Any]:
        gateway_app = create_mcp_gateway_app(
            McpServerRegistry(
                (
                    McpServerRegistration(
                        alias="learn",
                        tool_schema_digests={"search": "sha256:test"},
                        client=FakeUpstream(),
                    ),
                )
            )
        )
        client = HttpMcpGatewayClient(
            "http://mcp-gateway",
            transport=httpx.ASGITransport(app=gateway_app),
        )
        try:
            return await client.invoke(
                McpToolCall(
                    server_alias="learn",
                    tool_name="search",
                    arguments={"query": "MCP"},
                    context=RuntimeContext.create(
                        mode="direct",
                        principal=Principal.anonymous(),
                    ),
                    expected_schema_digest="sha256:test",
                )
            )
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == {"answer": "search:MCP"}


def test_gateway_rejects_mcp_schema_drift() -> None:
    async def scenario() -> None:
        gateway_app = create_mcp_gateway_app(
            McpServerRegistry(
                (
                    McpServerRegistration(
                        alias="learn",
                        tool_schema_digests={"search": "sha256:new"},
                        client=FakeUpstream(),
                    ),
                )
            )
        )
        client = HttpMcpGatewayClient(
            "http://mcp-gateway",
            transport=httpx.ASGITransport(app=gateway_app),
        )
        try:
            with pytest.raises(McpGatewayError, match="request failed"):
                await client.invoke(
                    McpToolCall(
                        server_alias="learn",
                        tool_name="search",
                        arguments={"query": "MCP"},
                        context=RuntimeContext.create(
                            mode="direct",
                            principal=Principal.anonymous(),
                        ),
                        expected_schema_digest="sha256:old",
                    )
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())
