"""Governed MCP configuration, discovery, import, and runtime invocation."""

import asyncio
import ipaddress
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from sage.control_models import (
    ControlPlaneState,
    McpServerRecord,
    McpToolRecord,
    ToolRegistrationRecord,
)
from sage.control_settings import ControlPlaneSettings
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.gateways import McpGatewayError, McpToolCall
from sage.skill_packages import SkillPackageManager
from sage.tools import McpToolBinding, McpToolDefinition


class McpControlError(ValueError):
    """Raised when an MCP configuration or lifecycle operation is unsafe."""


class McpConnector(Protocol):
    async def test(self, server: McpServerRecord) -> dict[str, str]: ...

    async def discover(self, server: McpServerRecord) -> tuple[McpToolRecord, ...]: ...

    async def invoke(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]: ...


class OfficialMcpConnector:
    def __init__(self, settings: ControlPlaneSettings) -> None:
        self._settings = settings

    async def test(self, server: McpServerRecord) -> dict[str, str]:
        async with asyncio.timeout(30):
            async with self._session(server) as session:
                result = await session.initialize()
                return {
                    "name": result.serverInfo.name,
                    "version": result.serverInfo.version,
                    "protocol_version": result.protocolVersion,
                }

    async def discover(self, server: McpServerRecord) -> tuple[McpToolRecord, ...]:
        tools: list[McpToolRecord] = []
        cursor: str | None = None
        async with asyncio.timeout(60):
            async with self._session(server) as session:
                await session.initialize()
                while True:
                    result = await session.list_tools(cursor=cursor)
                    for tool in result.tools:
                        input_schema = dict(tool.inputSchema or {"type": "object"})
                        output_schema = dict(tool.outputSchema or {"type": "object"})
                        tools.append(
                            McpToolRecord(
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=input_schema,
                                output_schema=output_schema,
                                schema_digest=canonical_digest(
                                    {"input": input_schema, "output": output_schema}
                                ),
                            )
                        )
                    cursor = result.nextCursor
                    if not cursor:
                        break
        return tuple(tools)

    async def invoke(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.invoke_raw(server, tool_name, arguments)
        if result.get("isError"):
            raise McpControlError(f"MCP Tool returned an error: {tool_name}")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        return {"content": result.get("content", [])}

    async def invoke_raw(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        async with asyncio.timeout(60):
            async with self._session(server) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
        return result.model_dump(mode="json", by_alias=True)

    @asynccontextmanager
    async def _session(self, server: McpServerRecord) -> AsyncIterator[ClientSession]:
        secret = self._secret(server)
        if server.transport == "streamable-http":
            url = self._validated_url(server.url)
            headers = {"Authorization": f"Bearer {secret}"} if secret else {}
            async with (
                httpx.AsyncClient(
                    headers=headers,
                    follow_redirects=False,
                    timeout=30,
                ) as client,
                streamable_http_client(url, http_client=client) as streams,
            ):
                read_stream, write_stream, _ = streams
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    yield session
            return

        preset = self._settings.mcp_stdio_presets.get(server.command_preset or "")
        if preset is None:
            raise McpControlError("MCP stdio command preset is not approved")
        allowed_environment = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
        environment = {name: os.environ[name] for name in allowed_environment if name in os.environ}
        environment.update(preset.env)
        if secret and preset.secret_env_name:
            environment[preset.secret_env_name] = secret
        parameters = StdioServerParameters(
            command=preset.command,
            args=list(preset.args),
            cwd=preset.cwd,
            env=environment,
        )
        with open(os.devnull, "w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as streams:
                read_stream, write_stream = streams
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    yield session

    def _validated_url(self, value: str | None) -> str:
        parsed = urlsplit(value or "")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise McpControlError("MCP URL must be an absolute credential-free HTTPS URL")
        hostname = parsed.hostname.casefold()
        insecure_local = parsed.scheme == "http" and self._settings.allow_insecure_local_mcp
        if parsed.scheme != "https" and not insecure_local:
            raise McpControlError("MCP URL must use HTTPS unless loopback DEV mode is enabled")
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        if self._settings.mcp_enforce_allowed_hosts:
            allowed = hostname in self._settings.mcp_allowed_hosts or (
                f"{hostname}:{port}" in self._settings.mcp_allowed_hosts
            )
            if not allowed:
                raise McpControlError("MCP host is not in SAGE_MCP_ALLOWED_HOSTS")
        try:
            addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise McpControlError("MCP host DNS resolution failed") from exc
        if not addresses:
            raise McpControlError("MCP host DNS resolution returned no addresses")
        if insecure_local and not all(
            ipaddress.ip_address(item[4][0]).is_loopback for item in addresses
        ):
            raise McpControlError("Insecure HTTP MCP is restricted to loopback addresses")
        return value or ""

    def _secret(self, server: McpServerRecord) -> str | None:
        if server.secret_ref:
            value = os.getenv(server.secret_ref)
            if not value:
                raise McpControlError(f"MCP secret reference is unavailable: {server.secret_ref}")
            return value
        if server.dev_secret:
            if not self._settings.allow_plaintext_dev_secrets:
                raise McpControlError("Plaintext MCP secrets require explicit DEV mode")
            return server.dev_secret
        return None


class McpServerManager:
    def __init__(
        self,
        store: LocalControlStore,
        settings: ControlPlaneSettings,
        connector: McpConnector,
        skills: SkillPackageManager,
    ) -> None:
        self._store = store
        self._settings = settings
        self._connector = connector
        self._skills = skills
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def create(self, values: dict[str, Any]) -> McpServerRecord:
        alias = str(values.get("alias", "")).strip()
        if not alias:
            raise McpControlError("MCP server alias is required")
        if values.get("dev_secret") and not self._settings.allow_plaintext_dev_secrets:
            raise McpControlError("Plaintext MCP secrets require explicit DEV mode")
        if values.get("dev_secret") and values.get("secret_ref"):
            raise McpControlError("Choose either a Secret reference or a DEV plaintext secret")
        if any(item.alias == alias for item in self.list_servers()):
            raise McpControlError(f"MCP server alias already exists: {alias}")
        record = self._record(f"mcp_{uuid4().hex}", values)

        def save(state: object) -> None:
            state.mcp_servers[record.server_id] = record  # type: ignore[attr-defined]

        self._store.update(save)
        return record

    def update(self, server_id: str, values: dict[str, Any]) -> McpServerRecord:
        existing = self.get(server_id)
        if values.get("dev_secret") and not self._settings.allow_plaintext_dev_secrets:
            raise McpControlError("Plaintext MCP secrets require explicit DEV mode")
        requested_secret_ref = values.get("secret_ref")
        requested_dev_secret = values.get("dev_secret")
        if requested_secret_ref and requested_dev_secret:
            raise McpControlError("Choose either a Secret reference or a DEV plaintext secret")
        if requested_secret_ref:
            secret_ref = requested_secret_ref
            dev_secret = None
        elif requested_dev_secret:
            secret_ref = None
            dev_secret = requested_dev_secret
        else:
            secret_ref = existing.secret_ref
            dev_secret = existing.dev_secret
        alias = str(values.get("alias", existing.alias)).strip()
        if any(item.alias == alias and item.server_id != server_id for item in self.list_servers()):
            raise McpControlError(f"MCP server alias already exists: {alias}")
        merged = {
            "alias": alias,
            "transport": values.get("transport", existing.transport),
            "url": values.get("url", existing.url),
            "command_preset": values.get("command_preset", existing.command_preset),
            "secret_ref": secret_ref,
            "dev_secret": dev_secret,
        }
        record = self._record(server_id, merged, created_at=existing.created_at)

        def apply(state: object) -> None:
            state.mcp_servers[server_id] = record  # type: ignore[attr-defined]

        self._store.update(apply)
        return record

    async def test(self, server_id: str) -> tuple[McpServerRecord, dict[str, str]]:
        server = self.get(server_id)
        if server.status == "disabled":
            raise McpControlError("MCP server is disabled")
        try:
            details = await self._connector.test(server)
        except Exception as exc:
            self._set_failure(server_id, str(exc))
            raise McpControlError(f"MCP connection test failed: {exc}") from exc
        now = datetime.now(UTC)

        def apply(state: object) -> McpServerRecord:
            current = state.mcp_servers[server_id]  # type: ignore[attr-defined]
            if current.config_digest != server.config_digest or current.status == "disabled":
                raise McpControlError("MCP configuration changed during connection test")
            current.status = "reachable"
            current.tested_at = now
            current.tested_config_digest = current.config_digest
            current.last_error = None
            current.updated_at = now
            return current.model_copy(deep=True)

        return self._store.update(apply), details

    async def discover(self, server_id: str) -> McpServerRecord:
        server = self.get(server_id)
        if server.status == "disabled":
            raise McpControlError("MCP server is disabled")
        self._require_fresh_test(server)
        try:
            tools = await self._connector.discover(server)
        except Exception as exc:
            self._set_failure(server_id, str(exc))
            raise McpControlError(f"MCP discovery failed: {exc}") from exc
        now = datetime.now(UTC)

        def apply(state: object) -> McpServerRecord:
            current = state.mcp_servers[server_id]  # type: ignore[attr-defined]
            if current.config_digest != server.config_digest or current.status == "disabled":
                raise McpControlError("MCP configuration changed during discovery")
            current.status = "discovered"
            current.tools = tools
            current.discovered_at = now
            current.discovered_config_digest = current.config_digest
            current.last_error = None
            current.updated_at = now
            return current.model_copy(deep=True)

        return self._store.update(apply)

    def import_tool(
        self,
        server_id: str,
        tool_name: str,
        *,
        skill_name: str,
        version: str,
        description: str,
        instructions: str,
        risk: str,
    ) -> object:
        server = self.get(server_id)
        self.require_fresh_discovery(server)
        tool = next((item for item in server.tools if item.name == tool_name), None)
        if tool is None:
            raise McpControlError(f"MCP Tool not found: {tool_name}")
        return self._skills.register_generated_mcp(
            server_id=server.server_id,
            server_alias=server.alias,
            server_config_digest=server.config_digest,
            tool_name=tool.name,
            schema_digest=tool.schema_digest,
            skill_name=skill_name,
            version=version,
            description=description,
            instructions=instructions,
            risk=risk,
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
        )

    def register_tool(
        self,
        server_id: str,
        tool_name: str,
        *,
        version: str,
        description: str,
        risk: str = "read-only",
    ) -> ToolRegistrationRecord:
        def register(state: ControlPlaneState) -> ToolRegistrationRecord:
            server = state.mcp_servers.get(server_id)
            if server is None:
                raise KeyError(server_id)
            self.require_fresh_discovery(server)
            discovered = next((item for item in server.tools if item.name == tool_name), None)
            if discovered is None:
                raise McpControlError(f"MCP Tool not found: {tool_name}")
            definition = McpToolDefinition.model_validate(
                {
                    "version": version,
                    "description": description,
                    "risk": risk,
                    "binding": McpToolBinding(
                        server_id=server.server_id,
                        server_alias=server.alias,
                        tool_name=discovered.name,
                        schema_digest=discovered.schema_digest,
                        server_config_digest=server.config_digest,
                    ),
                    "input_schema": discovered.input_schema,
                    "output_schema": discovered.output_schema,
                }
            )
            for existing in state.tool_registrations.values():
                if (
                    existing.tool_id == definition.tool_id
                    and existing.definition.version == version
                ):
                    if existing.content_digest != definition.content_digest:
                        raise McpControlError("Tool version already exists with different content")
                    return existing.model_copy(deep=True)
            record = ToolRegistrationRecord(
                registration_id=f"toolreg_{uuid4().hex}",
                tool_id=definition.tool_id,
                definition=definition,
                content_digest=definition.content_digest,
            )
            state.tool_registrations[record.registration_id] = record
            return record.model_copy(deep=True)

        return self._store.update(register)

    def list_tool_registrations(self) -> list[ToolRegistrationRecord]:
        return sorted(
            self._store.snapshot().tool_registrations.values(),
            key=lambda item: (item.tool_id, item.definition.version),
        )

    async def ensure_fresh_discovery(self, server_id: str) -> McpServerRecord:
        server = self.get(server_id)
        try:
            self.require_fresh_discovery(server)
            return server
        except McpControlError:
            if self._settings.environment != "development" or server.status in {
                "disabled",
                "failed",
            }:
                raise
        lock = self._refresh_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            server = self.get(server_id)
            try:
                self.require_fresh_discovery(server)
                return server
            except McpControlError:
                if server.status in {"disabled", "failed"}:
                    raise
            async with asyncio.timeout(90):
                await self.test(server_id)
                return await self.discover(server_id)

    def require_fresh_discovery(self, server: McpServerRecord) -> None:
        self._require_fresh(server.discovered_at, "MCP discovery")
        if server.discovered_config_digest != server.config_digest:
            raise McpControlError("MCP discovery is stale because configuration changed")
        if server.status != "discovered":
            raise McpControlError("MCP server has not completed discovery")

    def get(self, server_id: str) -> McpServerRecord:
        record = self._store.snapshot().mcp_servers.get(server_id)
        if record is None:
            raise KeyError(server_id)
        return record

    def get_by_alias(self, alias: str) -> McpServerRecord | None:
        return next((item for item in self.list_servers() if item.alias == alias), None)

    def list_servers(self) -> list[McpServerRecord]:
        return sorted(
            self._store.snapshot().mcp_servers.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def _record(
        self,
        server_id: str,
        values: dict[str, Any],
        *,
        created_at: datetime | None = None,
    ) -> McpServerRecord:
        transport = values.get("transport")
        url = values.get("url")
        preset = values.get("command_preset")
        if transport == "streamable-http" and not url:
            raise McpControlError("Streamable HTTP MCP requires a URL")
        if transport == "stdio" and preset not in self._settings.mcp_stdio_presets:
            raise McpControlError("MCP stdio command preset is not approved")
        if transport not in {"streamable-http", "stdio"}:
            raise McpControlError("Unsupported MCP transport")
        digest = canonical_digest(
            {
                "alias": values.get("alias"),
                "transport": transport,
                "url": url,
                "command_preset": preset,
                "secret_ref": values.get("secret_ref"),
                "dev_secret_digest": (
                    canonical_digest(values["dev_secret"]) if values.get("dev_secret") else None
                ),
            }
        )
        return McpServerRecord(
            server_id=server_id,
            alias=str(values.get("alias", "")).strip(),
            transport=transport,
            url=url,
            command_preset=preset,
            secret_ref=values.get("secret_ref"),
            dev_secret=values.get("dev_secret"),
            config_digest=digest,
            created_at=created_at or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def _require_fresh_test(self, server: McpServerRecord) -> None:
        self._require_fresh(server.tested_at, "MCP connection test")
        if server.tested_config_digest != server.config_digest:
            raise McpControlError("MCP test is stale because configuration changed")

    def _require_fresh(self, timestamp: datetime | None, label: str) -> None:
        if timestamp is None:
            raise McpControlError(f"{label} has not run")
        age = datetime.now(UTC) - timestamp
        if age > timedelta(seconds=self._settings.mcp_health_ttl_seconds):
            raise McpControlError(f"{label} is older than the configured freshness window")

    def _set_failure(self, server_id: str, message: str) -> None:
        def apply(state: object) -> None:
            current = state.mcp_servers[server_id]  # type: ignore[attr-defined]
            current.status = "failed"
            current.last_error = message[:1000]
            current.updated_at = datetime.now(UTC)

        self._store.update(apply)


class ManagedMcpGatewayClient:
    def __init__(self, servers: McpServerManager, connector: McpConnector) -> None:
        self._servers = servers
        self._connector = connector

    async def invoke(self, call: McpToolCall) -> dict[str, Any]:
        server = self._servers.get_by_alias(call.server_alias)
        if server is None:
            raise McpGatewayError("MCP server alias not found")
        try:
            server = await self._servers.ensure_fresh_discovery(server.server_id)
        except McpControlError as exc:
            raise McpGatewayError(str(exc)) from exc
        tool = next((item for item in server.tools if item.name == call.tool_name), None)
        if tool is None:
            raise McpGatewayError("MCP Tool not found")
        if (
            call.expected_server_config_digest
            and call.expected_server_config_digest != server.config_digest
        ):
            raise McpGatewayError("MCP server configuration changed")
        if call.expected_schema_digest and call.expected_schema_digest != tool.schema_digest:
            raise McpGatewayError("MCP Tool schema changed")
        try:
            return await self._connector.invoke(server, call.tool_name, call.arguments)
        except Exception as exc:
            raise McpGatewayError("MCP upstream call failed") from exc
