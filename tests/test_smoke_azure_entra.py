from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from sage import agent_model
from sage.settings import AgentModelSettings
from scripts import smoke_azure_entra as smoke


def test_cli_token_provider_uses_selected_tenant(monkeypatch):
    credential = Mock()
    constructor = Mock(return_value=credential)
    provider = Mock(return_value=lambda: "test-token")
    monkeypatch.setattr(agent_model, "AzureCliCredential", constructor)
    monkeypatch.setattr(agent_model, "get_bearer_token_provider", provider)
    settings = AgentModelSettings(
        azure_openai_credential="azure-cli", azure_openai_tenant_id="tenant-id"
    )
    assert callable(agent_model.azure_token_provider(settings))
    constructor.assert_called_once_with(tenant_id="tenant-id", process_timeout=30)
    provider.assert_called_once_with(credential, "https://ai.azure.com/.default")


def test_login_reuses_account_and_supports_forced_tenant(monkeypatch):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "az.cmd")
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout="tenant-id\n"))
    monkeypatch.setattr(smoke.subprocess, "run", command)
    smoke.ensure_login(login=False, tenant="tenant-id")
    assert command.call_count == 1
    smoke.ensure_login(login=True, tenant="tenant-id")
    assert command.call_args.args[0] == [
        "az.cmd",
        "login",
        "--output",
        "none",
        "--tenant",
        "tenant-id",
    ]
    assert command.call_args.kwargs["env"]["AZURE_CORE_LOGIN_EXPERIENCE_V2"] == "off"


def test_configure_only_after_success_without_api_key(tmp_path, monkeypatch):
    for name in (
        "SAGE_AGENT_MODEL",
        "SAGE_AZURE_OPENAI_AUTH",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "SAGE_AZURE_OPENAI_CREDENTIAL",
        "SAGE_AZURE_OPENAI_TENANT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    original = (
        "# Preserve this configuration\n"
        "SAGE_AGENT_MODEL=azure_openai:test-deployment\n"
        "SAGE_AZURE_OPENAI_AUTH=api-key\n"
        "AZURE_OPENAI_ENDPOINT=https://example.services.ai.azure.com/openai/v1\n"
    )
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(smoke, "ensure_login", Mock())
    model = Mock()
    model.invoke.side_effect = RuntimeError("sensitive upstream message")
    factory = Mock(return_value=(model, "azure_openai:test-deployment"))
    monkeypatch.setattr(smoke, "load_agent_model", factory)
    assert smoke.main(["--env-file", str(env_file), "--configure"]) == 1
    assert env_file.read_text(encoding="utf-8") == original
    model.invoke.side_effect = None
    model.invoke.return_value = AIMessage(content="Entra verified")
    assert smoke.main(["--env-file", str(env_file), "--configure"]) == 0
    configured = factory.call_args.args[0]
    assert configured.azure_openai_auth == "entra-id"
    assert configured.azure_openai_credential == "azure-cli"
    saved = env_file.read_text(encoding="utf-8")
    assert "SAGE_AZURE_OPENAI_AUTH='entra-id'" in saved
    assert "SAGE_AZURE_OPENAI_CREDENTIAL='azure-cli'" in saved
    assert "# Preserve this configuration" in saved
    assert "SAGE_AGENT_MODEL=azure_openai:test-deployment" in saved


def test_missing_cli_does_not_attempt_login(monkeypatch):
    import pytest

    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    with pytest.raises(smoke.SettingsError, match="Azure CLI is required"):
        smoke.ensure_login(login=False, tenant=None)


def test_login_selects_and_verifies_subscription(monkeypatch):
    subscription = "16b3c013-d300-468d-ac64-7eda0820b6d3"
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "az.cmd")
    command = Mock(side_effect=[
        SimpleNamespace(returncode=0, stdout="tenant-id\n"),
        SimpleNamespace(returncode=0),
        SimpleNamespace(returncode=0),
        SimpleNamespace(returncode=0, stdout=subscription + "\n"),
    ])
    monkeypatch.setattr(smoke.subprocess, "run", command)
    smoke.ensure_login(login=True, tenant=None, subscription=subscription)
    calls = command.call_args_list
    assert calls[1].args[0] == ["az.cmd", "login", "--output", "none"]
    assert calls[1].kwargs["env"]["AZURE_CORE_LOGIN_EXPERIENCE_V2"] == "off"
    assert calls[2].args[0] == ["az.cmd", "account", "set", "--subscription", subscription]
    assert calls[3].args[0] == ["az.cmd", "account", "show", "--query", "id", "--output", "tsv"]


@pytest.mark.parametrize("select_code, selected_id", [(1, "requested"), (0, "wrong")])
def test_subscription_failure_stops_verification(monkeypatch, select_code, selected_id):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "az.cmd")
    command = Mock(side_effect=[
        SimpleNamespace(returncode=0, stdout="tenant-id\n"),
        SimpleNamespace(returncode=select_code),
        SimpleNamespace(returncode=0, stdout=selected_id),
    ])
    monkeypatch.setattr(smoke.subprocess, "run", command)
    with pytest.raises(smoke.SettingsError, match="subscription"):
        smoke.ensure_login(login=False, tenant=None, subscription="requested")
