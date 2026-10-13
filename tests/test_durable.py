import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError

import sage.temporal_workflow as temporal_workflow_module
from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.durable import DurableRunHandle, DurableRunRequest, TemporalExecutionAdapter
from sage.temporal_worker import SageStepActivities
from sage.temporal_workflow import SageSolutionWorkflow

SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"
ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}


class FakeWorkflowHandle:
    id = "wf_123"
    first_execution_run_id = "temporal-run-1"

    def __init__(self) -> None:
        self.cancel_reason: str | None = None
        self.signals: list[tuple[str, dict[str, object]]] = []

    async def describe(self) -> object:
        return SimpleNamespace(
            run_id="temporal-run-1",
            status=SimpleNamespace(name="RUNNING"),
        )

    async def cancel(self, *, reason: str = "") -> None:
        self.cancel_reason = reason

    async def result(self):
        return {"steps": {"calc": {"total": 32.4}}}

    async def signal(self, name: str, payload: dict[str, object]) -> None:
        self.signals.append((name, payload))


class FakeTemporalClient:
    def __init__(self) -> None:
        self.handle = FakeWorkflowHandle()
        self.started: tuple[object, object, dict[str, object]] | None = None

    async def start_workflow(
        self,
        workflow: object,
        payload: object,
        **kwargs: object,
    ) -> FakeWorkflowHandle:
        self.started = (workflow, payload, kwargs)
        return self.handle

    def get_workflow_handle(self, workflow_id: str) -> FakeWorkflowHandle:
        assert workflow_id == "wf_123"
        return self.handle


def test_temporal_adapter_uses_stable_id_and_rejects_duplicates() -> None:
    client = FakeTemporalClient()
    adapter = TemporalExecutionAdapter(client)  # type: ignore[arg-type]
    request = DurableRunRequest(
        workflow_id="wf_123",
        workflow_type="contract-review",
        task_queue="sage-workflows",
        payload={"artifact_id": "art_123"},
        execution_timeout_seconds=3600,
    )

    started = asyncio.run(adapter.start(request))

    assert started.workflow_id == "wf_123"
    assert started.run_id == "temporal-run-1"
    assert client.started is not None
    workflow, payload, options = client.started
    assert workflow == "contract-review"
    assert payload == {"artifact_id": "art_123"}
    assert options["id"] == "wf_123"
    assert options["task_queue"] == "sage-workflows"
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert options["id_conflict_policy"] == WorkflowIDConflictPolicy.FAIL


def test_temporal_adapter_describes_and_cancels_workflow() -> None:
    client = FakeTemporalClient()
    adapter = TemporalExecutionAdapter(client)  # type: ignore[arg-type]

    described = asyncio.run(adapter.describe("wf_123"))
    asyncio.run(adapter.cancel("wf_123", reason="user requested"))
    asyncio.run(adapter.signal("wf_123", "resume", {"approved": True}))

    assert described.status == "running"
    assert client.handle.cancel_reason == "user requested"
    assert client.handle.signals == [("resume", {"approved": True})]
    assert asyncio.run(adapter.result("wf_123")) == {"steps": {"calc": {"total": 32.4}}}


class FakeDurableExecution:
    def __init__(self) -> None:
        self.status = "running"
        self.starts: list[DurableRunRequest] = []
        self.cancelled: list[str] = []
        self.signals: list[tuple[str, str, dict[str, object]]] = []

    async def start(self, request: DurableRunRequest) -> DurableRunHandle:
        self.starts.append(request)
        return DurableRunHandle(
            workflow_id=request.workflow_id,
            run_id="temporal-run-1",
            status="running",
        )

    async def describe(self, workflow_id: str) -> DurableRunHandle:
        return DurableRunHandle(
            workflow_id=workflow_id,
            run_id="temporal-run-1",
            status=self.status,
        )

    async def result(self, workflow_id: str):
        assert self.status == "completed"
        return {"steps": {"calc": {"total": 32.4}}}

    async def cancel(self, workflow_id: str, *, reason: str) -> None:
        assert reason
        self.cancelled.append(workflow_id)

    async def signal(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, object],
    ) -> None:
        self.signals.append((workflow_id, signal_name, payload))


