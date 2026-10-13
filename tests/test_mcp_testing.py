import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_tool_registration import NativeConnector, discover, manager_and_store

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.audit import SqliteAuditStore
from sage.control_settings import ControlPlaneSettings
from sage.control_store import LocalControlStore
from sage.domain import Principal
from sage.human_tasks import SqliteHumanTaskStore
from sage.mcp_control import McpControlError
from sage.mcp_test_models import ToolRiskReviewInput, ToolTestInput
from sage.mcp_testing import McpToolTests
from sage.mcp_testing import TestInfrastructureUnavailable as InfrastructureUnavailable


def setup_tests(tmp_path: Path):
    manager, store = manager_and_store(tmp_path)
    server_id = discover(manager)
    server = manager.get(server_id)
    connector = NativeConnector()
    connector.invoke = AsyncMock(wraps=connector.invoke)
    backend = AsyncMock()
    service = McpToolTests(
        store,
        manager,
        connector,
        SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3")),
        SqliteAuditStore(str(tmp_path / "audit.sqlite3")),
        backend,
        "tests",
    )
    principal = Principal("ws_default", "tester", frozenset({"platform_admin"}))
    binding = {
        "tool_name": "clauses/search",
        "expected_server_config_digest": server.config_digest,
        "expected_schema_digest": server.tools[0].schema_digest,
    }
    return service, connector, backend, principal, server_id, binding


def test_unpublished_read_test_and_replay(tmp_path: Path):
    service, connector, _, principal, server_id, binding = setup_tests(tmp_path)
    initial = service.store.snapshot()

    async def scenario():
        payload = ToolTestInput(**binding, arguments={}, idempotency_key="first")
        with pytest.raises(PermissionError, match="Review"):
            await service.create(server_id, payload, principal)
        await service.review(server_id, ToolRiskReviewInput(**binding, risk="read-only"), principal)
        result = await service.create(server_id, payload, principal)
        assert result["status"] == "completed"
        assert result["result"]["structuredContent"]["tool"] == "clauses/search"
        assert "request" not in result
        assert await service.create(server_id, payload, principal) == result
        with pytest.raises(McpControlError, match="Idempotency"):
            await service.create(
                server_id, payload.model_copy(update={"arguments": {"new": 1}}), principal
            )
        connector.invoke.assert_awaited_once()
        return result

    result = asyncio.run(scenario())
    persisted = LocalControlStore(tmp_path).snapshot()
    assert persisted.mcp_tests[result["test_id"]].request is None
    assert persisted.skills == initial.skills
    assert persisted.tool_registrations == initial.tool_registrations
    assert persisted.generations == initial.generations


def test_approved_test_survives_restart_without_redispatch(tmp_path: Path):
    service, connector, backend, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(
            server_id, ToolRiskReviewInput(**binding, risk="write-high"), principal
        )
        payload = ToolTestInput(
            **binding, arguments={}, idempotency_key="write", confirm_write=True
        )
        pending = await service.create(server_id, payload, principal)
        assert pending["status"] == "awaiting-approval"
        connector.invoke.assert_not_awaited()
        with pytest.raises(PermissionError, match="approval"):
            await service.execute(pending["test_id"])
        service.store = LocalControlStore(tmp_path)
        await service.decide(
            pending["test_id"], principal, approve=True, reason="Reviewed exact call"
        )
        backend.signal.assert_awaited_once()
        connector.invoke.assert_not_awaited()
        result = await service.execute(pending["test_id"])
        assert result.status == "completed"
        assert (await service.execute(pending["test_id"])).status == "completed"
        connector.invoke.assert_awaited_once()
        with pytest.raises(ValueError, match="differently"):
            await service.decide(pending["test_id"], principal, approve=False, reason="Changed")

    asyncio.run(scenario())


