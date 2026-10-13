import os

import pytest

from sage.settings import SettingsError, load_agent_settings

_MODEL_ENVIRONMENT = (
    "SAGE_AGENT_MODEL",
    "SAGE_AGENT_TEMPERATURE",
    "SAGE_AGENT_MAX_TOKENS",
    "SAGE_AGENT_TIMEOUT_SECONDS",
    "SAGE_AGENT_MAX_RETRIES",
    "SAGE_AGENT_RECURSION_LIMIT",
    "SAGE_AZURE_OPENAI_AUTH",
    "SAGE_AZURE_OPENAI_TOKEN_SCOPE",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def restore_model_environment():
    original = {name: os.environ.get(name) for name in _MODEL_ENVIRONMENT}
    yield
    for name, value in original.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _clear_model_environment(monkeypatch) -> None:
    for name in _MODEL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def test_loads_validated_agent_settings_from_env_file(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "SAGE_AGENT_MODEL=openai:test-model",
                "SAGE_AGENT_TEMPERATURE=0.2",
                "SAGE_AGENT_MAX_TOKENS=2048",
                "SAGE_AGENT_TIMEOUT_SECONDS=45",
                "SAGE_AGENT_MAX_RETRIES=4",
                "SAGE_AGENT_RECURSION_LIMIT=60",
            )
        ),
        encoding="utf-8",
    )

    settings = load_agent_settings(env_file)

    assert settings.model == "openai:test-model"
    assert settings.temperature == 0.2
    assert settings.max_tokens == 2048
    assert settings.timeout_seconds == 45
    assert settings.max_retries == 4
    assert settings.recursion_limit == 60
    assert settings.env_file_present is True


def test_process_environment_overrides_env_file(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("SAGE_AGENT_MODEL=openai:file-model\n", encoding="utf-8")
    monkeypatch.setenv("SAGE_AGENT_MODEL", "openai:process-model")

    settings = load_agent_settings(env_file)

    assert settings.model == "openai:process-model"


def test_rejects_invalid_agent_settings(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("SAGE_AGENT_TEMPERATURE=9\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="temperature"):
        load_agent_settings(env_file)


def test_loads_entra_id_settings_for_openai_v1_endpoint(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol",
                "SAGE_AZURE_OPENAI_AUTH=entra-id",
                "AZURE_OPENAI_ENDPOINT=https://example.services.ai.azure.com/openai/v1",
                "SAGE_AZURE_OPENAI_TOKEN_SCOPE=https://ai.azure.com/.default",
            )
        ),
        encoding="utf-8",
    )

    settings = load_agent_settings(env_file)

    assert settings.authentication_label() == "entra-id"
    assert settings.azure_deployment_name() == "gpt-5.6-sol"
    assert settings.azure_v1_base_url() == ("https://example.services.ai.azure.com/openai/v1/")
    assert settings.azure_openai_token_scope == "https://ai.azure.com/.default"


def test_loads_api_key_settings_for_openai_v1_endpoint(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol",
                "SAGE_AZURE_OPENAI_AUTH=api-key",
                "AZURE_OPENAI_ENDPOINT=https://example.services.ai.azure.com/openai/v1",
                "AZURE_OPENAI_API_KEY=test-key",
            )
        ),
        encoding="utf-8",
    )

    settings = load_agent_settings(env_file)

    assert settings.authentication_label() == "api-key"
    assert settings.azure_deployment_name() == "gpt-5.6-sol"
    assert settings.azure_v1_base_url() == ("https://example.services.ai.azure.com/openai/v1/")


def test_normalizes_complete_responses_endpoint_to_openai_v1_base(
    tmp_path, monkeypatch
) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol",
                "SAGE_AZURE_OPENAI_AUTH=api-key",
                "AZURE_OPENAI_ENDPOINT=https://gateway.example.com/resource/openai/v1/responses",
                "AZURE_OPENAI_API_KEY=test-key",
            )
        ),
        encoding="utf-8",
    )

    settings = load_agent_settings(env_file)

    assert settings.azure_v1_base_url() == (
        "https://gateway.example.com/resource/openai/v1/"
    )


def test_rejects_missing_azure_api_key(tmp_path, monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "SAGE_AGENT_MODEL=azure_openai:gpt-5.6-sol",
                "SAGE_AZURE_OPENAI_AUTH=api-key",
                "AZURE_OPENAI_ENDPOINT=https://example.services.ai.azure.com/openai/v1",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="AZURE_OPENAI_API_KEY"):
        load_agent_settings(env_file)


@pytest.mark.parametrize(
    ("model", "endpoint", "match"),
    [
        ("openai:gpt-5", "https://example.openai.azure.com/openai/v1", "requires"),
        ("azure_openai:deployment", "https://example.openai.azure.com", "/openai/v1"),
        ("azure_openai:deployment", "http://example/openai/v1", "HTTPS"),
    ],
)
def test_rejects_invalid_entra_id_configuration(
    tmp_path,
    monkeypatch,
    model,
    endpoint,
    match,
) -> None:
    _clear_model_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"SAGE_AGENT_MODEL={model}",
                "SAGE_AZURE_OPENAI_AUTH=entra-id",
                f"AZURE_OPENAI_ENDPOINT={endpoint}",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match=match):
        load_agent_settings(env_file)
