import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_native_tools import context

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_models import GenerationRecord, McpServerRecord, McpToolRecord
from sage.control_settings import ControlPlaneSettings, StdioPreset
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.errors import ExecutionError
from sage.evaluations import EvaluationManager
from sage.mcp_control import (
    ManagedMcpGatewayClient,
    McpControlError,
    McpServerManager,
    OfficialMcpConnector,
)
from sage.skill_packages import SkillPackageManager
from sage.solutions import SolutionManager
from sage.tools import ToolExecutor


class NativeConnector:
    async def test(self, server: McpServerRecord) -> dict[str, str]:
        return {"name": server.alias}

    async def discover(self, server: McpServerRecord) -> tuple[McpToolRecord, ...]:
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}
        return (
            McpToolRecord(
                name="clauses/search",
                input_schema=input_schema,
                output_schema=output_schema,
                schema_digest=canonical_digest({"input": input_schema, "output": output_schema}),
            ),
        )

    async def invoke(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {"clauses": [], "tool": tool_name}


def manager_and_store(tmp_path: Path) -> tuple[McpServerManager, LocalControlStore]:
    store = LocalControlStore(
        tmp_path,
        GenerationRecord(
            generation_id="gen_native",
            content_digest="sha256:unchanged",
            skills_root=str(tmp_path / "skills"),
            skill_versions={},
            agent_definitions={},
        ),
    )
    manager = McpServerManager(
        store,
        ControlPlaneSettings(data_root=tmp_path),
        NativeConnector(),
        SkillPackageManager(store),
    )
    return manager, store


def discover(manager: McpServerManager) -> str:
    server = manager.create(
        {
            "alias": "contracts",
            "transport": "streamable-http",
            "url": "https://example.test/mcp",
        }
    )
    asyncio.run(manager.test(server.server_id))
    asyncio.run(manager.discover(server.server_id))
    return server.server_id


def test_native_registration_is_persisted_draft_not_skill_or_generation(tmp_path: Path) -> None:
    manager, store = manager_and_store(tmp_path)
    server_id = discover(manager)
    previous = store.snapshot().generations
    record = manager.register_tool(
        server_id,
        "clauses/search",
        version="1.0.0",
        description="Query clauses",
    )
    repeated = manager.register_tool(
        server_id,
        "clauses/search",
        version="1.0.0",
        description="Query clauses",
    )
    reopened = LocalControlStore(tmp_path).snapshot()
    assert record == repeated
    assert record.status == "draft"
    assert reopened.tool_registrations[record.registration_id] == record
    assert reopened.skills == {}
    assert reopened.generations == previous
    assert not list((tmp_path / "artifacts").rglob("SKILL.md"))
    with pytest.raises(McpControlError, match="different content"):
        manager.register_tool(
            server_id,
            "clauses/search",
            version="1.0.0",
            description="Changed",
        )


def test_native_tool_publishes_without_skill_and_is_agent_visible(tmp_path: Path) -> None:
    app = create_app(
        Path(__file__).parent / "fixtures" / "skills",
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path),
        mcp_connector=NativeConnector(),
    )
    with TestClient(app) as client:
        initial_skills = app.state.control_store.snapshot().skills
        server = client.post(
            "/v1/admin/mcp/servers",
            json={
                "alias": "contracts",
                "transport": "streamable-http",
                "url": "https://example.test/mcp",
            },
        ).json()
        base = f"/v1/admin/mcp/servers/{server['server_id']}"
        assert client.post(f"{base}/test").is_success
        assert client.post(f"{base}/discover").is_success
        registration = client.post(
            f"{base}/tool-registrations",
            json={
                "tool_name": "clauses/search",
                "version": "1.0.0",
                "description": "Query clauses",
            },
        ).json()
        tool_id = registration["tool_id"]
        draft = client.post(
            "/v1/admin/agents",
            json={
                "agent_id": "native-agent",
                "name": "Native",
                "version": "1.0.0",
                "instructions": "Query clauses using the native tool.",
                "capability_ids": [tool_id],
            },
        ).json()
        assert (
            client.post(f"/v1/admin/agents/{draft['draft_id']}/validate").json()["status"]
            == "valid"
        )
        candidate = client.post(
            "/v1/admin/candidates",
            json={
                "title": "Native tool and agent",
                "agent_draft_ids": [draft["draft_id"]],
                "tool_registration_ids": [registration["registration_id"]],
                "default_agent_id": "native-agent",
            },
        ).json()
        published = client.post(f"/v1/admin/candidates/{candidate['candidate_id']}/publish")
        assert published.status_code == 200, published.text
        generation_id = published.json()["generation_id"]
        snapshot = app.state.runtime_manager.active
        run_context = replace(context(), generation_id=generation_id)
        view = snapshot.capabilities.resolve(run_context, [tool_id])
        assert len(view.tools) == 1
        assert not view.skills
        runtime = snapshot.agent_runtimes.get("native-agent")
        native = runtime._create_native_tool(view.tools[0])
        assert native.name.startswith("mcp_")
        assert (
            native.args_schema["properties"]["arguments"]
            == registration["definition"]["input_schema"]
        )
        assert (
            asyncio.run(snapshot.tool_executor.execute(tool_id, {}, context=run_context))["clauses"]
            == []
        )
        invocation = {"capability_id": tool_id, "input": {}, "idempotency_key": "native-first"}
        response = client.post("/v1/invocations", json=invocation)
        assert response.status_code == 200, response.text
        assert response.json()["output"]["tool"] == "clauses/search"
        assert client.post("/v1/invocations", json=invocation).json() == response.json()
        changed = client.post("/v1/invocations", json={**invocation, "input": {"changed": True}})
        assert changed.status_code == 409
        assert (
            client.post(
                "/v1/invocations", json={"capability_id": "tool:mcp:missing:tool"}
            ).status_code
            == 404
        )
        persisted = app.state.control_store.snapshot()
        assert persisted.skills == initial_skills
        assert persisted.tool_registrations[registration["registration_id"]].status == "published"
        assert (
            tool_id
            in LocalControlStore(tmp_path).snapshot().generations[generation_id].tool_definitions
        )
        assert (
            app.state.catalog_manager._evaluate(
                persisted.candidates[candidate["candidate_id"]], persisted
            )[0]
            == []
        )


