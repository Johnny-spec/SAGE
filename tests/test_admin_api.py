from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_models import McpServerRecord, McpToolRecord
from sage.control_settings import ControlPlaneSettings
from sage.digests import directory_digest

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}
SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


class FakeMcpConnector:
    async def test(self, server: McpServerRecord) -> dict[str, str]:
        return {"name": server.alias, "version": "1.0", "protocol_version": "test"}

    async def discover(self, server: McpServerRecord) -> tuple[McpToolRecord, ...]:
        return (
            McpToolRecord(
                name="search",
                description="Search official documentation",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                output_schema={"type": "object"},
                schema_digest="sha256:search-v1",
            ),
        )

    async def invoke(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {"answer": f"{server.alias}/{tool_name}:{arguments.get('query')}"}


def _python_skill_zip() -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "uploaded-python/SKILL.md",
            "---\nname: uploaded-python\ndescription: Uploaded Python example.\n---\n",
        )
        archive.writestr(
            "uploaded-python/skill.yaml",
            """apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata: {name: uploaded-python, version: 1.0.0}
spec:
  execution: {type: python-subprocess, entrypoint: main.py}
  inputSchema: {type: object}
  outputSchema: {type: object}
""",
        )
        archive.writestr("uploaded-python/main.py", "print('{}')\n")
    return stream.getvalue()


def test_development_quick_publish_and_status_guard(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        draft = client.post(
            "/v1/admin/agents",
            json={
                "agent_id": "quick-agent",
                "name": "Quick",
                "version": "1.0.0",
                "instructions": "Use echo.",
                "capability_ids": ["skill:echo"],
            },
        ).json()
        assert (
            client.post(f"/v1/admin/agents/{draft['draft_id']}/validate").json()["status"]
            == "valid"
        )
        candidate = client.post(
            "/v1/admin/candidates",
            json={
                "title": "Quick publish",
                "agent_draft_ids": [draft["draft_id"]],
                "skill_package_ids": [],
                "default_agent_id": "quick-agent",
            },
        ).json()
        path = f"/v1/admin/candidates/{candidate['candidate_id']}"
        published = client.post(f"{path}/publish")
        assert published.status_code == 200, published.text
        assert (
            client.post(f"{path}/publish").json()["generation_id"]
            == published.json()["generation_id"]
        )
        assert client.post(f"{path}/validate").status_code == 409
        approvals = client.get("/v1/admin/approvals").json()
        assert approvals == []
        app.state.control_settings = app.state.control_settings.model_copy(
            update={"environment": "production"}
        )
        assert client.post(f"{path}/publish").status_code == 403


def test_published_agent_preserves_validated_digest_for_later_solution_release(tmp_path: Path):
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path),
    )
    with TestClient(app) as client:
        draft = client.post("/v1/admin/agents", json={
            "agent_id": "echo", "name": "Echo", "version": "1.0.0",
            "instructions": "Use echo.", "capability_ids": ["skill:echo"],
        }).json()
        validated = client.post(f"/v1/admin/agents/{draft['draft_id']}/validate").json()
        candidate = client.post("/v1/admin/candidates", json={
            "title": "Echo", "agent_draft_ids": [draft["draft_id"]],
        }).json()
        response = client.post(f"/v1/admin/candidates/{candidate['candidate_id']}/publish")
        assert response.status_code == 200, response.text
        generation = response.json()
        assert generation["agent_definitions"]["echo"] == validated["definition"]
        assert client.post("/v1/admin/solutions", json={
            "solution_id": "echo", "title": "Echo", "owner": "tester",
        }).is_success
        release = client.post("/v1/admin/solutions/echo/releases", json={
            "version": "1.0.0", "components": [
                {"kind": "agent", "component_id": "echo", "version": "1.0.0",
                 "content_digest": validated["validation"]["content_digest"]},
                {"kind": "skill", "component_id": "echo",
                 "version": generation["skill_versions"]["echo"],
                 "content_digest": generation["skill_digests"]["echo"]},
            ],
        })
        assert release.is_success, release.text
        candidate = client.post("/v1/admin/candidates", json={
            "title": "Echo release", "solution_release_ids": [release.json()["release_id"]],
        }).json()
        response = client.post(f"/v1/admin/candidates/{candidate['candidate_id']}/publish")
        assert response.status_code == 200, response.text
        assert response.json()["solution_releases"]["echo"] == "echo@1.0.0"


