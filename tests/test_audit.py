from pathlib import Path

from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.audit import SqliteAuditStore
from sage.control_settings import ControlPlaneSettings

SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def test_audit_store_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    store = SqliteAuditStore(str(database))
    created = store.append(
        actor_id="usr_1",
        workspace_id="ws_1",
        action="POST",
        resource="/v1/invocations",
        outcome="success",
        status_code=200,
        details_digest="sha256:" + "a" * 64,
    )

    restored = SqliteAuditStore(str(database)).list()

    assert restored == (created,)


def test_admin_can_query_mutation_audit_without_token_disclosure(tmp_path: Path) -> None:
    token = "highly-sensitive-admin-token"
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_auth_enabled=True,
            admin_token=token,
        ),
    )
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/solutions",
            headers=headers,
            json={
                "solution_id": "audit-solution",
                "title": "Audit Solution",
                "owner": "platform-team",
                "supported_profiles": ["direct"],
            },
        )
        audits = client.get("/v1/admin/audits", headers=headers)

    assert created.status_code == 201
    assert audits.status_code == 200
    record = next(
        item
        for item in audits.json()
        if item["resource"] == "/v1/admin/solutions"
    )
    assert record["outcome"] == "success"
    assert record["actor_id"].startswith("admin:")
    assert token not in str(audits.json())