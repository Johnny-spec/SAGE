from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from sage import agent_model
from sage.agent_model import DemoAgentModel
from sage.settings import AgentModelSettings


def test_demo_model_starts_complex_iis_request_with_plan() -> None:
    model = DemoAgentModel().bind_tools(
        [
            {"name": "write_todos"},
            {"name": "task"},
            {"name": "skill_report_summary"},
        ]
    )

    response = model.invoke([HumanMessage(content="Analyze this IIS log and report it")])

    assert isinstance(response, AIMessage)
    assert response.tool_calls[0]["name"] == "write_todos"
    assert len(response.tool_calls[0]["args"]["todos"]) == 2


def test_demo_model_recalls_codename_from_prior_thread_messages() -> None:
    model = DemoAgentModel()
    messages = [
        HumanMessage(content="Remember that the project codename is Orion"),
        AIMessage(content="I will remember."),
        HumanMessage(content="What is the project codename?"),
    ]

    response = model.invoke(messages)

    assert response.content == "The project codename is Orion."


def test_incident_subagent_progressively_loads_then_calls_iis_skill() -> None:
    model = DemoAgentModel().bind_tools([{"name": "read_file"}, {"name": "skill_iis_logs"}])
    system = {"role": "system", "content": "You are the incident analyst subagent."}
    request = HumanMessage(content="Analyze IIS evidence and create a report")

    first = model.invoke([system, request])
    assert isinstance(first, AIMessage)
    assert first.tool_calls[0]["name"] == "read_file"
    assert first.tool_calls[0]["args"]["file_path"] == "/iis-logs/SKILL.md"

    instructions = ToolMessage(
        content="# IIS Logs\nRead references/triage.md for reports.",
        tool_call_id=first.tool_calls[0]["id"],
    )
    second = model.invoke([system, request, first, instructions])
    assert second.tool_calls[0]["name"] == "read_file"
    assert second.tool_calls[0]["args"]["file_path"] == "/iis-logs/references/triage.md"

    reference = ToolMessage(
        content="# IIS Triage Reporting",
        tool_call_id=second.tool_calls[0]["id"],
    )
    third = model.invoke([system, request, first, instructions, second, reference])
    assert third.tool_calls[0]["name"] == "skill_iis_logs"

    result = ToolMessage(
        content='{"skill":"iis-logs","result":{"ok":true}}',
        tool_call_id=third.tool_calls[0]["id"],
    )
    final = model.invoke([system, request, first, instructions, second, reference, third, result])

    assert final.content == result.content


def test_incident_subagent_selects_netlog_skill_for_browser_capture() -> None:
    model = DemoAgentModel().bind_tools([{"name": "read_file"}, {"name": "skill_netlog"}])
    system = {"role": "system", "content": "You are the incident analyst subagent."}
    request = HumanMessage(content="Analyze this Chromium netlog authentication failure")

    first = model.invoke([system, request])
    assert first.tool_calls[0]["name"] == "read_file"
    assert first.tool_calls[0]["args"]["file_path"] == "/netlog/SKILL.md"

    instructions = ToolMessage(
        content="# Chromium NetLog",
        tool_call_id=first.tool_calls[0]["id"],
    )
    second = model.invoke([system, request, first, instructions])

    assert second.tool_calls[0]["name"] == "skill_netlog"


def test_configured_model_uses_langchain_provider_loader(monkeypatch) -> None:
    configured = DemoAgentModel(model_name="provider-stub")
    captured = {}

    def init_model(name, **kwargs):
        captured.update({"name": name, **kwargs})
        return configured

    monkeypatch.setattr(agent_model, "init_chat_model", init_model)

    model, name = agent_model.load_agent_model(
        AgentModelSettings(
            model="openai:test-model",
            temperature=0.25,
            max_tokens=2048,
            timeout_seconds=30,
            max_retries=3,
        )
    )

    assert model is configured
    assert name == "openai:test-model"
    assert captured == {
        "name": "openai:test-model",
        "temperature": 0.25,
        "max_tokens": 2048,
        "timeout": 30,
        "max_retries": 3,
    }


