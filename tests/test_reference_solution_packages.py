from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from fastapi.testclient import TestClient

from sage.agent_definitions import default_diagnostic_agent
from sage.agent_drafts import agent_to_dict
from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_settings import ControlPlaneSettings
from sage.digests import canonical_digest, directory_digest, file_digest
from sage.domain import Principal
from sage.solution_packages import load_solution_bundle
from sage.triggers import TriggerDefinition
from sage.ui_schema import SolutionUiSchema
from sage.workflows import load_workflow_template

ROOT = Path(__file__).parents[1]
SOLUTIONS_ROOT = ROOT / "solutions"
SKILLS_ROOT = ROOT / "skills"
ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}

COMPONENT_FILES = {
    ("incident-diagnostics", "incident-diagnostics-ui"): "ui.yaml",
    ("enterprise-qa", "enterprise-qa-ui"): "ui.yaml",
    ("news-intelligence", "build-news-brief"): "workflow.yaml",
    ("news-intelligence", "daily-news"): "trigger.yaml",
    ("news-intelligence", "news-intelligence-ui"): "ui.yaml",
    ("contract-review", "review-contract"): "workflow.yaml",
    ("contract-review", "contract-review-ui"): "ui.yaml",
    ("contract-review", "contract-review-conformance"): "evaluation.yaml",
}


def _solution_zip(solution_id: str) -> bytes:
    root = SOLUTIONS_ROOT / solution_id
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            archive.writestr(path.relative_to(root).as_posix(), path.read_bytes())
    return stream.getvalue()


def test_reference_solution_component_locks_match_repository_content() -> None:
    manifests = [
        load_solution_bundle(path)
        for path in sorted(SOLUTIONS_ROOT.iterdir())
        if path.is_dir()
    ]

    assert {manifest.metadata.name for manifest in manifests} == {
        "incident-diagnostics",
        "enterprise-qa",
        "news-intelligence",
        "contract-review",
    }
    for manifest in manifests:
        solution_root = SOLUTIONS_ROOT / manifest.metadata.name
        for component in manifest.spec.components:
            if component.kind == "skill":
                actual_digest = directory_digest(SKILLS_ROOT / component.component_id)
            elif component.kind == "agent":
                assert component.component_id == "diagnostic-agent"
                actual_digest = canonical_digest(agent_to_dict(default_diagnostic_agent()))
            else:
                filename = COMPONENT_FILES[
                    (manifest.metadata.name, component.component_id)
                ]
                actual_digest = file_digest(solution_root / filename)
            assert component.content_digest == actual_digest


