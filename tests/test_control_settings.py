from pathlib import Path

import pytest

from sage.control_settings import (
    DEFAULT_DEVELOPMENT_ADMIN_TOKEN,
    load_control_plane_settings,
)

_CONTROL_ENVIRONMENT = (
    "SAGE_ENVIRONMENT",
    "SAGE_ADMIN_AUTH_ENABLED",
    "SAGE_REPLICA_COUNT",
    "SAGE_AUTHENTICATION_MODE",
    "SAGE_ENTRA_TENANT_ID",
    "SAGE_ENTRA_AUDIENCE",
    "SAGE_ENTRA_CLIENT_ID",
    "SAGE_ENTRA_SCOPE",
    "SAGE_CORS_ALLOWED_ORIGINS",
    "SAGE_TEMPORAL_TARGET",
    "SAGE_TEMPORAL_NAMESPACE",
    "SAGE_TEMPORAL_TASK_QUEUE",
    "SAGE_TEMPORAL_API_KEY",
    "SAGE_TEMPORAL_TLS",
    "SAGE_CHECKPOINT_DATABASE_URL",
    "SAGE_ARTIFACT_BACKEND",
    "SAGE_S3_BUCKET",
    "SAGE_S3_PREFIX",
    "SAGE_S3_ENDPOINT_URL",
    "SAGE_S3_REGION",
    "SAGE_CLAMAV_HOST",
    "SAGE_CLAMAV_PORT",
    "SAGE_CLAMAV_TIMEOUT_SECONDS",
    "SAGE_REQUIRE_CONTENT_SCANNING",
    "SAGE_SOLUTION_TRUST_STORE",
    "SAGE_REQUIRE_SIGNED_SOLUTIONS",
    "SAGE_EVALUATION_GATE_MODE",
    "SAGE_RUNNER_URL",
    "SAGE_RUNNER_TOKEN",
    "SAGE_RUNNER_PROFILE",
    "SAGE_ALLOW_LOCAL_SUBPROCESS",
    "SAGE_ADMIN_TOKEN",
    "SAGE_CONTROL_DATA_ROOT",
    "SAGE_SKILLS_ROOT",
    "SAGE_MCP_ALLOWED_HOSTS",
    "SAGE_MCP_ENFORCE_ALLOWED_HOSTS",
    "SAGE_MCP_HEALTH_TTL_SECONDS",
    "SAGE_MCP_STDIO_PRESETS",
    "SAGE_ALLOW_PLAINTEXT_DEV_SECRETS",
    "SAGE_ALLOW_INSECURE_LOCAL_MCP",
)


def _clear(monkeypatch) -> None:
    for name in _CONTROL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def test_admin_authentication_is_disabled_by_default(monkeypatch) -> None:
    _clear(monkeypatch)

    settings = load_control_plane_settings()

    assert settings.environment == "development"
    assert settings.admin_auth_enabled is False
    assert settings.admin_token is None
    assert settings.auto_admin_enabled is False


def test_dotenv_loads_mcp_settings_without_overriding_process_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SAGE_MCP_ALLOWED_HOSTS=env.example.com\n"
        "SAGE_ALLOW_INSECURE_LOCAL_MCP=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAGE_MCP_ALLOWED_HOSTS", "process.example.com")

    settings = load_control_plane_settings(env_file)

    assert settings.mcp_allowed_hosts == frozenset({"process.example.com"})
    assert settings.allow_insecure_local_mcp is True


