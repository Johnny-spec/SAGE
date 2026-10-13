import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sage.agent_model import DemoAgentModel
from sage.models import ExecutionBinding
from sage.registry import SkillPackageError, SkillRegistry


class JsonSkillModel(DemoAgentModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"ok":true}'))])


def make_skill(tmp_path: Path, mode: str = "workflow"):
    root = tmp_path / "flow"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: flow\ndescription: Process orders.\n---\nUse the declared tools.",
        encoding="utf-8",
    )
    execution = {"type": mode, "capabilityIds": ["tool:mcp:orders:query"]}
    if mode == "workflow":
        execution["workflow"] = "workflow.yaml"
    manifest = {
        "apiVersion": "sage.dev/v1alpha1",
        "kind": "SkillBinding",
        "metadata": {"name": "flow", "version": "1.0.0"},
        "spec": {
            "execution": execution,
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
    }
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (root / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sage.dev/v1alpha1",
                "kind": "WorkflowTemplate",
                "workflowId": "flow",
                "version": "1.0.0",
                "steps": [{"id": "query", "kind": "tool", "target": "tool:mcp:orders:query"}],
            }
        ),
        encoding="utf-8",
    )
    return root, manifest


@pytest.mark.parametrize("mode", ["workflow", "model"])
def test_orchestrated_skill_explicit_dependencies(tmp_path: Path, mode: str):
    root, _ = make_skill(tmp_path, mode)
    definition = SkillRegistry.load_definition(root)
    assert definition.manifest.spec.execution.type == mode
    assert definition.manifest.spec.execution.capability_ids == ("tool:mcp:orders:query",)
    with pytest.raises(ValueError, match="explicit"):
        ExecutionBinding(type=mode)


def test_orchestrated_skill_rejects_path_escape_and_undeclared_tool(tmp_path: Path):
    root, manifest = make_skill(tmp_path)
    manifest["spec"]["execution"]["workflow"] = "../outside.yaml"
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(SkillPackageError, match="inside"):
        SkillRegistry.load_definition(root)

    manifest["spec"]["execution"]["workflow"] = "workflow.yaml"
    manifest["spec"]["execution"]["capabilityIds"] = ["tool:mcp:orders:other"]
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(SkillPackageError, match="not declared"):
        SkillRegistry.load_definition(root)