def test_entra_id_model_uses_default_credential_token_provider(monkeypatch) -> None:
    configured = DemoAgentModel(model_name="entra-stub")
    credential = object()
    captured = {}

    def token_provider():
        return "fake-token"

    monkeypatch.setattr(agent_model, "DefaultAzureCredential", lambda: credential)

    def create_provider(received_credential, scope):
        assert received_credential is credential
        assert scope == "https://ai.azure.com/.default"
        return token_provider

    def create_chat_model(**kwargs):
        captured.update(kwargs)
        return configured

    monkeypatch.setattr(agent_model, "get_bearer_token_provider", create_provider)
    monkeypatch.setattr(agent_model, "ChatOpenAI", create_chat_model)
    settings = AgentModelSettings(
        model="azure_openai:gpt-5.6-sol",
        azure_openai_auth="entra-id",
        azure_openai_endpoint=("https://wshao-childqa-resource.services.ai.azure.com/openai/v1"),
        temperature=0,
        max_tokens=4096,
        timeout_seconds=120,
        max_retries=2,
    )

    model, name = agent_model.load_agent_model(settings)

    assert model is configured
    assert name == "azure_openai:gpt-5.6-sol"
    assert captured == {
        "model": "gpt-5.6-sol",
        "base_url": ("https://wshao-childqa-resource.services.ai.azure.com/openai/v1/"),
        "api_key": token_provider,
        "use_responses_api": True,
        "temperature": 0,
        "max_tokens": 4096,
        "timeout": 120,
        "max_retries": 2,
    }


def test_azure_api_key_model_uses_openai_v1_endpoint(monkeypatch) -> None:
    configured = DemoAgentModel(model_name="api-key-stub")
    captured = {}
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-azure-key")

    def create_chat_model(**kwargs):
        captured.update(kwargs)
        return configured

    monkeypatch.setattr(agent_model, "ChatOpenAI", create_chat_model)
    settings = AgentModelSettings(
        model="azure_openai:gpt-5.6-sol",
        azure_openai_auth="api-key",
        azure_openai_endpoint=("https://wshao-childqa-resource.services.ai.azure.com/openai/v1"),
        temperature=0,
        max_tokens=4096,
        timeout_seconds=120,
        max_retries=2,
    )

    model, name = agent_model.load_agent_model(settings)

    assert model is configured
    assert name == "azure_openai:gpt-5.6-sol"
    assert isinstance(captured["api_key"], SecretStr)
    assert captured.pop("api_key").get_secret_value() == "test-azure-key"
    assert captured == {
        "model": "gpt-5.6-sol",
        "base_url": ("https://wshao-childqa-resource.services.ai.azure.com/openai/v1/"),
        "use_responses_api": True,
        "temperature": 0,
        "max_tokens": 4096,
        "timeout": 120,
        "max_retries": 2,
    }


def test_azure_api_management_gateway_receives_subscription_key_header(
    monkeypatch,
) -> None:
    configured = DemoAgentModel(model_name="gateway-stub")
    captured = {}
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-gateway-key")

    def create_chat_model(**kwargs):
        captured.update(kwargs)
        return configured

    monkeypatch.setattr(agent_model, "ChatOpenAI", create_chat_model)
    settings = AgentModelSettings(
        model="azure_openai:gpt-5.6-sol",
        azure_openai_auth="api-key",
        azure_openai_endpoint=(
            "https://wshao-gateway.azure-api.net/"
            "wshao-childqa-resource/openai/v1/responses"
        ),
    )

    model, _ = agent_model.load_agent_model(settings)

    assert model is configured
    assert captured["base_url"] == (
        "https://wshao-gateway.azure-api.net/"
        "wshao-childqa-resource/openai/v1/"
    )
    assert captured["default_headers"] == {
        "api-key": "test-gateway-key"
    }