def test_admin_control_plane_end_to_end(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
            mcp_allowed_hosts=frozenset({"mcp.example"}),
        ),
        mcp_connector=FakeMcpConnector(),
    )

    with TestClient(app) as client:
        admin_page = client.get("/admin")
        assert admin_page.status_code == 200
        assert 'id="agentForm"' in admin_page.text
        assert 'id="skillUploadForm"' in admin_page.text
        assert 'id="mcpForm"' in admin_page.text
        assert 'id="candidateForm"' in admin_page.text
        bootstrap = client.get("/v1/control/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.headers["cache-control"] == "no-store"
        assert bootstrap.json() == {
            "environment": "development",
            "admin_auth_enabled": False,
            "auto_admin": False,
            "admin_token": None,
        }
        assert client.get("/v1/admin/status").status_code == 200
        assert client.get("/v1/admin/status", headers=ADMIN_HEADERS).status_code == 200

        agent = client.post(
            "/v1/admin/agents",
            headers=ADMIN_HEADERS,
            json={
                "agent_id": "support-agent",
                "name": "Support Agent",
                "version": "1.0.0",
                "instructions": "Use the selected governed Skills.",
                "capability_ids": ["skill:echo"],
                "subagents": [],
                "memory_policy": "thread",
                "max_steps": 20,
            },
        )
        assert agent.status_code == 201
        agent_id = agent.json()["draft_id"]
        validated_agent = client.post(
            f"/v1/admin/agents/{agent_id}/validate",
            headers=ADMIN_HEADERS,
        )
        assert validated_agent.json()["status"] == "valid"

        uploaded = client.post(
            "/v1/admin/skills/uploads",
            headers=ADMIN_HEADERS,
            files={"file": ("uploaded-python.zip", _python_skill_zip(), "application/zip")},
        )
        assert uploaded.status_code == 201
        package_id = uploaded.json()["package_id"]
        validated_skill = client.post(
            f"/v1/admin/skills/{package_id}/validate",
            headers=ADMIN_HEADERS,
        )
        assert validated_skill.json()["status"] == "valid"
        assert validated_skill.json()["executable"] is False
        assert (
            client.get(
                f"/v1/admin/skills/{package_id}/download/original",
                headers=ADMIN_HEADERS,
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/v1/admin/skills/{package_id}/download/normalized",
                headers=ADMIN_HEADERS,
            ).status_code
            == 200
        )

        server = client.post(
            "/v1/admin/mcp/servers",
            headers=ADMIN_HEADERS,
            json={
                "alias": "learn",
                "transport": "streamable-http",
                "url": "https://mcp.example/mcp",
            },
        )
        assert server.status_code == 201
        assert "dev_secret" not in server.json()
        server_id = server.json()["server_id"]
        assert (
            client.post(
                f"/v1/admin/mcp/servers/{server_id}/test",
                headers=ADMIN_HEADERS,
            ).status_code
            == 200
        )
        discovered = client.post(
            f"/v1/admin/mcp/servers/{server_id}/discover",
            headers=ADMIN_HEADERS,
        )
        assert discovered.status_code == 200
        assert discovered.json()["tools"][0]["name"] == "search"
        imported = client.post(
            f"/v1/admin/mcp/servers/{server_id}/tools/search/import",
            headers=ADMIN_HEADERS,
            json={
                "skill_name": "learn-search",
                "version": "1.0.0",
                "description": "Search official documentation.",
                "instructions": "Use for current official product documentation.",
                "risk": "read-only",
            },
        )
        assert imported.status_code == 201
        mcp_package_id = imported.json()["package_id"]

        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Publish control-plane resources",
                "description": "Agent, disabled Python package, and MCP Skill.",
                "agent_draft_ids": [agent_id],
                "skill_package_ids": [package_id, mcp_package_id],
            },
        )
        assert candidate.status_code == 201
        candidate_id = candidate.json()["candidate_id"]
        validated = client.post(
            f"/v1/admin/candidates/{candidate_id}/validate",
            headers=ADMIN_HEADERS,
        )
        assert validated.json()["status"] == "valid"
        approval = client.post(
            f"/v1/admin/candidates/{candidate_id}/submit",
            headers=ADMIN_HEADERS,
        )
        approval_id = approval.json()["approval_id"]
        approved = client.post(
            f"/v1/admin/approvals/{approval_id}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "Reviewed all validation evidence."},
        )
        assert approved.json()["status"] == "approved"
        activated = client.post(
            f"/v1/admin/candidates/{candidate_id}/activate",
            headers=ADMIN_HEADERS,
        )
        assert activated.status_code == 200
        assert activated.json()["generation_id"] != "gen_startup"

        agents = {item["agent_id"] for item in client.get("/v1/agents").json()}
        skills = {item["name"] for item in client.get("/v1/skills").json()}
        assert "support-agent" in agents
        assert "learn-search" in skills
        assert "uploaded-python" not in skills

        mcp_result = client.post(
            "/v1/messages",
            json={
                "message": "search",
                "skill": "learn-search",
                "arguments": {"query": "SAGE"},
            },
        )
        assert mcp_result.status_code == 200
        assert mcp_result.json()["result"]["answer"] == "learn/search:SAGE"

        rollback_request = client.post(
            "/v1/admin/generations/gen_startup/rollback-request",
            headers=ADMIN_HEADERS,
        )
        assert rollback_request.status_code == 200
        rollback_approval_id = rollback_request.json()["approval_id"]
        rollback_approval = client.post(
            f"/v1/admin/approvals/{rollback_approval_id}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "Restore the verified startup generation."},
        )
        assert rollback_approval.json()["status"] == "approved"
        rolled_back = client.post(
            f"/v1/admin/approvals/{rollback_approval_id}/rollback",
            headers=ADMIN_HEADERS,
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["generation_id"] == "gen_startup"
        assert "support-agent" not in {item["agent_id"] for item in client.get("/v1/agents").json()}


def test_control_plane_draft_survives_application_restart(tmp_path: Path) -> None:
    settings = ControlPlaneSettings(
        data_root=tmp_path / "state",
        admin_token="test-admin-token",
    )
    payload = {
        "agent_id": "persistent-agent",
        "name": "Persistent Agent",
        "version": "1.0.0",
        "instructions": "Use the echo capability.",
        "capability_ids": ["skill:echo"],
        "subagents": [],
        "memory_policy": "thread",
        "max_steps": 10,
    }
    first_app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=settings,
        mcp_connector=FakeMcpConnector(),
    )
    with TestClient(first_app) as client:
        created = client.post(
            "/v1/admin/agents",
            headers=ADMIN_HEADERS,
            json=payload,
        )
        assert created.status_code == 201
        draft_id = created.json()["draft_id"]

    second_app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=settings,
        mcp_connector=FakeMcpConnector(),
    )
    with TestClient(second_app) as client:
        drafts = client.get("/v1/admin/agents", headers=ADMIN_HEADERS).json()

    assert any(item["draft_id"] == draft_id for item in drafts)


