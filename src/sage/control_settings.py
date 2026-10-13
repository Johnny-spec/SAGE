"""Environment-backed settings for the local control plane."""

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sage.settings import DEFAULT_ENV_FILE, PROJECT_ROOT, SettingsError

DEFAULT_DEVELOPMENT_ADMIN_TOKEN = "sage-local-admin"


class StdioPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secret_env_name: str | None = None


class ControlPlaneSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_root: Path = PROJECT_ROOT / ".sage-state"
    skills_root: Path = PROJECT_ROOT / "skills"
    environment: Literal["development", "production"] = "development"
    replica_count: int = Field(default=1, ge=1)
    authentication_mode: Literal["anonymous", "entra-id"] = "anonymous"
    entra_tenant_id: str | None = None
    entra_audience: str | None = None
    entra_client_id: str | None = None
    entra_scope: str | None = None
    temporal_target: str | None = None
    temporal_namespace: str = "default"
    temporal_task_queue: str = "sage-workflows"
    temporal_api_key: str | None = None
    temporal_tls: bool = False
    checkpoint_database_url: str | None = None
    artifact_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = None
    s3_prefix: str = "sage"
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_insecure: bool = False
    otel_service_name: str = "sage"
    clamav_host: str | None = None
    clamav_port: int = Field(default=3310, ge=1, le=65_535)
    clamav_timeout_seconds: float = Field(default=30, gt=0, le=300)
    require_content_scanning: bool | None = None
    solution_trust_store: Path | None = None
    require_signed_solutions: bool | None = None
    evaluation_gate_mode: Literal["advisory", "required"] = "advisory"
    runner_url: str | None = None
    runner_token: str | None = Field(default=None, min_length=32)
    runner_profile: str = Field(default="python-restricted-v1", min_length=1, max_length=100)
    allow_local_subprocess: bool | None = None
    admin_auth_enabled: bool = False
    admin_token: str | None = None
    cors_allowed_origins: tuple[str, ...] = ()
    allow_plaintext_dev_secrets: bool = False
    allow_insecure_local_mcp: bool = False
    mcp_enforce_allowed_hosts: bool = True
    mcp_allowed_hosts: frozenset[str] = frozenset()
    mcp_stdio_presets: dict[str, StdioPreset] = Field(default_factory=dict)
    mcp_health_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for origin in origins:
            parsed = urlparse(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS origins must be explicit HTTP(S) origins without paths")
            value = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    @property
    def auto_admin_enabled(self) -> bool:
        return (
            self.admin_auth_enabled
            and self.environment == "development"
            and bool(self.admin_token)
        )

    @property
    def content_scanning_required(self) -> bool:
        if self.require_content_scanning is not None:
            return self.require_content_scanning
        return self.environment == "production"

    @property
    def signed_solutions_required(self) -> bool:
        if self.require_signed_solutions is not None:
            return self.require_signed_solutions
        return self.environment == "production"

    @property
    def local_subprocess_enabled(self) -> bool:
        if self.allow_local_subprocess is not None:
            return self.allow_local_subprocess
        return self.environment == "development" and self.runner_url is None

    @model_validator(mode="after")
    def validate_authentication(self) -> "ControlPlaneSettings":
        if self.authentication_mode == "entra-id" and (
            not self.entra_tenant_id or not self.entra_audience
        ):
            raise ValueError(
                "entra_tenant_id and entra_audience are required for Entra authentication"
            )
        if self.artifact_backend == "s3" and not self.s3_bucket:
            raise ValueError("s3_bucket is required for the S3 Artifact backend")
        if self.environment == "production" and not self.mcp_enforce_allowed_hosts:
            raise ValueError("Production requires MCP host allowlist enforcement")
        if self.environment == "production" and not self.admin_auth_enabled:
            raise ValueError("Production requires administration authentication")
        if (self.runner_url is None) != (self.runner_token is None):
            raise ValueError("Runner URL and token must be configured together")
        if self.runner_url is not None:
            parsed_runner_url = urlparse(self.runner_url)
            if parsed_runner_url.scheme not in {"http", "https"} or not parsed_runner_url.netloc:
                raise ValueError("Runner URL must be an absolute HTTP(S) URL")
        if self.environment == "production" and self.local_subprocess_enabled:
            raise ValueError("Production cannot execute Python Skills in the API process")
        if self.environment == "production" and self.replica_count > 1:
            if self.artifact_backend != "s3":
                raise ValueError("Production replicas require the S3 Artifact backend")
            raise ValueError(
                "Production replicas require PostgreSQL state providers, which are not configured"
            )
        return self


def load_control_plane_settings(
    env_file: Path | None = None,
) -> ControlPlaneSettings:
    file_values = dotenv_values(env_file or DEFAULT_ENV_FILE) if env_file is not None else {}

    def optional_environment(name: str) -> str | None:
        value = os.environ.get(name) if name in os.environ else file_values.get(name)
        if value is None or not str(value).strip():
            return None
        return str(value).strip()

    def enabled(name: str) -> bool:
        return (optional_environment(name) or "").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    environment = (optional_environment("SAGE_ENVIRONMENT") or "development").casefold()
    admin_auth_enabled = enabled("SAGE_ADMIN_AUTH_ENABLED")
    admin_token = optional_environment("SAGE_ADMIN_TOKEN")
    if admin_auth_enabled and environment == "development" and admin_token is None:
        admin_token = DEFAULT_DEVELOPMENT_ADMIN_TOKEN
    raw_presets = optional_environment("SAGE_MCP_STDIO_PRESETS")
    presets: dict[str, Any] = {}
    if raw_presets:
        try:
            parsed = json.loads(raw_presets)
        except json.JSONDecodeError as exc:
            raise SettingsError("SAGE_MCP_STDIO_PRESETS must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise SettingsError("SAGE_MCP_STDIO_PRESETS must be a JSON object")
        presets = parsed
    allowed_hosts = frozenset(
        item.strip().casefold()
        for item in (optional_environment("SAGE_MCP_ALLOWED_HOSTS") or "").split(",")
        if item.strip()
    )
    cors_allowed_origins = tuple(
        item.strip()
        for item in (optional_environment("SAGE_CORS_ALLOWED_ORIGINS") or "").split(",")
        if item.strip()
    )
    try:
        return ControlPlaneSettings.model_validate(
            {
                "data_root": (
                    optional_environment("SAGE_CONTROL_DATA_ROOT") or PROJECT_ROOT / ".sage-state"
                ),
                "skills_root": (
                    optional_environment("SAGE_SKILLS_ROOT") or PROJECT_ROOT / "skills"
                ),
                "environment": environment,
                "replica_count": optional_environment("SAGE_REPLICA_COUNT") or 1,
                "authentication_mode": (
                    optional_environment("SAGE_AUTHENTICATION_MODE") or "anonymous"
                ),
                "entra_tenant_id": optional_environment("SAGE_ENTRA_TENANT_ID"),
                "entra_audience": optional_environment("SAGE_ENTRA_AUDIENCE"),
                "entra_client_id": optional_environment("SAGE_ENTRA_CLIENT_ID"),
                "entra_scope": optional_environment("SAGE_ENTRA_SCOPE"),
                "temporal_target": optional_environment("SAGE_TEMPORAL_TARGET"),
                "temporal_namespace": (
                    optional_environment("SAGE_TEMPORAL_NAMESPACE") or "default"
                ),
                "temporal_task_queue": (
                    optional_environment("SAGE_TEMPORAL_TASK_QUEUE") or "sage-workflows"
                ),
                "temporal_api_key": optional_environment("SAGE_TEMPORAL_API_KEY"),
                "temporal_tls": enabled("SAGE_TEMPORAL_TLS"),
                "checkpoint_database_url": optional_environment(
                    "SAGE_CHECKPOINT_DATABASE_URL"
                ),
                "artifact_backend": (
                    optional_environment("SAGE_ARTIFACT_BACKEND") or "local"
                ),
                "s3_bucket": optional_environment("SAGE_S3_BUCKET"),
                "s3_prefix": optional_environment("SAGE_S3_PREFIX") or "sage",
                "s3_endpoint_url": optional_environment("SAGE_S3_ENDPOINT_URL"),
                "s3_region": optional_environment("SAGE_S3_REGION"),
                "otel_exporter_otlp_endpoint": optional_environment(
                    "OTEL_EXPORTER_OTLP_ENDPOINT"
                ),
                "otel_exporter_otlp_insecure": enabled(
                    "OTEL_EXPORTER_OTLP_INSECURE"
                ),
                "otel_service_name": (
                    optional_environment("OTEL_SERVICE_NAME") or "sage"
                ),
                "clamav_host": optional_environment("SAGE_CLAMAV_HOST"),
                "clamav_port": optional_environment("SAGE_CLAMAV_PORT") or 3310,
                "clamav_timeout_seconds": (
                    optional_environment("SAGE_CLAMAV_TIMEOUT_SECONDS") or 30
                ),
                "require_content_scanning": (
                    enabled("SAGE_REQUIRE_CONTENT_SCANNING")
                    if optional_environment("SAGE_REQUIRE_CONTENT_SCANNING") is not None
                    else None
                ),
                "solution_trust_store": optional_environment(
                    "SAGE_SOLUTION_TRUST_STORE"
                ),
                "require_signed_solutions": (
                    enabled("SAGE_REQUIRE_SIGNED_SOLUTIONS")
                    if optional_environment("SAGE_REQUIRE_SIGNED_SOLUTIONS") is not None
                    else None
                ),
                "evaluation_gate_mode": (
                    optional_environment("SAGE_EVALUATION_GATE_MODE") or "advisory"
                ),
                "runner_url": optional_environment("SAGE_RUNNER_URL"),
                "runner_token": optional_environment("SAGE_RUNNER_TOKEN"),
                "runner_profile": (
                    optional_environment("SAGE_RUNNER_PROFILE") or "python-restricted-v1"
                ),
                "allow_local_subprocess": (
                    enabled("SAGE_ALLOW_LOCAL_SUBPROCESS")
                    if optional_environment("SAGE_ALLOW_LOCAL_SUBPROCESS") is not None
                    else None
                ),
                "admin_auth_enabled": admin_auth_enabled,
                "admin_token": admin_token,
                "cors_allowed_origins": cors_allowed_origins,
                "allow_plaintext_dev_secrets": enabled("SAGE_ALLOW_PLAINTEXT_DEV_SECRETS"),
                "allow_insecure_local_mcp": enabled("SAGE_ALLOW_INSECURE_LOCAL_MCP"),
                "mcp_enforce_allowed_hosts": (
                    enabled("SAGE_MCP_ENFORCE_ALLOWED_HOSTS")
                    if optional_environment("SAGE_MCP_ENFORCE_ALLOWED_HOSTS") is not None
                    else True
                ),
                "mcp_allowed_hosts": allowed_hosts,
                "mcp_stdio_presets": presets,
                "mcp_health_ttl_seconds": (
                    optional_environment("SAGE_MCP_HEALTH_TTL_SECONDS") or 900
                ),
            }
        )
    except ValidationError as exc:
        raise SettingsError(f"Invalid control-plane configuration: {exc}") from exc