def test_native_registration_and_gateway_reject_config_drift(tmp_path: Path) -> None:
    manager, _ = manager_and_store(tmp_path)
    server_id = discover(manager)
    record = manager.register_tool(
        server_id,
        "clauses/search",
        version="1.0.0",
        description="Query clauses",
    )
    executor = ToolExecutor(
        [record.definition],
        generation_id="gen_native",
        allowed_tool_ids=[record.tool_id],
        gateway=ManagedMcpGatewayClient(manager, NativeConnector()),
    )
    assert asyncio.run(executor.execute(record.tool_id, {}, context=context()))["clauses"] == []
    manager.update(server_id, {"url": "https://example.test/changed"})
    with pytest.raises(McpControlError):
        manager.register_tool(
            server_id,
            "clauses/search",
            version="2.0.0",
            description="Query clauses",
        )
    asyncio.run(manager.test(server_id))
    asyncio.run(manager.discover(server_id))
    with pytest.raises(ExecutionError, match="configuration changed"):
        asyncio.run(executor.execute(record.tool_id, {}, context=context()))


def test_v10_migration_preserves_historical_digests(tmp_path: Path) -> None:
    _, store = manager_and_store(tmp_path)
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="test-solution",
        title="Test",
        description="",
        owner="test-team",
        entry_agent_id=None,
        supported_profiles=("direct",),
    )
    from sage.control_models import SolutionComponentLock

    release = solutions.create_release(
        "test-solution",
        version="1.0.0",
        components=(
            SolutionComponentLock(
                kind="evaluation-suite",
                component_id="test-suite",
                version="1.0.0",
                content_digest="sha256:" + "c" * 64,
            ),
        ),
    )
    EvaluationManager(store).record(
        release.release_id,
        suite_id="test-suite",
        status="passed",
        metrics={"success": 1.0},
        summary="Historical report",
        actor_fingerprint="test-evaluator",
    )
    raw = store.snapshot().model_dump(mode="json")
    raw["schema_version"] = 10
    raw.pop("tool_registrations")
    state_file = tmp_path / "control-plane.json"
    state_file.write_text(json.dumps(raw), encoding="utf-8")
    migrated = LocalControlStore(tmp_path).snapshot()
    assert migrated.schema_version == 13
    assert migrated.tool_registrations == {}
    for collection in ("generations", "solutions", "solution_releases", "evaluations"):
        assert migrated.model_dump(mode="json")[collection] == raw[collection]