def test_reference_solution_workflow_trigger_and_ui_assets_are_valid() -> None:
    for solution_id in ("news-intelligence", "contract-review"):
        workflow = load_workflow_template(SOLUTIONS_ROOT / solution_id / "workflow.yaml")
        assert workflow.version == "1.0.0"

    trigger = TriggerDefinition.model_validate(
        yaml.safe_load(
            (SOLUTIONS_ROOT / "news-intelligence" / "trigger.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    assert trigger.workflow_type == "build-news-brief"

    for solution_id in (
        "incident-diagnostics",
        "enterprise-qa",
        "news-intelligence",
        "contract-review",
    ):
        schema = SolutionUiSchema.model_validate(
            yaml.safe_load(
                (SOLUTIONS_ROOT / solution_id / "ui.yaml").read_text(encoding="utf-8")
            )
        )
        assert schema.solution_id == solution_id


class ReferencePrincipalResolver:
    def resolve(self, authorization: str | None) -> Principal:
        del authorization
        return Principal(
            workspace_id="ws_reference",
            principal_id="reference-user",
            roles=frozenset(
                {
                    "user",
                    "incident-reader",
                    "knowledge-reader",
                    "news-reader",
                    "legal-reviewer",
                }
            ),
            authentication_method="local",
        )


def test_all_reference_solutions_import_activate_and_execute_without_kernel_changes(
    tmp_path: Path,
) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
        principal_resolver=ReferencePrincipalResolver(),
    )
    release_ids: list[str] = []

    with TestClient(app) as client:
        generation = client.get(
            "/v1/admin/generations",
            headers=ADMIN_HEADERS,
        ).json()[0]
        incident_manifest = load_solution_bundle(
            SOLUTIONS_ROOT / "incident-diagnostics"
        )
        locked_iis = next(
            component
            for component in incident_manifest.spec.components
            if component.kind == "skill" and component.component_id == "iis-logs"
        )
        assert generation["skill_digests"]["iis-logs"] == locked_iis.content_digest
        assert generation["skill_versions"]["iis-logs"] == locked_iis.version
        for solution_id in (
            "incident-diagnostics",
            "enterprise-qa",
            "news-intelligence",
            "contract-review",
        ):
            imported = client.post(
                "/v1/admin/solutions/imports",
                headers=ADMIN_HEADERS,
                files={
                    "file": (
                        f"{solution_id}.zip",
                        _solution_zip(solution_id),
                        "application/zip",
                    )
                },
            )
            assert imported.status_code == 201
            release_ids.append(imported.json()["release"]["release_id"])

        evaluation = client.post(
            "/v1/admin/solution-releases/contract-review@1.0.0/evaluations",
            headers=ADMIN_HEADERS,
            json={
                "suite_id": "contract-review-conformance",
                "status": "passed",
                "metrics": {"citation_integrity": 1.0},
                "summary": "Reference conformance passed.",
            },
        )
        assert evaluation.status_code == 201
        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Activate reference Solutions",
                "solution_release_ids": release_ids,
            },
        )
        candidate_id = candidate.json()["candidate_id"]
        validated = client.post(
            f"/v1/admin/candidates/{candidate_id}/validate",
            headers=ADMIN_HEADERS,
        )
        assert validated.json()["status"] == "valid", validated.json()["validation"][
            "issues"
        ]
        approval = client.post(
            f"/v1/admin/candidates/{candidate_id}/submit",
            headers=ADMIN_HEADERS,
        )
        client.post(
            f"/v1/admin/approvals/{approval.json()['approval_id']}/approve",
            headers=ADMIN_HEADERS,
            json={"reason": "All reference packages and evaluations reviewed."},
        )
        activated = client.post(
            f"/v1/admin/candidates/{candidate_id}/activate",
            headers=ADMIN_HEADERS,
        )
        visible = client.get("/v1/solutions")
        incident = client.post(
            "/v1/invocations",
            json={
                "solution_id": "incident-diagnostics",
                "capability_id": "skill:echo",
                "input": {"text": "healthy"},
            },
        )
        enterprise = client.post(
            "/v1/invocations",
            json={
                "solution_id": "enterprise-qa",
                "capability_id": "skill:enterprise-qa",
                "input": {
                    "query": "retention",
                    "documents": [
                        {"id": "policy", "text": "Retention is seven years."}
                    ],
                },
            },
        )
        news = client.post(
            "/v1/invocations",
            json={
                "solution_id": "news-intelligence",
                "capability_id": "skill:news-intelligence",
                "input": {
                    "articles": [
                        {"title": "Headline", "url": "https://news.example/item"}
                    ]
                },
            },
        )
        contract = client.post(
            "/v1/invocations",
            json={
                "solution_id": "contract-review",
                "capability_id": "skill:contract-review",
                "input": {"contract_text": "甲方向乙方交付服务。"},
            },
        )
        contract_ui = client.get("/v1/solutions/contract-review/ui-schema")

    assert activated.status_code == 200
    assert {item["solution_id"] for item in visible.json()} == {
        "incident-diagnostics",
        "enterprise-qa",
        "news-intelligence",
        "contract-review",
    }
    assert incident.json()["output"] == {"text": "healthy"}
    assert enterprise.json()["output"]["status"] == "answered"
    assert news.json()["output"]["story_count"] == 1
    assert contract.json()["output"]["status"] == "needs-review"
    assert contract_ui.status_code == 200
    assert contract_ui.json()["solution_id"] == "contract-review"


def test_candidate_rejects_tampered_solution_package_asset(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="test-admin-token",
        ),
    )

    with TestClient(app) as client:
        imported = client.post(
            "/v1/admin/solutions/imports",
            headers=ADMIN_HEADERS,
            files={
                "file": (
                    "enterprise-qa.zip",
                    _solution_zip("enterprise-qa"),
                    "application/zip",
                )
            },
        )
        release = imported.json()["release"]
        package_root = client.app.state.control_store.resolve_path(
            release["package_relpath"]
        )
        (package_root / "ui.yaml").write_text("changed: true\n", encoding="utf-8")
        candidate = client.post(
            "/v1/admin/candidates",
            headers=ADMIN_HEADERS,
            json={
                "title": "Tampered package",
                "solution_release_ids": [release["release_id"]],
            },
        )
        validated = client.post(
            f"/v1/admin/candidates/{candidate.json()['candidate_id']}/validate",
            headers=ADMIN_HEADERS,
        )

    assert validated.json()["status"] == "invalid"
    assert any(
        issue["code"] == "solution_asset_changed"
        for issue in validated.json()["validation"]["issues"]
    )