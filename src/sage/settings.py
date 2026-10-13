"""Validated environment-backed settings for the SAGE Agent runtime."""

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class SettingsError(ValueError):
    """Raised when local or process environment settings are invalid."""


class AgentModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str | None = None
    temperature: float | None = Field(default=0, ge=0, le=2)
    max_tokens: int | None = Field(default=4096, ge=1)
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    recursion_limit: int = Field(default=40, ge=5, le=200)
    azure_openai_auth: Literal["api-key", "entra-id"] = "api-key"
    azure_openai_credential: Literal["default", "azure-cli"] = "default"
    azure_openai_tenant_id: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_token_scope: str = "https://ai.azure.com/.default"
    env_file_present: bool = False

    def model_kwargs(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "timeout": self.timeout_seconds,
            "max_retries": self.max_retries,
        }
        if self.temperature is not None:
            values["temperature"] = self.temperature
        if self.max_tokens is not None:
            values["max_tokens"] = self.max_tokens
        return values

    def azure_deployment_name(self) -> str:
        if not self.model or not self.model.startswith("azure_openai:"):
            raise SettingsError(
                "Azure OpenAI authentication requires "
                "SAGE_AGENT_MODEL=azure_openai:<deployment-name>"
            )
        deployment = self.model.split(":", 1)[1].strip()
        if not deployment:
            raise SettingsError("Azure OpenAI deployment name cannot be empty")
        return deployment

    def azure_v1_base_url(self) -> str:
        endpoint = (self.azure_openai_endpoint or "").rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SettingsError(
                "AZURE_OPENAI_ENDPOINT must be an HTTPS URL ending in /openai/v1 "
                "or /openai/v1/responses "
                "for Azure OpenAI authentication"
            )
        path = parsed.path.rstrip("/")
        if path.endswith("/openai/v1/responses"):
            endpoint = endpoint[: -len("/responses")]
            path = path[: -len("/responses")]
        if not path.endswith("/openai/v1"):
            raise SettingsError(
                "AZURE_OPENAI_ENDPOINT must include the /openai/v1 or "
                "/openai/v1/responses path "
                "for Azure OpenAI authentication"
            )
        return f"{endpoint}/"

    def authentication_label(self) -> Literal["offline", "api-key", "entra-id"]:
        if not self.model:
            return "offline"
        if self.model.startswith("azure_openai:"):
            return self.azure_openai_auth
        return "api-key"


def _optional_environment(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def load_agent_settings(
    env_file: Path = DEFAULT_ENV_FILE,
    *,
    azure_auth: Literal["api-key", "entra-id"] | None = None,
) -> AgentModelSettings:
    """Load `.env` without overriding process variables, then validate settings."""

    env_file = env_file.resolve()
    load_dotenv(dotenv_path=env_file, override=False)
    values: dict[str, object] = {
        "model": _optional_environment("SAGE_AGENT_MODEL"),
        "azure_openai_auth": azure_auth
        or _optional_environment("SAGE_AZURE_OPENAI_AUTH")
        or "api-key",
        "azure_openai_credential": _optional_environment("SAGE_AZURE_OPENAI_CREDENTIAL")
        or "default",
        "azure_openai_tenant_id": _optional_environment("SAGE_AZURE_OPENAI_TENANT_ID"),
        "azure_openai_endpoint": _optional_environment("AZURE_OPENAI_ENDPOINT"),
        "azure_openai_token_scope": (
            _optional_environment("SAGE_AZURE_OPENAI_TOKEN_SCOPE")
            or "https://ai.azure.com/.default"
        ),
        "env_file_present": env_file.is_file(),
    }
    environment_fields = {
        "temperature": "SAGE_AGENT_TEMPERATURE",
        "max_tokens": "SAGE_AGENT_MAX_TOKENS",
        "timeout_seconds": "SAGE_AGENT_TIMEOUT_SECONDS",
        "max_retries": "SAGE_AGENT_MAX_RETRIES",
        "recursion_limit": "SAGE_AGENT_RECURSION_LIMIT",
    }
    for field_name, environment_name in environment_fields.items():
        value = _optional_environment(environment_name)
        if value is not None:
            values[field_name] = value
    try:
        settings = AgentModelSettings.model_validate(values)
        is_azure_model = bool(settings.model and settings.model.startswith("azure_openai:"))
        if settings.azure_openai_auth == "entra-id" or is_azure_model:
            settings.azure_deployment_name()
            settings.azure_v1_base_url()
            if (
                settings.azure_openai_auth == "api-key"
                and _optional_environment("AZURE_OPENAI_API_KEY") is None
            ):
                raise SettingsError(
                    "AZURE_OPENAI_API_KEY is required when SAGE_AZURE_OPENAI_AUTH=api-key"
                )
        return settings
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise SettingsError(f"Invalid Agent model configuration: {details}") from exc
