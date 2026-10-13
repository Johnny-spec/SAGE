import json
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.solution_assist import SolutionAssistant


def test_ai_suggestion_is_validated_and_never_saved(tmp_path: Path) -> None:
    suggestion = {
        "title": "Echo studio",
        "description": "Echo an input",
        "definition": {
            "apiVersion": "sage.dev/v1beta1",
            "kind": "WorkflowTemplate",
            "workflowId": "echo-flow",
            "version": "1.0.0",
            "steps": [
                {
                    "id": "echo",
                    "kind": "skill",
                    "target": "echo",
                    "inheritInput": False,
                    "bindings": {"text": {"source": "workflow-input", "path": "/message"}},
                },
            ],
        },
        "recommendations": [{"step_id": "echo", "reason": "Bind message to the echo input"}],
    }
    invalid = json.loads(json.dumps(suggestion))
    invalid["definition"]["steps"][0]["target"] = "invented"
    app = create_app(
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            admin_auth_enabled=False,
        ),
    )
    with TestClient(app) as client:
        payload = {
            "goal": "Create an echo workflow",
            "solution_id": "echo-app",
            "targets": ["echo"],
        }
        assert client.post("/v1/admin/solution-assist", json=payload).status_code == 503
        app.state.solution_assistant = SolutionAssistant(
            FakeListChatModel(
                responses=[json.dumps(suggestion), json.dumps(invalid), "not JSON"],
            )
        )
        before = app.state.control_store.snapshot()
        response = client.post("/v1/admin/solution-assist", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["saved"] is False
        assert (
            response.json()["draft"]["definition"]["steps"][0]["bindings"]["text"]["path"]
            == "/message"
        )
        assert client.post("/v1/admin/solution-assist", json=payload).status_code == 422
        assert client.post("/v1/admin/solution-assist", json=payload).status_code == 422

        class BlockModel:
            async def ainvoke(self, messages):
                return AIMessage(content=[{"type": "text", "text": json.dumps(suggestion)}])

        app.state.solution_assistant = SolutionAssistant(BlockModel())
        assert client.post("/v1/admin/solution-assist", json=payload).status_code == 200
        after = app.state.control_store.snapshot()
        assert before == after
