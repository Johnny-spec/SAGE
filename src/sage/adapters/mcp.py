"""Skill adapter that delegates MCP tools to the independent Gateway."""

from collections.abc import Mapping
from typing import Any

from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.gateways import McpGatewayClient, McpGatewayError, McpToolCall
from sage.models import SkillDefinition


class McpAdapter:
    def __init__(self, gateway: McpGatewayClient) -> None:
        self._gateway = gateway

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        binding = definition.manifest.spec.execution
        if binding.server_alias is None or binding.tool_name is None:
            raise ExecutionError(f"Incomplete MCP binding: {definition.name}")
        try:
            return await self._gateway.invoke(
                McpToolCall(
                    server_alias=binding.server_alias,
                    tool_name=binding.tool_name,
                    arguments=dict(arguments),
                    context=context,
                    expected_schema_digest=binding.schema_digest,
                    expected_server_config_digest=binding.server_config_digest,
                )
            )
        except McpGatewayError as exc:
            raise ExecutionError(
                f"MCP Gateway invocation failed: {binding.server_alias}/{binding.tool_name}: {exc}"
            ) from exc