def test_admin_can_create_solution_release_lock(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        mcp_connector=FakeMcpConnector(),
    )
    digest = "sha256:" + "b" * 64

    with TestClient(app) as client:
        inactive = client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "inactive-agent",
                "title": "Inactive Agent",
                "owner": "platform-team",
                "entry_agent_id": "missing-agent",
                "supported_profiles": ["direct"],
            },
        )
        solution = client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "incident-diagnostics",
                "title": "Incident diagnostics",
                "description": "Governed IIS and Edge diagnostics.",
                "owner": "platform-team",
                "entry_agent_id": "diagnostic-agent",
                "supported_profiles": ["direct", "interactive"],
            },
        )
        release = client.post(
            "/v1/admin/solutions/incident-diagnostics/releases",
            headers=ADMIN_HEADERS,
            json={
                "version": "1.0.0",
                "source_generation_id": "gen_startup",
                "components": [
                    {
                        "kind": "agent",
                        "component_id": "diagnostic-agent",
                        "version": "1.0.0",
                        "content_digest": digest,
                    },
                    {
                        "kind": "skill",
                        "component_id": "echo",
                        "version": "1.0.0",
                        "content_digest": digest,
                    },
                ],
            },
        )
        releases = client.get(
            "/v1/admin/solutions/incident-diagnostics/releases",
            headers=ADMIN_HEADERS,
        )
        status = client.get("/v1/admin/status", headers=ADMIN_HEADERS)

    assert inactive.status_code == 400
    assert inactive.json()["detail"] == "Entry Agent is not active: missing-agent"
    assert solution.status_code == 201
    assert release.status_code == 201
    assert release.json()["release_id"] == "incident-diagnostics@1.0.0"
    assert release.json()["content_digest"] != digest
    assert releases.json() == [release.json()]
    assert status.json()["solutions"] == 1
    assert status.json()["solution_releases"] == 1