def test_orchestrated_skill_durable_api_requires_infrastructure(tmp_path: Path):
    from fastapi.testclient import TestClient

    from sage.agent_model import DemoAgentModel
    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    make_skill(skills_root)
    app = create_app(
        skills_root,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        response = client.post("/v1/durable-runs", json={"skill": "flow", "idempotency_key": "one"})
        assert response.status_code == 503, response.text


@pytest.mark.parametrize("mode", ["workflow", "model"])
def test_standalone_skill_launch_result_and_dependency_scope(tmp_path: Path, mode: str):
    from fastapi.testclient import TestClient

    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings
    from sage.durable import DurableRunHandle

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    root, manifest = make_skill(skills_root, mode)
    shutil.copytree(Path(__file__).parent / "fixtures" / "skills" / "echo", skills_root / "echo")
    manifest["spec"]["execution"]["capabilityIds"] = ["skill:echo"]
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    workflow = yaml.safe_load((root / "workflow.yaml").read_text(encoding="utf-8"))
    workflow["steps"][0].update({"kind": "skill", "target": "echo"})
    (root / "workflow.yaml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    backend = AsyncMock()
    backend.start.return_value = DurableRunHandle(
        workflow_id="wf", run_id="temporal", status="running"
    )
    app = create_app(
        skills_root,
        agent_model=JsonSkillModel(),
        durable_execution=backend,
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        initial_agents = app.state.control_store.snapshot().agents
        response = client.post("/v1/durable-runs", json={"skill": "flow", "idempotency_key": "one"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        denied = client.post(
            "/v1/invocations",
            json={
                "parent_run_id": run_id,
                "capability_id": "tool:mcp:unknown:query",
            },
        )
        assert denied.status_code == 403, denied.text
        action = "model" if mode == "model" else "result"
        result = client.post(
            f"/v1/admin/skill-runs/{run_id}/{action}", json={"output": {"ok": True}}
        )
        assert result.status_code == 200, result.text
        assert result.json()["output"] == {"ok": True}
        assert client.get(f"/v1/durable-runs/{run_id}/result").json() == result.json()
        result_view = client.get(f"/v1/runs/{run_id}/result")
        events = client.get(f"/v1/runs/{run_id}/events").json()
        available = next(item for item in events if item["name"] == "result.available")
        assert result_view.status_code == 200
        assert available["data"]["result_digest"] == result_view.json()["result_digest"]
        assert result_view.json()["data"] == {"ok": True}
        assert app.state.control_store.snapshot().agents == initial_agents


def test_pending_model_turn_is_not_resumed_before_approval(tmp_path: Path):
    import asyncio
    from types import SimpleNamespace

    from sage.human_tasks import SqliteHumanTaskStore
    from sage.invocations import SqliteInvocationStore
    from sage.skill_orchestration import SkillOrchestrator

    async def scenario():
        root, _ = make_skill(tmp_path, "model")
        definition = SkillRegistry.load_definition(root)
        tasks = SqliteHumanTaskStore(str(tmp_path / "tasks.sqlite3"))
        task = tasks.create(
            run_id="run",
            workspace_id="ws",
            solution_id="_standalone",
            kind="approval",
            title="Write",
            instructions="Review",
            assigned_roles=("admin",),
        )
        pending = {
            "status": "awaiting-approval",
            "tasks": [{"task_id": task.task_id}],
            "counters": {},
        }
        run = SimpleNamespace(
            status="running",
            events=[
                SimpleNamespace(
                    name="skill.orchestration.prepared",
                    data={"input": {}, "execution_type": "model"},
                ),
                SimpleNamespace(name="skill.model.turn", data=pending),
            ],
        )
        runtime = AsyncMock()
        runs = AsyncMock()
        orchestrator = SkillOrchestrator(
            None,
            None,
            runs,
            SqliteInvocationStore(str(tmp_path / "calls.sqlite3")),
            AsyncMock(),
            "queue",
            tasks,
        )
        orchestrator.resolve = AsyncMock(return_value=(run, None, definition))
        orchestrator._model_runtimes["run"] = runtime
        assert await orchestrator.model("run") == pending
        assert await orchestrator.model("run") == pending
        runtime.durable_turn.assert_not_awaited()
        tasks.decide(
            task.task_id,
            actor_id="admin",
            actor_roles=frozenset({"admin"}),
            decision={},
            approve=False,
            reason="Rejected",
        )
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="Approval rejected"):
            await orchestrator.model("run")
        runtime.durable_turn.assert_not_awaited()
        runs.append.assert_awaited_once_with("run", "run.failed", {"error": "approval_not_granted"})

    asyncio.run(scenario())


def test_durable_agent_and_direct_tool_approval_api(tmp_path: Path):
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from test_runtime_approval import AdminPrincipalResolver
    from test_tool_registration import NativeConnector

    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings
    from sage.durable import DurableRunHandle

    connector = NativeConnector()
    connector.invoke = AsyncMock(return_value={"ok": True})
    backend = AsyncMock()
    backend.start.return_value = DurableRunHandle(workflow_id="wf", status="running")
    app = create_app(
        Path(__file__).parent / "fixtures" / "skills",
        agent_model=JsonSkillModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
        principal_resolver=AdminPrincipalResolver(),
        mcp_connector=connector,
        durable_execution=backend,
    )
    with TestClient(app) as client:
        server = client.post(
            "/v1/admin/mcp/servers",
            json={
                "alias": "writes",
                "transport": "streamable-http",
                "url": "https://example.test/mcp",
            },
        ).json()
        base = f"/v1/admin/mcp/servers/{server['server_id']}"
        assert client.post(f"{base}/test").is_success
        assert client.post(f"{base}/discover").is_success
        tool = client.post(
            f"{base}/tool-registrations",
            json={
                "tool_name": "clauses/search",
                "version": "1.0.0",
                "description": "Write",
                "risk": "write-high",
            },
        ).json()
        agent = client.post(
            "/v1/admin/agents",
            json={
                "agent_id": "writer",
                "name": "Writer",
                "version": "1.0.0",
                "instructions": "Process the request",
                "capability_ids": [tool["tool_id"]],
            },
        ).json()
        assert (
            client.post(f"/v1/admin/agents/{agent['draft_id']}/validate").json()["status"]
            == "valid"
        )
        candidate = client.post(
            "/v1/admin/candidates",
            json={
                "title": "Write",
                "agent_draft_ids": [agent["draft_id"]],
                "tool_registration_ids": [tool["registration_id"]],
                "default_agent_id": "writer",
            },
        ).json()
        published = client.post(f"/v1/admin/candidates/{candidate['candidate_id']}/publish")
        assert published.is_success, published.text
        agent_request = {"agent_id": "writer", "idempotency_key": "agent"}
        assert client.post("/v1/durable-runs", json=agent_request).status_code == 503
        app.state.runtime_manager.active.agent_runtimes._checkpointer = InMemorySaver()
        started = client.post("/v1/durable-runs", json=agent_request)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        result = client.post(f"/v1/admin/skill-runs/{run_id}/model")
        assert result.status_code == 200, result.text
        assert result.json()["output"] == {"answer": '{"ok":true}'}

        request = {
            "capability_id": tool["tool_id"],
            "input": {"value": 1},
            "idempotency_key": "write",
        }
        pending = client.post("/v1/invocations", json=request)
        assert pending.status_code == 202, pending.text
        body = pending.json()
        connector.invoke.assert_not_awaited()
        assert client.post("/v1/invocations", json=request).json() == body
        assert (
            client.post("/v1/invocations", json={**request, "input": {"value": 2}}).status_code
            == 409
        )
        endpoint = f"/v1/admin/skill-runs/{body['run_id']}/tool"
        assert client.post(endpoint).json()["status"] == "awaiting-approval"
        tasks = client.get("/v1/human-tasks")
        assert tasks.is_success, tasks.text
        assert any(task["task_id"] == body["approval_task_id"] for task in tasks.json())
        decision_url = f"/v1/human-tasks/{body['approval_task_id']}/decision"
        decision = {"approve": True, "reason": "Reviewed exact call"}
        assert client.post(decision_url, json=decision).is_success
        assert client.post(decision_url, json={"approve": False}).status_code == 409
        from unittest.mock import patch

        from sage.invocations import InvocationInProgressError

        with patch.object(
            app.state.skill_orchestrator.approvals, "dispatch",
            side_effect=InvocationInProgressError("running"),
        ):
            assert client.post(endpoint).status_code == 409
            assert client.get(f"/v1/runs/{body['run_id']}").json()["status"] == "running"
            connector.invoke.assert_not_awaited()
        completed = client.post(endpoint)
        assert completed.status_code == 200, completed.text
        assert completed.json()["output"] == {"ok": True}
        assert client.post(endpoint).json() == completed.json()
        connector.invoke.assert_awaited_once()
        assert client.get(f"/v1/durable-runs/{body['run_id']}/result").json() == completed.json()


def test_solution_must_lock_orchestrated_skill_dependencies(tmp_path: Path):
    from fastapi.testclient import TestClient

    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings
    from sage.digests import directory_digest

    skills = tmp_path / "skills"
    skills.mkdir()
    root, manifest = make_skill(skills, "model")
    shutil.copytree(Path(__file__).parent / "fixtures" / "skills" / "echo", skills / "echo")
    manifest["spec"]["execution"]["capabilityIds"] = ["skill:echo"]
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    app = create_app(
        skills,
        agent_model=JsonSkillModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/solutions",
            json={
                "solution_id": "flow",
                "title": "Flow",
                "owner": "test",
                "supported_profiles": ["durable"],
            },
        )
        assert created.is_success, created.text
        release = client.post(
            "/v1/admin/solutions/flow/releases",
            json={
                "version": "1.0.0",
                "components": [
                    {
                        "kind": "skill",
                        "component_id": "flow",
                        "version": "1.0.0",
                        "content_digest": directory_digest(root),
                    }
                ],
            },
        )
        assert release.is_success, release.text
        candidate = client.post(
            "/v1/admin/candidates",
            json={
                "title": "Missing lock",
                "solution_release_ids": [release.json()["release_id"]],
            },
        ).json()
        validated = client.post(f"/v1/admin/candidates/{candidate['candidate_id']}/validate")
        assert validated.is_success, validated.text
        assert "solution_skill_capability_not_locked" in {
            issue["code"] for issue in validated.json()["validation"]["issues"]
        }


def test_example_workflow_passes_only_declared_tool_arguments():
    from sage.temporal_workflow import _bound_step_input
    from sage.workflows import load_workflow_template
    from scripts.demo_mcp_server import calculate_total, lookup_runbook

    workflow = load_workflow_template(Path("docs/examples/quote/workflow.yaml"))
    values = {"unit_price": 12, "quantity": 3, "service": "iis"}
    results = {}
    handlers = {"total": calculate_total, "runbook": lookup_runbook}
    for step in workflow.steps:
        arguments = _bound_step_input(step.model_dump(mode="json"), values, results)
        results[step.step_id] = {"output": handlers[step.step_id](**arguments)}
    output = _bound_step_input(
        {
            "inherit_input": False,
            "bindings": {
                name: binding.model_dump(mode="json") for name, binding in workflow.outputs.items()
            },
        },
        values,
        results,
    )
    assert output["total"] == 36
    assert output["runbook"]["found"] is True


def test_invalid_model_output_marks_run_failed(tmp_path: Path):
    from fastapi.testclient import TestClient

    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings
    from sage.durable import DurableRunHandle

    class InvalidModel(JsonSkillModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="not-json"))])

    skills = tmp_path / "skills"
    skills.mkdir()
    root, manifest = make_skill(skills, "model")
    shutil.copytree(Path(__file__).parent / "fixtures" / "skills" / "echo", skills / "echo")
    manifest["spec"]["execution"]["capabilityIds"] = ["skill:echo"]
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    backend = AsyncMock()
    backend.start.return_value = DurableRunHandle(workflow_id="wf", status="running")
    app = create_app(skills, agent_model=InvalidModel(), durable_execution=backend,
                     control_settings=ControlPlaneSettings(data_root=tmp_path / "state"))
    with TestClient(app) as client:
        started = client.post("/v1/durable-runs", json={"skill": "flow", "idempotency_key": "bad"})
        run_id = started.json()["run_id"]
        result = client.post(f"/v1/admin/skill-runs/{run_id}/model")
        assert result.status_code == 422, result.text
        assert client.get(f"/v1/runs/{run_id}").json()["status"] == "failed"


@pytest.mark.parametrize("mode", ["model", "workflow"])
def test_model_skill_write_dependency_requires_checkpoint_and_public_approval(
    tmp_path: Path, mode: str,
):
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from test_runtime_approval import AdminPrincipalResolver

    from sage.app import create_app
    from sage.control_settings import ControlPlaneSettings
    from sage.durable import DurableRunHandle

    skills = tmp_path / "skills"
    skills.mkdir()
    root, manifest = make_skill(skills, mode)
    manifest["spec"]["execution"]["capabilityIds"] = ["skill:echo"]
    (root / "skill.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    workflow_path = root / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow["steps"][0].update({"kind": "skill", "target": "echo"})
    workflow_path.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    shutil.copytree(Path(__file__).parent / "fixtures/skills/echo", skills / "echo")
    echo_path = skills / "echo/skill.yaml"
    echo = yaml.safe_load(echo_path.read_text(encoding="utf-8"))
    echo["spec"].setdefault("policy", {})["risk"] = "write-high"
    echo_path.write_text(yaml.safe_dump(echo), encoding="utf-8")
    backend = AsyncMock()
    backend.start.return_value = DurableRunHandle(workflow_id="wf", status="running")
    app = create_app(
        skills, agent_model=JsonSkillModel(), durable_execution=backend,
        principal_resolver=AdminPrincipalResolver(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        request = {"skill": "flow", "idempotency_key": "approval"}
        snapshot = app.state.runtime_manager.active
        if mode == "model":
            unavailable = client.post("/v1/durable-runs", json=request)
            assert unavailable.status_code == 503, unavailable.text
            backend.start.assert_not_awaited()
        snapshot.agent_runtimes._checkpointer = InMemorySaver()
        started = client.post("/v1/durable-runs", json=request)
        assert started.status_code == 202, started.text
        import asyncio

        run = asyncio.run(app.state.run_store.get(started.json()["run_id"]))
        invocation = {"parent_run_id": run.context.run_id, "capability_id": "skill:echo",
                      "input": {"text": "reviewed"}, "idempotency_key": "call"}
        pending = client.post("/v1/invocations", json=invocation)
        assert pending.status_code == 202, pending.text
        task = app.state.human_task_store.get(pending.json()["approval_task_id"])
        assert task.subject["scope"] == "published-skill"
        assert task.subject["arguments"] == {"text": "reviewed"}
        response = client.post(f"/v1/human-tasks/{task.task_id}/decision", json={
            "approve": True, "reason": "Reviewed Skill arguments",
        })
        assert response.status_code == 200, response.text
        backend.signal.assert_awaited_once()
        assert backend.signal.await_args.args[0] == run.context.request_id
        completed = client.post("/v1/invocations", json=invocation)
        assert completed.status_code == 200, completed.text
        assert completed.json()["output"] == {"text": "reviewed"}
        assert client.post("/v1/invocations", json=invocation).json() == completed.json()
