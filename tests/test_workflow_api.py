from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.tools import McpToolBinding, McpToolDefinition
from sage.workflow_api import create_workflow_router


def test_workflow_targets_include_published_tool_details() -> None:
    tool = McpToolDefinition(
        version="1.2.0", description="Search approved knowledge articles.",
        binding=McpToolBinding(server_id="knowledge", server_alias="Knowledge MCP",
                               tool_name="search", schema_digest="sha256:" + "a" * 64,
                               server_config_digest="sha256:" + "b" * 64),
        input_schema={"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Search phrase"},
            "limit": {"type": "integer", "default": 5, "minimum": 1},
        }},
        output_schema={"type": "object", "properties": {"matches": {"type": "array"}}},
        timeout_seconds=45, max_output_bytes=8192,
    )
    other = tool.model_copy(update={
        "binding": tool.binding.model_copy(update={"server_id": "other",
                                                   "server_alias": "Other MCP"}),
        "output_schema": None,
    })
    app = FastAPI()
    app.include_router(create_workflow_router())
    app.state.runtime_manager = SimpleNamespace(active=SimpleNamespace(
        registry=[], generation=SimpleNamespace(generation_id="gen-tools", agent_definitions={},
                                                tool_definitions={tool.tool_id: tool,
                                                                  other.tool_id: other}),
    ))
    with TestClient(app) as client:
        response = client.get("/workflow-targets")
    assert response.status_code == 200
    first, second = response.json()["targets"]
    assert first["description"] == tool.description
    assert first["server_alias"] == "Knowledge MCP"
    assert first["server_id"] == "knowledge"
    assert first["target"] != second["target"]
    assert first["content_digest"] == tool.content_digest
    assert first["schema_digest"] == tool.binding.schema_digest
    assert first["tool_timeout_seconds"] == 45
    assert first["max_output_bytes"] == 8192
    assert first["input_schema"] == tool.input_schema
    assert first["tool_output_schema"] == tool.output_schema
    assert first["output_schema"]["properties"]["output"] == tool.output_schema
    assert second["tool_output_schema"] is None
    assert not {"url", "secret_ref", "dev_secret", "authorization"}.intersection(first)


def test_workflow_api_roundtrip_and_authorization(tmp_path: Path) -> None:
    app = create_app(agent_model=DemoAgentModel(), control_settings=ControlPlaneSettings(
        data_root=tmp_path / "state", admin_auth_enabled=True, admin_token="test-token",
    ))
    with TestClient(app) as client:
        base = "/v1/admin/workflow-drafts"
        assert client.get(base).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"
        response = client.post("/v1/admin/solutions", json={
            "solution_id": "demo", "title": "Demo", "owner": "tester",
        })
        assert response.status_code < 300, response.text
        generation = app.state.control_store.snapshot().active_generation_id
        payload = {"title": "Review", "solution_id": "demo", "definition": {
            "apiVersion": "sage.dev/v1beta1", "kind": "WorkflowTemplate",
            "workflowId": "review", "version": "1.0.0", "steps": [
                {"id": "echo", "kind": "skill", "target": "echo", "inheritInput": False},
            ],
        }, "design": {"positions": {"echo": {"x": 20, "y": 40}}}}
        created = client.post(base, json=payload)
        assert created.status_code == 201, created.text
        path = f"{base}/{created.json()['draft_id']}"
        assert created.headers["etag"] == '"1"'
        assert client.get(base).json()["total"] == 1
        assert client.get(path).json()["design"] == payload["design"]
        assert client.put(path, json=payload).status_code == 428
        headers = {"If-Match": '"1"'}
        assert client.post(path + "/validate", headers=headers).json()["status"] == "valid"
        exported = client.get(path + "/export", headers=headers)
        assert exported.status_code == 200, exported.text
        assert yaml.safe_load(exported.text)["workflowId"] == "review"
        imported = client.post(base + "/import", json={"content": exported.text})
        assert imported.status_code == 200, imported.text
        assert imported.json()["definition"]["workflowId"] == "review"
        assert client.post(base + "/import", json={"content": "value: &a [*a]"}).status_code == 422
        assert client.post(base + "/import", json={
            "content": "workflowId: first\nworkflowId: second",
        }).status_code == 422
        assert client.get("/v1/admin/workflow-targets").status_code == 200
        saved = client.put(path, headers=headers, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 2
        assert client.put(path, headers=headers, json=payload).status_code == 409
        assert client.post(path + "/validate", headers=headers).status_code == 409
        assert client.delete(path, headers=headers).status_code == 409
        assert client.delete(path, headers={"If-Match": '"2"'}).status_code == 204
        assert client.get(path).status_code == 404
        assert app.state.control_store.snapshot().active_generation_id == generation