def test_admin_can_import_solution_bundle_manifest(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        mcp_connector=FakeMcpConnector(),
    )
    digest = "sha256:" + "d" * 64
    manifest = f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: news-intelligence, version: 1.0.0, title: News Intelligence}}
spec:
  owner: news-team
  allowedRoles: [news-reader]
  supportedProfiles: [durable, scheduled]
  components:
    - kind: workflow
      id: build-news-brief
      version: 1.0.0
      contentDigest: {digest}
"""

    with TestClient(app) as client:
        imported = client.post(
            "/v1/admin/solutions/imports",
            headers=ADMIN_HEADERS,
            files={"file": ("solution.yaml", manifest.encode(), "application/yaml")},
        )
        releases = client.get(
            "/v1/admin/solution-releases",
            headers=ADMIN_HEADERS,
        )

    assert imported.status_code == 201
    assert imported.json()["solution"]["solution_id"] == "news-intelligence"
    assert imported.json()["release"]["allowed_roles"] == ["news-reader"]
    assert imported.json()["release"]["supported_profiles"] == ["durable", "scheduled"]
    assert [item["release_id"] for item in releases.json()] == ["news-intelligence@1.0.0"]


def test_admin_records_locked_release_evaluation(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        mcp_connector=FakeMcpConnector(),
    )
    digest = "sha256:" + "e" * 64

    with TestClient(app) as client:
        client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "evaluated-solution",
                "title": "Evaluated Solution",
                "owner": "platform-team",
                "supported_profiles": ["direct"],
            },
        )
        release = client.post(
            "/v1/admin/solutions/evaluated-solution/releases",
            headers=ADMIN_HEADERS,
            json={
                "version": "1.0.0",
                "components": [
                    {
                        "kind": "evaluation-suite",
                        "component_id": "conformance",
                        "version": "1.0.0",
                        "content_digest": digest,
                    }
                ],
            },
        )
        report = client.post(
            f"/v1/admin/solution-releases/{release.json()['release_id']}/evaluations",
            headers=ADMIN_HEADERS,
            json={
                "suite_id": "conformance",
                "status": "passed",
                "metrics": {"success_rate": 1.0},
                "summary": "Passed.",
            },
        )
        reports = client.get(
            f"/v1/admin/solution-releases/{release.json()['release_id']}/evaluations",
            headers=ADMIN_HEADERS,
        )

    assert report.status_code == 201
    assert report.json()["release_digest"] == release.json()["content_digest"]
    assert reports.json() == [report.json()]


def test_active_solution_scopes_public_catalog_and_execution(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        mcp_connector=FakeMcpConnector(),
    )

    with TestClient(app) as client:
        solution = client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "echo-solution",
                "title": "Echo Solution",
                "description": "A deterministic reference Solution.",
                "owner": "platform-team",
                "supported_profiles": ["direct"],
            },
        )
        assert solution.status_code == 201
        release = client.post(
            "/v1/admin/solutions/echo-solution/releases",
            headers=ADMIN_HEADERS,
            json={
                "version": "1.0.0",
                "source_generation_id": "gen_startup",
                "components": [
                    {
                        "kind": "skill",
                        "component_id": "echo",
                        "version": "1.0.0",
                        "content_digest": directory_digest(SKILL_FIXTURES / "echo"),
                    }
                ],
            },
        )
        assert release.status_code == 201
        release_id = release.json()["release_id"]
        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Activate Echo Solution",
                "solution_release_ids": [release_id],
            },
        )
        assert candidate.status_code == 201
        candidate_id = candidate.json()["candidate_id"]
        validated = client.post(
            f"/v1/admin/candidates/{candidate_id}/validate",
            headers=ADMIN_HEADERS,
        )
        assert validated.json()["status"] == "valid"
        approval = client.post(
            f"/v1/admin/candidates/{candidate_id}/submit",
            headers=ADMIN_HEADERS,
        )
        approval_id = approval.json()["approval_id"]
        client.post(
            f"/v1/admin/approvals/{approval_id}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "Reference Solution reviewed."},
        )
        activated = client.post(
            f"/v1/admin/candidates/{candidate_id}/activate",
            headers=ADMIN_HEADERS,
        )
        solutions = client.get("/v1/solutions")
        scoped_skills = client.get("/v1/skills?solution_id=echo-solution")
        scoped_agents = client.get("/v1/agents?solution_id=echo-solution")
        unscoped_skills = client.get("/v1/skills")
        unscoped_run = client.post("/v1/messages", json={"message": "hello"})
        scoped_run = client.post(
            "/v1/messages",
            json={
                "solution_id": "echo-solution",
                "message": "hello",
                "skill": "echo",
                "arguments": {"text": "hello"},
            },
        )
        saved_memory = client.put(
            "/v1/memories",
            json={
                "solution_id": "echo-solution",
                "key": "output-style",
                "value": "concise",
            },
        )
        memories = client.get("/v1/memories?solution_id=echo-solution")
        human_task = client.app.state.human_task_store.create(
            run_id=scoped_run.json()["run_id"],
            workspace_id="ws_default",
            solution_id="echo-solution",
            kind="input",
            title="Confirm output format",
            instructions="Select the desired format.",
            assigned_roles=("user",),
        )
        human_tasks = client.get("/v1/human-tasks?solution_id=echo-solution")
        submitted_task = client.post(
            f"/v1/human-tasks/{human_task.task_id}/decision",
            json={"decision": {"format": "text"}, "reason": "Selected by user"},
        )
        deleted_memory = client.delete(
            f"/v1/memories/{saved_memory.json()['memory_id']}?solution_id=echo-solution"
        )

    assert activated.status_code == 200
    assert activated.json()["solution_releases"] == {"echo-solution": release_id}
    assert [item["solution_id"] for item in solutions.json()] == ["echo-solution"]
    assert [item["name"] for item in scoped_skills.json()] == ["echo"]
    assert scoped_agents.json() == []
    assert unscoped_skills.status_code == 404
    assert unscoped_run.status_code == 404
    assert scoped_run.status_code == 200
    assert scoped_run.json()["result"] == {"text": "hello"}
    assert saved_memory.status_code == 200
    assert [(item["key"], item["value"]) for item in memories.json()] == [
        ("output-style", "concise")
    ]
    assert deleted_memory.status_code == 204
    assert [item["task_id"] for item in human_tasks.json()] == [human_task.task_id]
    assert submitted_task.status_code == 200
    assert submitted_task.json()["status"] == "submitted"


def test_admin_authentication_can_be_reenabled_without_configured_token(
    tmp_path: Path,
) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
        ),
        mcp_connector=FakeMcpConnector(),
    )

    with TestClient(app) as client:
        bootstrap = client.get("/v1/control/bootstrap")
        response = client.get(
            "/v1/admin/status",
            headers={"Authorization": "Bearer any-value"},
        )

    assert bootstrap.json() == {
        "environment": "production",
        "admin_auth_enabled": True,
        "auto_admin": False,
        "admin_token": None,
    }
    assert response.status_code == 503
    assert "SAGE_ADMIN_TOKEN" in response.json()["detail"]