def test_durable_api_starts_idempotently_and_cancels_locked_workflow(
    tmp_path: Path,
) -> None:
    durable = FakeDurableExecution()
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        durable_execution=durable,
    )
    digest = "sha256:" + "c" * 64

    with TestClient(app) as client:
        client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "document-analysis",
                "title": "Document analysis",
                "owner": "platform-team",
                "supported_profiles": ["durable"],
            },
        )
        release = client.post(
            "/v1/admin/solutions/document-analysis/releases",
            headers=ADMIN_HEADERS,
            json={
                "version": "1.0.0",
                "components": [
                    {
                        "kind": "workflow",
                        "component_id": "analyze-document",
                        "version": "1.0.0",
                        "content_digest": digest,
                    }
                ],
            },
        )
        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Activate document analysis",
                "solution_release_ids": [release.json()["release_id"]],
            },
        )
        candidate_id = candidate.json()["candidate_id"]
        client.post(
            f"/v1/admin/candidates/{candidate_id}/validate",
            headers=ADMIN_HEADERS,
        )
        approval = client.post(
            f"/v1/admin/candidates/{candidate_id}/submit",
            headers=ADMIN_HEADERS,
        )
        client.post(
            f"/v1/admin/approvals/{approval.json()['approval_id']}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "Workflow reviewed."},
        )
        client.post(
            f"/v1/admin/candidates/{candidate_id}/activate",
            headers=ADMIN_HEADERS,
        )
        payload = {
            "solution_id": "document-analysis",
            "workflow_type": "analyze-document",
            "idempotency_key": "document-42",
            "payload": {"artifact_id": "art_42"},
            "execution_timeout_seconds": 3600,
        }
        started = client.post("/v1/durable-runs", json=payload)
        repeated = client.post("/v1/durable-runs", json=payload)
        changed = client.post(
            "/v1/durable-runs",
            json={**payload, "payload": {"artifact_id": "art_other"}},
        )
        described = client.get(f"/v1/durable-runs/{started.json()['run_id']}")
        human_task = client.app.state.human_task_store.create(
            run_id=started.json()["run_id"],
            workspace_id="ws_default",
            solution_id="document-analysis",
            kind="approval",
            title="Approve report",
            instructions="",
            assigned_roles=("user",),
        )
        decision = client.post(
            f"/v1/human-tasks/{human_task.task_id}/decision",
            json={"decision": {"approved": True}, "approve": True, "reason": "Reviewed"},
        )
        cancelled = client.post(
            f"/v1/durable-runs/{started.json()['run_id']}/cancel"
        )
        repeated_cancel = client.post(
            f"/v1/durable-runs/{started.json()['run_id']}/cancel"
        )
        run = client.get(f"/v1/runs/{started.json()['run_id']}")
        second = client.post(
            "/v1/durable-runs", json={**payload, "idempotency_key": "completed-43"},
        )
        second_id = second.json()["run_id"]
        assert client.get(f"/v1/durable-runs/{second_id}/result").status_code == 409
        durable.status = "completed"
        completed_result = client.get(f"/v1/durable-runs/{second_id}/result")
        assert completed_result.status_code == 200
        assert completed_result.json() == {"steps": {"calc": {"total": 32.4}}}
        assert client.get(f"/v1/runs/{second_id}").json()["status"] == "completed"

    assert started.status_code == 202
    assert repeated.status_code == 202
    assert started.json()["workflow_id"] == repeated.json()["workflow_id"]
    assert changed.status_code == 409
    assert len(durable.starts) == 2
    assert durable.starts[0].payload["context"]["solution_release_id"] == (
        "document-analysis@1.0.0"
    )
    assert described.status_code == 200
    assert decision.status_code == 200
    assert durable.signals[0][0] == started.json()["workflow_id"]
    assert durable.signals[0][1] == "human_task_decided"
    assert cancelled.status_code == 202
    assert repeated_cancel.status_code == 202
    assert durable.cancelled == [started.json()["workflow_id"]]
    assert run.json()["status"] == "cancelled"


def test_temporal_worker_activity_calls_governed_invocation_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": "completed", "output": {"story_count": 1}},
        )

    client = httpx.AsyncClient(
        base_url="http://sage",
        transport=httpx.MockTransport(handler),
    )
    activities = SageStepActivities(
        api_base_url="http://sage",
        admin_token="admin",
        bearer_token="worker-token",
        token_scope=None,
        client=client,
    )

    result = asyncio.run(
        activities.execute_step(
            {
                "context": {"run_id": "run_1", "solution_id": "news-intelligence"},
                "step": {
                    "step_id": "analyze",
                    "kind": "skill",
                    "target": "news-intelligence",
                },
                "input": {"articles": []},
            }
        )
    )
    asyncio.run(client.aclose())

    assert result["output"]["story_count"] == 1
    assert requests[0].url.path == "/v1/invocations"
    assert requests[0].headers["authorization"] == "Bearer worker-token"
    assert '"capability_id":"skill:news-intelligence"' in requests[0].content.decode()


