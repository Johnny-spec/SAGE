"""Independent MCP Gateway service boundary.

The transport-specific upstream clients are injected from deployment configuration. This
service owns alias resolution, schema snapshot enforcement, and the stable internal API used by
the main SAGE application.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class McpUpstreamClient(Protocol):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class McpServerRegistration:
    alias: str
    tool_schema_digests: Mapping[str, str]
    client: McpUpstreamClient

    def __post_init__(self) -> None:
        if not self.alias or not self.alias.strip():
            raise ValueError("MCP server alias must not be empty")
        object.__setattr__(
            self,
            "tool_schema_digests",
            MappingProxyType(dict(self.tool_schema_digests)),
        )


class McpServerRegistry:
    def __init__(self, registrations: Iterable[McpServerRegistration]) -> None:
        by_alias: dict[str, McpServerRegistration] = {}
        for registration in registrations:
            if registration.alias in by_alias:
                raise ValueError(f"duplicate MCP server alias: {registration.alias}")
            by_alias[registration.alias] = registration
        self._registrations = MappingProxyType(by_alias)

    def get(self, alias: str) -> McpServerRegistration | None:
        return self._registrations.get(alias)

    def __len__(self) -> int:
        return len(self._registrations)


class GatewayRunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    thread_id: str
    workspace_id: str
    principal_id: str
    roles: list[str] = Field(default_factory=list)
    generation_id: str


class GatewayToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_alias: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_schema_digest: str | None = None
    expected_server_config_digest: str | None = None
    run: GatewayRunContext


class GatewayToolResponse(BaseModel):
    result: dict[str, Any]


def create_mcp_gateway_app(registry: McpServerRegistry) -> FastAPI:
    gateway = FastAPI(title="SAGE MCP Gateway", version="0.1.0")

    @gateway.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @gateway.get("/health/ready")
    async def health_ready() -> dict[str, int | str]:
        return {"status": "ready", "servers": len(registry)}

    @gateway.post("/internal/v1/tools/call", response_model=GatewayToolResponse)
    async def call_tool(payload: GatewayToolRequest) -> GatewayToolResponse:
        registration = registry.get(payload.server_alias)
        if registration is None:
            raise HTTPException(status_code=404, detail="MCP server alias not found")
        actual_digest = registration.tool_schema_digests.get(payload.tool_name)
        if actual_digest is None:
            raise HTTPException(status_code=404, detail="MCP tool not found")
        if (
            payload.expected_schema_digest is not None
            and payload.expected_schema_digest != actual_digest
        ):
            raise HTTPException(status_code=409, detail="MCP tool schema changed")
        try:
            result = await registration.client.call_tool(payload.tool_name, payload.arguments)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="MCP upstream call failed") from exc
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="MCP upstream returned invalid output")
        return GatewayToolResponse(result=result)

    return gateway


app = create_mcp_gateway_app(McpServerRegistry(()))
