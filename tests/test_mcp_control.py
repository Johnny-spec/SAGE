import asyncio
from pathlib import Path
from typing import Any

import pytest

from sage.control_models import GenerationRecord, McpServerRecord, McpToolRecord
from sage.control_settings import ControlPlaneSettings
from sage.control_store import LocalControlStore
from sage.mcp_control import McpControlError, McpServerManager
from sage.skill_packages import SkillPackageManager


class FakeConnector:
    async def test(self, server: McpServerRecord) -> dict[str, str]:
        return {"name": server.alias, "version": "1", "protocol_version": "test"}

    async def discover(self, server: McpServerRecord) -> tuple[McpToolRecord, ...]:
        return (
            McpToolRecord(
                name="search",
                description="Search docs",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                output_schema={"type": "object"},
                schema_digest="sha256:tool",
            ),
        )

    async def invoke(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {"server": server.alias, "tool": tool_name, "arguments": arguments}


def _manager(tmp_path: Path, *, allow_plaintext: bool = False) -> McpServerManager:
    store = LocalControlStore(
        tmp_path / "data",
        GenerationRecord(
            generation_id="gen_startup",
            content_digest="sha256:startup",
            skills_root=str(tmp_path),
            skill_versions={},
            agent_definitions={},
        ),
    )
    settings = ControlPlaneSettings(
        data_root=tmp_path / "data",
        allow_plaintext_dev_secrets=allow_plaintext,
        mcp_allowed_hosts=frozenset({"mcp.example"}),
    )
    return McpServerManager(
        store,
        settings,
        FakeConnector(),
        SkillPackageManager(store),
    )


def test_mcp_test_discover_and_import_tool(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    server = manager.create(
        {
            "alias": "learn",
            "transport": "streamable-http",
            "url": "https://mcp.example/mcp",
        }
    )

    tested, _ = asyncio.run(manager.test(server.server_id))
    discovered = asyncio.run(manager.discover(server.server_id))
    package = manager.import_tool(
        server.server_id,
        "search",
        skill_name="learn-search",
        version="1.0.0",
        description="Search official docs.",
        instructions="Use this Skill when current official documentation is required.",
        risk="read-only",
    )

    assert tested.status == "reachable"
    assert discovered.status == "discovered"
    assert package.status == "valid"
    assert package.execution_type == "mcp"
    assert package.executable is True


def test_plaintext_mcp_secret_requires_dev_mode(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(McpControlError, match="DEV mode"):
        manager.create(
            {
                "alias": "secret",
                "transport": "streamable-http",
                "url": "https://mcp.example/mcp",
                "dev_secret": "do-not-store",
            }
        )


def test_mcp_configuration_change_invalidates_health(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    server = manager.create(
        {
            "alias": "learn",
            "transport": "streamable-http",
            "url": "https://mcp.example/one",
        }
    )
    asyncio.run(manager.test(server.server_id))
    changed = manager.update(server.server_id, {"url": "https://mcp.example/two"})

    with pytest.raises(McpControlError, match="has not run"):
        asyncio.run(manager.discover(changed.server_id))
