from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.solution_packages import load_solution_bundle
from sage.supply_chain import (
    SolutionTrustStore,
    SupplyChainError,
    sign_solution_package,
)


def _solution_package(root: Path) -> None:
    digest = "sha256:" + "a" * 64
    (root / "solution.yaml").write_text(
        f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: signed-solution, version: 1.0.0, title: Signed}}
spec:
  owner: platform-team
  supportedProfiles: [direct]
  components:
    - kind: skill
      id: echo
      version: 1.0.0
      contentDigest: {digest}
""",
        encoding="utf-8",
    )
    (root / "sbom.json").write_text(
        '{"bomFormat":"CycloneDX","specVersion":"1.6","components":[]}',
        encoding="utf-8",
    )
    (root / "provenance.json").write_text(
        """{
  "builderId": "https://ci.example/sage",
  "sourceUri": "https://git.example/platform/solutions",
  "sourceDigest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "buildType": "sage-solution-v1"
}...""".replace("}...", "}"),
        encoding="utf-8",
    )


def _keys(root: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    private_path = root / "private.pem"
    trust_root = root / "trust"
    trust_root.mkdir()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (trust_root / "platform.pem").write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, trust_root


def test_signed_solution_verifies_sbom_provenance_and_trust(tmp_path: Path) -> None:
    _solution_package(tmp_path)
    private_key, trust_root = _keys(tmp_path)
    sign_solution_package(tmp_path, private_key_path=private_key, key_id="platform")

    verification = SolutionTrustStore(trust_root).verify(
        tmp_path,
        load_solution_bundle(tmp_path),
        signature_required=True,
    )

    assert verification.signature_verified is True
    assert verification.signature_key_id == "platform"
    assert verification.sbom_digest is not None
    assert verification.provenance_digest is not None


def test_signed_solution_rejects_post_signing_tamper(tmp_path: Path) -> None:
    _solution_package(tmp_path)
    private_key, trust_root = _keys(tmp_path)
    sign_solution_package(tmp_path, private_key_path=private_key, key_id="platform")
    (tmp_path / "provenance.json").write_text(
        (tmp_path / "provenance.json").read_text(encoding="utf-8").replace(
            "sage-solution-v1",
            "tampered-build",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SupplyChainError, match="payload digest mismatch"):
        SolutionTrustStore(trust_root).verify(
            tmp_path,
            load_solution_bundle(tmp_path),
            signature_required=True,
        )


def test_production_rejects_unsigned_manifest_import(tmp_path: Path) -> None:
    skills = Path(__file__).parent / "fixtures" / "skills"
    app = create_app(
        skills,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
            admin_token="admin-token",
            require_content_scanning=False,
        ),
    )
    manifest = b"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {name: unsigned, version: 1.0.0, title: Unsigned}
spec:
  owner: team
  components:
    - kind: skill
      id: echo
      version: 1.0.0
      contentDigest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/solutions/imports",
            headers={"Authorization": "Bearer admin-token"},
            files={"file": ("solution.yaml", manifest, "application/yaml")},
        )

    assert response.status_code == 400
    assert "signed ZIP" in response.json()["detail"]