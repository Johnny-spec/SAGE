import io
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.scanning import AllowAllContentScanner, RejectingContentScanner
from sage.solution_packages import write_solution_bundle_zip
from sage.supply_chain import sign_solution_package


def make_draft(client):
    response = client.post(
        "/v1/admin/solutions",
        json={
            "solution_id": "studio",
            "title": "Studio",
            "owner": "tester",
            "supported_profiles": ["durable"],
        },
    )
    assert response.status_code < 300, response.text
    response = client.post(
        "/v1/admin/workflow-drafts",
        json={
            "title": "Echo",
            "solution_id": "studio",
            "definition": {
                "apiVersion": "sage.dev/v1beta1",
                "kind": "WorkflowTemplate",
                "workflowId": "echo-flow",
                "version": "1.0.0",
                "steps": [
                    {"id": "echo", "kind": "skill", "target": "echo", "input": {"text": "hi"}},
                ],
            },
        },
    )
    assert response.status_code == 201, response.text
    return "/v1/admin/workflow-drafts/" + response.json()["draft_id"]


def test_build_release_candidate_replay_and_publish(tmp_path: Path) -> None:
    app = create_app(
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            admin_auth_enabled=False,
        ),
    )
    with TestClient(app) as client:
        path = make_draft(client)
        headers = {"If-Match": '"1"'}
        preview = client.post(path + "/resolve", json={"version": "1.0.0"}, headers=headers)
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        request = {"version": "1.0.0", "build_digest": preview["build_digest"]}
        assert client.post(path + "/bundle", json=request, headers=headers).content[:2] == b"PK"
        built = client.post(path + "/build", json=request, headers=headers)
        assert built.status_code == 200, built.text
        result = built.json()
        assert result["candidate"]["status"] == "valid", result
        assert app.state.control_store.snapshot().active_generation_id == preview["generation_id"]
        replay = client.post(path + "/build", json=request, headers=headers).json()
        assert replay["replayed"]
        assert replay["candidate"]["candidate_id"] == result["candidate"]["candidate_id"]
        assert len(app.state.control_store.snapshot().solution_releases) == 1
        assert len(list((tmp_path / "artifacts" / "solutions").iterdir())) == 1
        candidate_path = "/v1/admin/candidates/" + result["candidate"]["candidate_id"]
        published = client.post(candidate_path + "/publish")
        assert published.status_code == 200, published.text
        assert app.state.control_store.snapshot().active_generation_id != preview["generation_id"]
        assert client.post(path + "/build", json=request, headers=headers).status_code == 409


def test_build_security_and_stale_review(tmp_path: Path) -> None:
    app = create_app(
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            admin_auth_enabled=True,
            admin_token="test-token",
        ),
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/admin/workflow-drafts/absent/resolve",
                json={
                    "version": "1.0.0",
                },
            ).status_code
            == 401
        )
        client.headers["Authorization"] = "Bearer test-token"
        path = make_draft(client)
        headers = {"If-Match": '"1"'}
        preview = client.post(path + "/resolve", json={"version": "1.0.0"}, headers=headers).json()
        request = {"version": "1.0.0", "build_digest": preview["build_digest"]}
        app.state.solution_bundle_packages._signature_required = True
        blocked = client.post(path + "/build", json=request, headers=headers)
        assert blocked.status_code == 422, blocked.text
        assert not app.state.control_store.snapshot().solution_releases
        assert not list((tmp_path / "artifacts" / "solutions").iterdir())
        assert client.post(path + "/build", json=request).status_code == 428
        draft = client.get(path).json()
        draft = {key: draft[key] for key in ("title", "solution_id", "definition", "design")}
        client.put(path, json=draft, headers=headers)
        assert client.post(path + "/bundle", json=request, headers=headers).status_code == 409


def test_build_scan_race_and_commit_rollback(tmp_path: Path, monkeypatch) -> None:
    app = create_app(
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            admin_auth_enabled=False,
        ),
    )
    with TestClient(app) as client:
        path = make_draft(client)
        headers = {"If-Match": '"1"'}
        preview = client.post(path + "/resolve", json={"version": "1.0.0"}, headers=headers).json()
        payload = {"version": "1.0.0", "build_digest": preview["build_digest"]}
        packages = app.state.solution_bundle_packages
        packages._content_scanner = RejectingContentScanner()
        assert client.post(path + "/build", json=payload, headers=headers).status_code == 422
        packages._content_scanner = AllowAllContentScanner()

        def fail_candidate(*args, **kwargs):
            raise ValueError("candidate failure")

        with monkeypatch.context() as patch:
            patch.setattr(app.state.catalog_manager, "create_candidate", fail_candidate)
            failed = client.post(path + "/build", json=payload, headers=headers)
            assert failed.status_code == 422, failed.text
        assert not app.state.control_store.snapshot().solution_releases

        class ChangingScanner:
            def require_clean(self, archive):
                def mutate(state):
                    state.workflow_drafts[path.rsplit("/", 1)[1]].revision += 1

                app.state.control_store.update(mutate)

        packages._content_scanner = ChangingScanner()
        assert client.post(path + "/build", json=payload, headers=headers).status_code == 409
        assert not app.state.control_store.snapshot().solution_releases
        assert not app.state.control_store.snapshot().candidates
        assert not list((tmp_path / "artifacts" / "solutions").iterdir())


def test_signed_build_preserves_production_publication_gate(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "test.pem").write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    app = create_app(
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
            admin_token="test-token",
            require_content_scanning=False,
            solution_trust_store=trust,
        ),
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-token"
        path = make_draft(client)
        headers = {"If-Match": '"1"'}
        preview = client.post(path + "/resolve", json={"version": "1.0.0"}, headers=headers).json()
        assert preview["signature_required"]
        payload = {"version": "1.0.0", "build_digest": preview["build_digest"]}
        bundle = client.post(path + "/bundle", json=payload, headers=headers).content
        root = tmp_path / "bundle"
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            archive.extractall(root)
        sign_solution_package(root, private_key_path=key_path, key_id="test")
        signed = tmp_path / "signed.zip"
        write_solution_bundle_zip(root, signed)
        built = client.post(
            path + "/build-signed",
            data=payload,
            headers=headers,
            files={"file": ("signed.zip", signed.read_bytes(), "application/zip")},
        )
        assert built.status_code == 200, built.text
        result = built.json()
        assert result["release"]["package_metadata"]["signature_verified"] == "true"
        assert result["release"]["package_metadata"]["sbom_digest"].startswith("sha256:")
        assert result["candidate"]["status"] == "valid", result
        candidate_path = "/v1/admin/candidates/" + result["candidate"]["candidate_id"]
        assert client.post(candidate_path + "/publish").status_code == 403
        assert client.post(candidate_path + "/submit").status_code == 200
        assert app.state.control_store.snapshot().active_generation_id == preview["generation_id"]
