import asyncio
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_settings import ControlPlaneSettings
from sage.domain import Principal, RuntimeContext


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_skill_operation_returns_handle_stream_result_and_idempotent_replay(
    client: TestClient,
) -> None:
    payload = {
        "capability_id": "skill:echo",
        "input": {"text": "async echo"},
    }

    started = client.post(
        "/v1/operations",
        headers={"Idempotency-Key": "operation-echo-1"},
        json=payload,
    )
    run_id = started.json()["run_id"]
    with client.stream("GET", started.json()["links"]["events"]) as stream:
        events = "".join(stream.iter_text())
    status = client.get(started.json()["links"]["status"])
    result = client.get(started.json()["links"]["result"])
    replay = client.post(
        "/v1/operations",
        headers={"Idempotency-Key": "operation-echo-1"},
        json=payload,
    )
    changed = client.post(
        "/v1/operations",
        headers={"Idempotency-Key": "operation-echo-1"},
        json={**payload, "input": {"text": "changed"}},
    )

    assert started.status_code == 202
    assert started.json()["schema_version"] == "1.0"
    assert started.json()["lifecycle"] == "interactive"
    assert started.json()["restart_recoverable"] is False
    assert started.json()["capability_id"] == "skill:echo"
    assert "event: run.started" in events
    assert "event: run.completed" in events
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["runtime_attached"] is False
    assert result.status_code == 200
    assert result.json()["data"] == {"text": "async echo"}
    assert replay.status_code == 200
    assert replay.json()["run_id"] == run_id
    assert replay.json()["status"] == "completed"
    assert changed.status_code == 409
    run = asyncio.run(client.app.state.run_store.get(run_id))
    assert run is not None
    assert run.input_data == {
        "kind": "interactive-operation",
        "operation_id": started.json()["operation_id"],
        "request": {**payload, "artifact_ids": []},
    }


class BlockingAgentModel(BaseChatModel):
    model_name: str = "blocking-agent"

    @property
    def _llm_type(self) -> str:
        return "blocking-agent"

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        del tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        raise AssertionError("BlockingAgentModel must run asynchronously")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_agent_operation_can_be_cancelled_explicitly(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=BlockingAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        started = client.post(
            "/v1/operations",
            headers={"Idempotency-Key": "operation-agent-cancel"},
            json={
                "capability_id": "agent:diagnostic-agent",
                "input": {"message": "Wait for cancellation"},
            },
        )
        cancelled = client.post(started.json()["links"]["cancel"])
        status = client.get(started.json()["links"]["status"])
        result = client.get(started.json()["links"]["result"])

    assert started.status_code == 202
    assert started.json()["status"] == "running"
    assert started.json()["runtime_attached"] is True
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["runtime_attached"] is False
    assert status.json()["status"] == "cancelled"
    assert result.status_code == 409
    assert result.json()["detail"] == "Run result is not complete: cancelled"


def test_detached_interactive_operation_does_not_claim_restart_recovery(
    client: TestClient,
) -> None:
    context = RuntimeContext.create(
        mode="agent",
        principal=Principal.anonymous(),
        run_id="run_op_detached",
        generation_id="gen_startup",
        agent_id="diagnostic-agent",
    )
    asyncio.run(
        client.app.state.run_store.begin(
            context,
            input_data={
                "kind": "interactive-operation",
                "operation_id": "op_detached",
                "request": {
                    "capability_id": "agent:diagnostic-agent",
                    "input": {"message": "interrupted"},
                },
            },
        )
    )

    status = client.get("/v1/operations/run_op_detached")
    cancelled = client.post("/v1/operations/run_op_detached/cancel")

    assert status.status_code == 200
    assert status.json()["status"] == "running"
    assert status.json()["runtime_attached"] is False
    assert status.json()["restart_recoverable"] is False
    assert cancelled.status_code == 409
    assert "no longer attached" in cancelled.json()["detail"]


def test_operation_rejects_missing_key_and_native_tool_target(client: TestClient) -> None:
    missing_key = client.post(
        "/v1/operations",
        json={"capability_id": "skill:echo", "input": {"text": "x"}},
    )
    native_tool = client.post(
        "/v1/operations",
        headers={"Idempotency-Key": "native-not-supported"},
        json={"capability_id": "tool:mcp:test:search", "input": {}},
    )

    assert missing_key.status_code == 422
    assert "Idempotency-Key is required" in missing_key.json()["detail"]
    assert native_tool.status_code == 422
    assert "Native MCP tools use /v1/invocations" in native_tool.json()["detail"]