def test_admin_authentication_can_be_reenabled_for_development(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")

    settings = load_control_plane_settings()

    assert settings.admin_auth_enabled is True
    assert settings.admin_token == DEFAULT_DEVELOPMENT_ADMIN_TOKEN
    assert settings.auto_admin_enabled is True


def test_production_never_synthesizes_admin_token(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")

    settings = load_control_plane_settings()

    assert settings.environment == "production"
    assert settings.admin_token is None
    assert settings.auto_admin_enabled is False


def test_production_rejects_disabled_admin_authentication(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ENVIRONMENT", "production")

    with pytest.raises(Exception, match="administration authentication"):
        load_control_plane_settings()


def test_explicit_development_admin_token_is_preserved(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SAGE_ADMIN_TOKEN", "custom-development-token")

    settings = load_control_plane_settings()

    assert settings.admin_token == "custom-development-token"
    assert settings.auto_admin_enabled is True


def test_evaluation_gate_defaults_to_advisory_and_supports_strict_mode(
    monkeypatch,
) -> None:
    _clear(monkeypatch)

    assert load_control_plane_settings().evaluation_gate_mode == "advisory"

    monkeypatch.setenv("SAGE_EVALUATION_GATE_MODE", "required")
    assert load_control_plane_settings().evaluation_gate_mode == "required"


def test_runner_configuration_disables_local_subprocess_by_default(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_RUNNER_URL", "http://sage-runner:8080")
    monkeypatch.setenv("SAGE_RUNNER_TOKEN", "x" * 32)

    settings = load_control_plane_settings()

    assert settings.runner_url == "http://sage-runner:8080"
    assert settings.runner_profile == "python-restricted-v1"
    assert settings.local_subprocess_enabled is False


def test_runner_url_and_token_must_be_configured_together(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_RUNNER_URL", "http://sage-runner:8080")

    with pytest.raises(Exception, match="URL and token"):
        load_control_plane_settings()


def test_production_rejects_local_subprocess_execution(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SAGE_ALLOW_LOCAL_SUBPROCESS", "true")

    with pytest.raises(Exception, match="cannot execute Python Skills"):
        load_control_plane_settings()


def test_entra_authentication_requires_tenant_and_audience(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_AUTHENTICATION_MODE", "entra-id")
    monkeypatch.setenv("SAGE_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("SAGE_ENTRA_AUDIENCE", "api-client-id")

    settings = load_control_plane_settings()

    assert settings.authentication_mode == "entra-id"
    assert settings.entra_tenant_id == "tenant-id"
    assert settings.entra_audience == "api-client-id"


def test_cors_requires_explicit_http_origins(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(
        "SAGE_CORS_ALLOWED_ORIGINS",
        "https://client.example.com,http://localhost:5173/",
    )

    settings = load_control_plane_settings()

    assert settings.cors_allowed_origins == (
        "https://client.example.com",
        "http://localhost:5173",
    )

    monkeypatch.setenv("SAGE_CORS_ALLOWED_ORIGINS", "*")
    with pytest.raises(Exception, match="explicit HTTP"):
        load_control_plane_settings()


def test_durable_and_checkpoint_provider_settings_are_loaded(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_TEMPORAL_TARGET", "temporal.internal:7233")
    monkeypatch.setenv("SAGE_TEMPORAL_NAMESPACE", "sage")
    monkeypatch.setenv("SAGE_TEMPORAL_TASK_QUEUE", "sage-workers")
    monkeypatch.setenv("SAGE_TEMPORAL_TLS", "true")
    monkeypatch.setenv(
        "SAGE_CHECKPOINT_DATABASE_URL",
        "postgresql://sage:secret@postgres.internal/sage",
    )

    settings = load_control_plane_settings()

    assert settings.temporal_target == "temporal.internal:7233"
    assert settings.temporal_namespace == "sage"
    assert settings.temporal_task_queue == "sage-workers"
    assert settings.temporal_tls is True
    assert settings.checkpoint_database_url.startswith("postgresql://")


def test_s3_artifact_provider_settings_are_validated(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ARTIFACT_BACKEND", "s3")
    monkeypatch.setenv("SAGE_S3_BUCKET", "sage-artifacts")
    monkeypatch.setenv("SAGE_S3_ENDPOINT_URL", "https://minio.internal")

    settings = load_control_plane_settings()

    assert settings.artifact_backend == "s3"
    assert settings.s3_bucket == "sage-artifacts"
    assert settings.s3_endpoint_url == "https://minio.internal"


def test_production_rejects_multiple_replicas_with_local_state(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SAGE_REPLICA_COUNT", "2")

    try:
        load_control_plane_settings()
    except Exception as exc:
        assert "replicas" in str(exc)
    else:
        raise AssertionError("unsafe multi-replica local deployment was accepted")


def test_production_requires_mcp_host_allowlist(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SAGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SAGE_ADMIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SAGE_MCP_ENFORCE_ALLOWED_HOSTS", "false")

    try:
        load_control_plane_settings()
    except Exception as exc:
        assert "MCP host allowlist" in str(exc)
    else:
        raise AssertionError("production accepted disabled MCP host allowlist")
