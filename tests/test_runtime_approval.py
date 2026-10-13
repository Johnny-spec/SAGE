import asyncio
import shutil
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.digests import canonical_digest, directory_digest
from sage.domain import Principal, RuntimeContext

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}
SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


class AdminPrincipalResolver:
    def resolve(self, authorization: str | None) -> Principal:
        del authorization
        return Principal(
            workspace_id="ws_default",
            principal_id="admin-user",
            roles=frozenset({"user", "platform_admin"}),
            authentication_method="local",
        )


def test_high_risk_invocation_requires_matching_approved_human_task(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    shutil.copytree(SKILL_FIXTURES, skills_root)
    manifest_path = skills_root / "echo" / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["policy"]["risk"] = "write-high"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    app = create_app(
        skills_root,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        principal_resolver=AdminPrincipalResolver(),
    )

    with TestClient(app) as client:
        client.post(
            "/v1/admin/solutions",
            headers=ADMIN_HEADERS,
            json={
                "solution_id": "high-risk",
                "title": "High Risk",
                "owner": "platform-team",
                "allowed_roles": ["platform_admin"],
                "supported_profiles": ["direct"],
            },
        )
        release = client.post(
            "/v1/admin/solutions/high-risk/releases",
            headers=ADMIN_HEADERS,
            json={
                "version": "1.0.0",
                "components": [
                    {
                        "kind": "skill",
                        "component_id": "echo",
                        "version": "1.0.0",
                        "content_digest": directory_digest(skills_root / "echo"),
                    }
                ],
            },
        ).json()
        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Activate high-risk Solution",
                "solution_release_ids": [release["release_id"]],
            },
        ).json()
        client.post(
            f"/v1/admin/candidates/{candidate['candidate_id']}/validate",
            headers=ADMIN_HEADERS,
        )
        approval = client.post(
            f"/v1/admin/candidates/{candidate['candidate_id']}/submit",
            headers=ADMIN_HEADERS,
        ).json()
        client.post(
            f"/v1/admin/approvals/{approval['approval_id']}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "Release reviewed."},
        )
        activated = client.post(
            f"/v1/admin/candidates/{candidate['candidate_id']}/activate",
            headers=ADMIN_HEADERS,
        ).json()
        principal = AdminPrincipalResolver().resolve(None)
        parent = RuntimeContext.create(
            mode="direct",
            lifecycle="durable",
            principal=principal,
            run_id="run_parent",
            generation_id=activated["generation_id"],
            solution_id="high-risk",
            solution_release_id=release["release_id"],
            request_id="workflow-parent",
        )
        asyncio.run(client.app.state.run_store.begin(parent))

        unapproved = client.post(
            "/v1/invocations",
            json={
                "solution_id": "high-risk",
                "capability_id": "skill:echo",
                "input": {"text": "approved"},
            },
        )
        wrong_task = client.app.state.human_task_store.create(
            run_id=parent.run_id,
            workspace_id=principal.workspace_id,
            solution_id="high-risk",
            kind="approval",
            title="Approve echo",
            instructions="",
            assigned_roles=("platform_admin",),
            subject={
                "capability_id": "skill:echo",
                "input_digest": canonical_digest({"text": "other"}),
            },
        )
        client.post(
            f"/v1/human-tasks/{wrong_task.task_id}/decision",
            json={"decision": {}, "approve": True, "reason": "Reviewed"},
        )
        mismatched = client.post(
            "/v1/invocations",
            json={
                "solution_id": "high-risk",
                "capability_id": "skill:echo",
                "input": {"text": "approved"},
                "parent_run_id": parent.run_id,
                "approval_task_id": wrong_task.task_id,
            },
        )
        correct_task = client.app.state.human_task_store.create(
            run_id=parent.run_id,
            workspace_id=principal.workspace_id,
            solution_id="high-risk",
            kind="approval",
            title="Approve echo",
            instructions="",
            assigned_roles=("platform_admin",),
            subject={
                "capability_id": "skill:echo",
                "input_digest": canonical_digest({"text": "approved"}),
            },
        )
        client.post(
            f"/v1/human-tasks/{correct_task.task_id}/decision",
            json={"decision": {}, "approve": True, "reason": "Reviewed"},
        )
        approved = client.post(
            "/v1/invocations",
            json={
                "solution_id": "high-risk",
                "capability_id": "skill:echo",
                "input": {"text": "approved"},
                "parent_run_id": parent.run_id,
                "approval_task_id": correct_task.task_id,
            },
        )

    assert unapproved.status_code == 403
    assert mismatched.status_code == 403
    assert approved.status_code == 200
    assert approved.json()["output"] == {"text": "approved"}