"""Stable internal contract between SAGE and an independent MCP Gateway."""

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from sage.domain import RuntimeContext


class McpGatewayError(Exception):
    """Raised when the independent MCP Gateway cannot complete a tool call."""


@dataclass(frozen=True, slots=True)
class McpToolCall:
    server_alias: str
    tool_name: str
    arguments: dict[str, Any]
    context: RuntimeContext
    expected_schema_digest: str | None = None
    expected_server_config_digest: str | None = None


class McpGatewayClient(Protocol):
    async def invoke(self, call: McpToolCall) -> dict[str, Any]: ...


class HttpMcpGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        internal_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP Gateway base URL must be an absolute HTTP(S) URL")
        headers = {"Authorization": f"Bearer {internal_token}"} if internal_token else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
            transport=transport,
        )

    async def invoke(self, call: McpToolCall) -> dict[str, Any]:
        payload = {
            "server_alias": call.server_alias,
            "tool_name": call.tool_name,
            "arguments": call.arguments,
            "expected_schema_digest": call.expected_schema_digest,
            "expected_server_config_digest": call.expected_server_config_digest,
            "run": {
                "run_id": call.context.run_id,
                "thread_id": call.context.thread_id,
                "workspace_id": call.context.principal.workspace_id,
                "principal_id": call.context.principal.principal_id,
                "roles": sorted(call.context.principal.roles),
                "generation_id": call.context.generation_id,
            },
        }
        try:
            response = await self._client.post("/internal/v1/tools/call", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise McpGatewayError("MCP Gateway request failed") from exc
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise McpGatewayError("MCP Gateway returned an invalid result envelope")
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
