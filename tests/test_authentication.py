from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.authentication import AuthenticationError, EntraPrincipalResolver
from sage.control_settings import ControlPlaneSettings
from sage.domain import Principal

SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


class StaticJwksClient:
    def __init__(self, key: object) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> object:
        del token
        return SimpleNamespace(key=self._key)


def _token(private_key: object, **updates: object) -> str:
    claims: dict[str, object] = {
        "iss": "https://login.microsoftonline.com/tenant-id/v2.0",
        "aud": "api-client-id",
        "tid": "tenant-id",
        "oid": "user-id",
        "roles": ["incident-reader"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(updates)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def test_entra_resolver_validates_token_and_maps_roles() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resolver = EntraPrincipalResolver(
        tenant_id="tenant-id",
        audience="api-client-id",
        jwks_client=StaticJwksClient(private_key.public_key()),
    )

    principal = resolver.resolve(f"Bearer {_token(private_key)}")

    assert principal.workspace_id == "tenant-id"
    assert principal.principal_id == "user-id"
    assert principal.roles == frozenset({"user", "incident-reader"})
    assert principal.authentication_method == "entra-id"


def test_entra_resolver_rejects_wrong_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resolver = EntraPrincipalResolver(
        tenant_id="tenant-id",
        audience="api-client-id",
        jwks_client=StaticJwksClient(private_key.public_key()),
    )

    with pytest.raises(AuthenticationError, match="invalid"):
        resolver.resolve(f"Bearer {_token(private_key, aud='other-api')}")


class RejectingResolver:
    def resolve(self, authorization: str | None):
        del authorization
        raise AuthenticationError("A Bearer access token is required")


class FixedPrincipalResolver:
    def __init__(self, roles: frozenset[str]) -> None:
        self._roles = roles

    def resolve(self, authorization: str | None) -> Principal:
        del authorization
        return Principal(
            workspace_id="tenant-id",
            principal_id="admin-user",
            roles=self._roles,
            authentication_method="entra-id",
        )


def test_public_api_requires_configured_principal_but_health_remains_public(
    tmp_path: Path,
) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            admin_token="admin-token",
        ),
        principal_resolver=RejectingResolver(),
    )

    with TestClient(app) as client:
        health = client.get("/health/live")
        public_api = client.get("/v1/skills")
        admin_api = client.get(
            "/v1/admin/status",
            headers={"Authorization": "Bearer admin-token"},
        )

    assert health.status_code == 200
    assert public_api.status_code == 401
    assert admin_api.status_code == 200


def test_auth_config_exposes_pkce_metadata_without_credentials(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            authentication_mode="entra-id",
            admin_auth_enabled=True,
            entra_tenant_id="tenant-id",
            entra_audience="api-client-id",
            entra_client_id="spa-client-id",
            entra_scope="api://api-client-id/access_as_user",
        ),
        principal_resolver=RejectingResolver(),
    )

    with TestClient(app) as client:
        response = client.get("/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "entra-id",
        "tenant_id": "tenant-id",
        "client_id": "spa-client-id",
        "scope": "api://api-client-id/access_as_user",
    }
    assert "secret" not in response.text.casefold()


def test_security_headers_restrict_scripts_frames_and_content_types(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
        ),
    )

    with TestClient(app) as client:
        response = client.get("/")

    policy = response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "https://login.microsoftonline.com" in policy
    assert "strict-transport-security" in response.headers


@pytest.mark.parametrize(
    ("roles", "expected_status"),
    [
        (frozenset({"user", "platform_admin"}), 200),
        (frozenset({"user"}), 403),
    ],
)
def test_entra_admin_api_requires_administration_role(
    tmp_path: Path,
    roles: frozenset[str],
    expected_status: int,
) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            authentication_mode="entra-id",
            admin_auth_enabled=True,
            entra_tenant_id="tenant-id",
            entra_audience="api-client-id",
        ),
        principal_resolver=FixedPrincipalResolver(roles),
    )

    with TestClient(app) as client:
        response = client.get("/v1/admin/status")

    assert response.status_code == expected_status