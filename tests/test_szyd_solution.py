import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_agent_runtime import ScriptedProviderModel
from test_runner_app import InProcessRunnerClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.reference_solutions.szyd_compliance import default_szyd_agent
from sage.runner_app import RunnerSettings
from sage.settings import AgentModelSettings

ROOT = Path(__file__).parents[1]


def publication_module():
    spec = importlib.util.spec_from_file_location(
        "publish_szyd", ROOT / "scripts/publish_szyd_solution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SzydWorkflowModel(ScriptedProviderModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        start = max(index for index, item in enumerate(messages) if isinstance(item, HumanMessage))
        current = messages[start:]
        completed = self._completed_tools(current)
        results = [
            json.loads(item.content)
            for item in current
            if isinstance(item, ToolMessage) and item.name == "skill_szyd_law"
        ]
        if "read_file" not in completed:
            result = self._tool_call("read_file", {"file_path": "/szyd-law/SKILL.md"})
        elif not results:
            result = self._tool_call("skill_szyd_law", {"arguments": {"operation": "open"}})
        else:
            wrapped = results[-1]
            assert "error" not in wrapped, wrapped
            latest = wrapped["result"]
            decision = latest["decision"]["action"]
            state = latest["state"]
            if latest["completed"]:
                result = AIMessage(content=state["final_result"]["compliance_summary"])
            elif decision == "SUPPLEMENT":
                if len(results) == 1:
                    result = self._operation("resume", latest)
                else:
                    result = AIMessage(content="Please provide the investment amount and scope.")
            elif state["supplement_round"] == 0:
                result = self._operation(
                    "stage",
                    latest,
                    {
                        "name": "identification",
                        "stage": {
                            "version": 1,
                            "status": "INSUFFICIENT",
                            "reasoning": "Missing scope",
                            "data": {
                                "is_major": None,
                                "matched_categories": [],
                                "matched_items": [],
                            },
                            "evidence_refs": [],
                            "needs": ["Amount and scope"],
                        },
                    },
                )
            elif not state["evidence"]:
                result = self._operation(
                    "evidence",
                    latest,
                    {
                        "evidence": [
                            {
                                "id": "user-supplement",
                                "source": "user",
                                "content": state["input_text"],
                                "locator": "supplement message",
                                "kind": "document",
                                "verified": True,
                            }
                        ]
                    },
                )
            elif decision == "FINAL_SKIP":
                result = self._operation(
                    "final",
                    latest,
                    {
                        "approval_status": "NOT_APPLICABLE",
                        "final_risk_assessment": "N/A",
                        "key_findings": ["Routine stationery purchase"],
                        "compliance_summary": "Nonmajor fixture: routine stationery purchase.",
                        "recommendations": [],
                    },
                )
            else:
                result = self._operation(
                    "stage",
                    latest,
                    {
                        "name": "identification",
                        "stage": {
                            "version": state["version"],
                            "status": "DONE",
                            "reasoning": "Routine purchase",
                            "data": {
                                "is_major": False,
                                "matched_categories": [],
                                "matched_items": [],
                            },
                            "evidence_refs": ["user-supplement"],
                        },
                    },
                )
        return ChatResult(generations=[ChatGeneration(message=result)])

    def _operation(self, operation, latest, payload=None):
        return self._tool_call(
            "skill_szyd_law",
            {
                "arguments": {
                    "operation": operation,
                    "expected_revision": latest["revision"],
                    "payload": payload or {},
                }
            },
        )


def test_runtime_installs_entry_agent_and_stateful_adapter(tmp_path):
    app = create_app(
        ROOT / "skills",
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        agent = default_szyd_agent()
        assert agent.capability_ids == ("skill:szyd-law",)
        assert agent.agent_id in {item["agent_id"] for item in client.get("/v1/agents").json()}
        response = client.post(
            "/v1/messages",
            json={
                "message": "Review investment",
                "mode": "direct",
                "skill": "szyd-law",
                "thread_id": "case-thread",
                "arguments": {"operation": "open"},
            },
        )
        assert response.status_code == 200, response.text
        assert (tmp_path / "state/szyd-cases.sqlite3").is_file()


def test_publish_and_resume_via_entry_agent_and_real_runner(tmp_path):
    settings = ControlPlaneSettings(data_root=tmp_path / "state", admin_token="test-admin-token")
    runner = InProcessRunnerClient(RunnerSettings(token="x" * 32, work_root=tmp_path / "runner"))
    publisher = publication_module()
    initial_skills = tmp_path / "initial-skills"
    initial_skills.mkdir()

    def application():
        return create_app(
            initial_skills,
            agent_model=SzydWorkflowModel(),
            control_settings=settings,
            runner_client=runner,
            agent_settings=AgentModelSettings(recursion_limit=200),
        )

    with TestClient(application(), headers={"Authorization": "Bearer test-admin-token"}) as client:
        published = publisher.publish(client, activate=True)
        assert published["status"] == "active"
        assert publisher.publish(client, activate=True)["status"] == "already-active"
        solutions = client.get("/v1/solutions").json()
        assert solutions[0]["solution_id"] == "szyd-compliance"
        assert solutions[0]["supported_profiles"] == ["interactive"]
        assert (
            client.get("/v1/agent?solution_id=szyd-compliance").json()["agent_id"] == "szyd-agent"
        )
        first = client.post(
            "/v1/messages",
            json={
                "message": "Please review this proposal",
                "mode": "agent",
                "solution_id": "szyd-compliance",
                "thread_id": "review-thread",
            },
        )
        assert first.status_code == 200, first.text
        assert "provide" in first.json()["message"]
    with TestClient(application(), headers={"Authorization": "Bearer test-admin-token"}) as client:
        second = client.post(
            "/v1/messages",
            json={
                "message": "This is a routine stationery purchase of 10, not a major decision.",
                "mode": "agent",
                "solution_id": "szyd-compliance",
                "thread_id": "review-thread",
            },
        )
        assert second.status_code == 200, second.text
        assert "Nonmajor fixture" in second.json()["message"]
    assert runner.requests
    assert all(
        "state" not in request.arguments or isinstance(request.arguments["state"], dict)
        for request in runner.requests
    )