def test_temporal_worker_agent_step_calls_governed_agent_invocation_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": "completed", "output": {"message": "done"}},
        )

    client = httpx.AsyncClient(
        base_url="http://sage",
        transport=httpx.MockTransport(handler),
    )
    activities = SageStepActivities(
        api_base_url="http://sage",
        admin_token="admin",
        bearer_token="worker-token",
        token_scope=None,
        client=client,
    )

    result = asyncio.run(
        activities.execute_step(
            {
                "context": {"run_id": "run_1", "solution_id": "incident"},
                "step": {
                    "step_id": "analyze",
                    "kind": "agent",
                    "target": "incident-analyst",
                },
                "input": {
                    "message": "Analyze the evidence",
                    "evidence_ids": ["art_1"],
                },
            }
        )
    )
    asyncio.run(client.aclose())

    assert result["output"]["message"] == "done"
    assert requests[0].url.path == "/v1/agent-invocations"
    body = requests[0].content.decode()
    assert '"parent_run_id":"run_1"' in body
    assert '"agent_id":"incident-analyst"' in body
    assert '"idempotency_key":"run_1:analyze"' in body


def test_temporal_worker_rejects_agent_output_that_fails_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"status": "completed", "output": {"confidence": "high"}},
        )

    client = httpx.AsyncClient(
        base_url="http://sage",
        transport=httpx.MockTransport(handler),
    )
    activities = SageStepActivities(
        api_base_url="http://sage",
        admin_token="admin",
        bearer_token="worker-token",
        token_scope=None,
        client=client,
    )

    with pytest.raises(ApplicationError, match="failed schema validation"):
        asyncio.run(
            activities.execute_step(
                {
                    "context": {"run_id": "run_1", "solution_id": "incident"},
                    "step": {
                        "step_id": "verify",
                        "kind": "agent",
                        "target": "verifier",
                        "output_schema": {
                            "type": "object",
                            "properties": {"confidence": {"type": "number"}},
                            "required": ["confidence"],
                        },
                    },
                    "input": {},
                }
            )
        )
    asyncio.run(client.aclose())


def test_solution_workflow_executes_ready_steps_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0
    both_started = asyncio.Event()

    async def execute_activity(
        name: str,
        request: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        nonlocal active, maximum_active
        assert name == "execute-sage-step"
        assert options["activity_id"]
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        step = request["step"]
        assert isinstance(step, dict)
        return {"output": {"step": step["step_id"]}}

    monkeypatch.setattr(
        temporal_workflow_module.workflow,
        "execute_activity",
        execute_activity,
    )
    payload = {
        "context": {"run_id": "run_parallel"},
        "input": {},
        "workflow": {
            "workflow_id": "parallel-analysis",
            "max_concurrency": 2,
            "steps": [
                {"step_id": "security", "kind": "agent", "target": "security"},
                {"step_id": "logs", "kind": "agent", "target": "logs"},
            ],
        },
    }

    result = asyncio.run(SageSolutionWorkflow().run(payload))

    assert maximum_active == 2
    assert set(result["results"]) == {"security", "logs"}


def test_solution_workflow_branches_and_joins_all_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    async def execute_activity(
        name: str,
        request: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        del name, options
        step = request["step"]
        assert isinstance(step, dict)
        step_id = str(step["step_id"])
        executed.append(step_id)
        if step_id == "route":
            return {"output": {"route": "security"}}
        return {"output": {"step": step_id}}

    monkeypatch.setattr(
        temporal_workflow_module.workflow,
        "execute_activity",
        execute_activity,
    )
    security_condition = {
        "source": "step-output",
        "step_id": "route",
        "path": "/output/route",
        "operator": "equals",
        "value": "security",
    }
    billing_condition = {**security_condition, "value": "billing"}
    payload = {
        "context": {"run_id": "run_branch"},
        "input": {},
        "workflow": {
            "workflow_id": "branch-analysis",
            "max_concurrency": 2,
            "steps": [
                {"step_id": "route", "kind": "agent", "target": "router"},
                {
                    "step_id": "security",
                    "kind": "agent",
                    "target": "security",
                    "depends_on": ["route"],
                    "when": security_condition,
                },
                {
                    "step_id": "billing",
                    "kind": "agent",
                    "target": "billing",
                    "depends_on": ["route"],
                    "when": billing_condition,
                },
                {
                    "step_id": "summary",
                    "kind": "agent",
                    "target": "summary",
                    "depends_on": ["security", "billing"],
                    "join_policy": "all-settled",
                },
            ],
        },
    }

    result = asyncio.run(SageSolutionWorkflow().run(payload))

    assert executed == ["route", "security", "summary"]
    assert result["results"]["billing"]["_sage"]["status"] == "skipped"