def test_native_registration_admin_api_requires_existing_auth(tmp_path: Path) -> None:
    app = create_app(
        Path(__file__).parent / "fixtures" / "skills",
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            admin_auth_enabled=True,
            admin_token="native-test-token",
        ),
        mcp_connector=NativeConnector(),
    )
    headers = {"Authorization": "Bearer native-test-token"}
    with TestClient(app) as client:
        assert client.get("/v1/admin/tools").status_code == 401
        server = client.post(
            "/v1/admin/mcp/servers",
            headers=headers,
            json={
                "alias": "contracts",
                "transport": "streamable-http",
                "url": "https://example.test/mcp",
            },
        )
        assert server.status_code == 201
        server_id = server.json()["server_id"]
        endpoint = f"/v1/admin/mcp/servers/{server_id}/tool-registrations"
        payload = {
            "tool_name": "clauses/search",
            "version": "1.0.0",
            "description": "Query clauses",
        }
        assert client.post(endpoint, json=payload).status_code == 401
        assert client.post(endpoint, headers=headers, json=payload).status_code == 422
        assert client.post(f"/v1/admin/mcp/servers/{server_id}/test", headers=headers).is_success
        discovered = client.post(f"/v1/admin/mcp/servers/{server_id}/discover", headers=headers)
        assert discovered.is_success
        response = client.post(endpoint, headers=headers, json=payload)
        assert response.status_code == 201
        assert response.json()["status"] == "draft"
        assert response.json()["tool_id"].endswith(":clauses%2Fsearch")
        assert client.get("/v1/admin/tools", headers=headers).json() == [response.json()]
        state = app.state.control_store.snapshot()
        assert state.skills == {}
        assert all(
            not key.startswith("tool:")
            for key in state.generations[state.active_generation_id].skill_versions
        )


def test_native_tool_real_mcp_stdio_protocol(tmp_path: Path) -> None:
    _, store = manager_and_store(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    settings = ControlPlaneSettings(
        data_root=tmp_path,
        mcp_stdio_presets={
            "demo-native": StdioPreset(
                command=sys.executable,
                args=(
                    "-c",
                    "from scripts.demo_mcp_server import create_demo_server; "
                    "create_demo_server('127.0.0.1', 0).run(transport='stdio')",
                ),
                cwd=str(project_root),
            ),
        },
    )
    connector = OfficialMcpConnector(settings)
    manager = McpServerManager(store, settings, connector, SkillPackageManager(store))
    server = manager.create(
        {
            "alias": "native-demo",
            "transport": "stdio",
            "command_preset": "demo-native",
        }
    )

    async def scenario() -> dict[str, Any]:
        await manager.test(server.server_id)
        await manager.discover(server.server_id)
        registration = manager.register_tool(
            server.server_id,
            "calculate_total",
            version="1.0.0",
            description="Calculate total",
        )
        executor = ToolExecutor(
            [registration.definition],
            generation_id="gen_native",
            gateway=ManagedMcpGatewayClient(manager, connector),
            allowed_tool_ids=[registration.tool_id],
        )
        return await executor.execute(
            registration.tool_id,
            {"unit_price": 10, "quantity": 5, "discount_percent": 10},
            context=context(),
        )

    result = asyncio.run(scenario())
    assert result["total"] == 45
    assert store.snapshot().skills == {}