def test_high_risk_unavailable_and_drift_block_dispatch(tmp_path: Path):
    service, connector, backend, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(
            server_id, ToolRiskReviewInput(**binding, risk="destructive"), principal
        )
        payload = ToolTestInput(
            **binding, arguments={}, idempotency_key="write", confirm_write=True
        )
        service.backend = None
        with pytest.raises(InfrastructureUnavailable):
            await service.create(server_id, payload, principal)
        assert not service.store.snapshot().mcp_tests
        service.backend = backend
        pending = await service.create(server_id, payload, principal)
        await service.decide(pending["test_id"], principal, approve=True, reason="Approved")
        service.servers.update(server_id, {"url": "https://example.test/changed"})
        result = await service.execute(pending["test_id"])
        assert result.status == "failed"
        assert result.result == {"error": "tool_binding_changed"}
        assert result.request is None
        assert await service.execute(pending["test_id"]) == result
        connector.invoke.assert_not_awaited()

    asyncio.run(scenario())


def test_write_timeout_never_retries_and_secret_arguments_blocked(tmp_path: Path):
    service, connector, _, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(server_id, ToolRiskReviewInput(**binding, risk="write-low"), principal)
        payload = ToolTestInput(
            **binding, arguments={}, idempotency_key="write", confirm_write=True
        )
        with pytest.raises(ValueError, match="secret"):
            await service.create(
                server_id,
                payload.model_copy(update={"arguments": {"password": "private"}}),
                principal,
            )
        connector.invoke.side_effect = TimeoutError()
        result = await service.create(server_id, payload, principal)
        assert result["status"] == "outcome-uncertain"
        assert await service.create(server_id, payload, principal) == result
        connector.invoke.assert_awaited_once()

    asyncio.run(scenario())


def test_admin_tool_test_api_without_registration(tmp_path: Path):
    app = create_app(
        Path(__file__).parent / "fixtures" / "skills",
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path),
        mcp_connector=NativeConnector(),
    )
    with TestClient(app) as client:
        server = client.post(
            "/v1/admin/mcp/servers",
            json={
                "alias": "tests",
                "transport": "streamable-http",
                "url": "https://example.test/mcp",
            },
        ).json()
        base = f"/v1/admin/mcp/servers/{server['server_id']}"
        assert client.post(f"{base}/test").is_success
        discovered = client.post(f"{base}/discover").json()
        binding = {
            "tool_name": "clauses/search",
            "expected_server_config_digest": discovered["config_digest"],
            "expected_schema_digest": discovered["tools"][0]["schema_digest"],
        }
        assert client.post(
            f"{base}/tool-risk-reviews", json={**binding, "risk": "read-only"}
        ).is_success
        response = client.post(
            f"{base}/tool-tests",
            json={
                **binding,
                "arguments": {},
                "idempotency_key": "first",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert (
            client.get(f"{base}/tool-tests/{response.json()['test_id']}").json() == response.json()
        )
        assert client.get("/v1/admin/tools").json() == []
        assert not app.state.control_store.snapshot().skills


def test_workflow_native_tool_uses_unified_invocations():
    from sage.temporal_worker import SageStepActivities
    from sage.workflows import WorkflowStepDefinition

    step = WorkflowStepDefinition(id="query", kind="tool", target="tool:mcp:server:query")
    activities = SageStepActivities(
        api_base_url="http://localhost", admin_token=None, bearer_token=None,
        token_scope=None, client=AsyncMock(),
    )
    activities._post = AsyncMock(return_value={"output": {"ok": True}})
    result = asyncio.run(activities.execute_step({
        "context": {"run_id": "run_test", "solution_id": "solution_test"},
        "step": step.model_dump(), "input": {"query": "test"},
    }))
    assert result == {"output": {"ok": True}}
    call = activities._post.await_args
    assert call.args[0] == "/v1/invocations"
    assert call.args[1]["capability_id"] == step.target
    assert call.args[1]["input"] == {"query": "test"}
    assert call.args[1]["parent_run_id"] == "run_test"


def test_review_cannot_downgrade_registered_risk(tmp_path: Path):
    service, _, _, principal, server_id, binding = setup_tests(tmp_path)
    service.servers.register_tool(server_id, "clauses/search", version="1.0.0",
                                  description="Write", risk="write-high")

    async def scenario():
        with pytest.raises(PermissionError, match="downgrade"):
            await service.review(
                server_id, ToolRiskReviewInput(**binding, risk="read-only"), principal
            )
        await service.review(
            server_id, ToolRiskReviewInput(**binding, risk="write-high"), principal
        )
        with pytest.raises(PermissionError, match="downgrade"):
            await service.review(
                server_id, ToolRiskReviewInput(**binding, risk="write-low"), principal
            )

    asyncio.run(scenario())


def test_schema_sensitive_output_is_redacted():
    schema = {"properties": {
        "private": {"x-sensitive": True},
        "nested": {"anyOf": [{"properties": {"value": {"writeOnly": True}}}]},
    }}
    output = McpToolTests.redact({"private": "personal", "nested": {"value": "hidden"},
                                "text": "known-credential"}, "known-credential", schema)
    assert output == {"private": "[redacted]", "nested": {"value": "[redacted]"},
                      "text": "[redacted]"}


def test_changed_review_finishes_without_dispatch(tmp_path: Path):
    service, connector, _, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        review = ToolRiskReviewInput(**binding, risk="write-high")
        await service.review(server_id, review, principal)
        pending = await service.create(server_id, ToolTestInput(
            **binding, arguments={}, idempotency_key="review-change", confirm_write=True,
        ), principal)
        await service.decide(pending["test_id"], principal, approve=True, reason="Reviewed")
        await service.review(server_id, review, principal)
        result = await service.execute(pending["test_id"])
        assert result.status == "failed"
        assert result.result == {"error": "risk_review_changed"}
        assert result.request is None
        connector.invoke.assert_not_awaited()

    asyncio.run(scenario())


def test_cancelled_write_persists_uncertainty_and_propagates(tmp_path: Path):
    service, connector, _, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(server_id, ToolRiskReviewInput(**binding, risk="write-low"), principal)
        payload = ToolTestInput(
            **binding, arguments={}, idempotency_key="cancel", confirm_write=True,
        )
        connector.invoke.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await service.create(server_id, payload, principal)
        result = await service.create(server_id, payload, principal)
        assert result["status"] == "outcome-uncertain"
        assert result["result"] == {"error": "cancelled"}
        connector.invoke.assert_awaited_once()

    asyncio.run(scenario())


def test_expired_approval_finishes_without_dispatch(tmp_path: Path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    service, connector, _, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(
            server_id, ToolRiskReviewInput(**binding, risk="write-high"), principal,
        )
        pending = await service.create(server_id, ToolTestInput(
            **binding, arguments={}, idempotency_key="expired", confirm_write=True,
        ), principal)
        task = service.tasks.get(pending["approval_task_id"])
        expired = task.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        with patch.object(service.tasks, "get", return_value=expired):
            result = await service.execute(pending["test_id"])
        assert result.status == "failed"
        assert result.result == {"error": "approval_expired"}
        assert result.request is None
        connector.invoke.assert_not_awaited()

    asyncio.run(scenario())


def test_late_preflight_failure_cannot_overwrite_running_test(tmp_path: Path):
    service, _, _, principal, server_id, binding = setup_tests(tmp_path)

    async def scenario():
        await service.review(
            server_id, ToolRiskReviewInput(**binding, risk="write-high"), principal,
        )
        pending = await service.create(server_id, ToolTestInput(
            **binding, arguments={}, idempotency_key="race", confirm_write=True,
        ), principal)
        record = service.get(pending["test_id"], principal)
        service.store.update(lambda state: state.mcp_tests.update({
            record.test_id: record.model_copy(update={"status": "running"}),
        }))
        result = service.finish(
            record, "failed", {"error": "tool_binding_changed"}, 0,
            expected_status="awaiting-approval",
        )
        assert result.status == "running"
        assert result.request is not None

    asyncio.run(scenario())
