"""Model selection and an offline model for the Deep Agent demo."""

import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from azure.identity import AzureCliCredential, DefaultAzureCredential, get_bearer_token_provider
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from sage.settings import AgentModelSettings, SettingsError, load_agent_settings

_CODENAME_STATEMENT = re.compile(
    r"(?:project\s+codename|项目代号)\s*(?:is|是|:|：)\s*([\w.-]+)",
    re.IGNORECASE,
)


def _tool_name(tool: object) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        if isinstance(tool.get("name"), str):
            return tool["name"]
    name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
    return str(name or type(tool).__name__)


class DemoAgentModel(BaseChatModel):
    """Predictable tool-calling model used when no external model is configured."""

    bound_tool_names: tuple[str, ...] = ()
    model_name: str = "sage-demo-model"

    @property
    def _llm_type(self) -> str:
        return "sage-demo"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        del tool_choice, kwargs
        return self.model_copy(
            update={"bound_tool_names": tuple(_tool_name(tool) for tool in tools)}
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        user_message, current_turn = self._current_turn(messages)
        tool_names = self._completed_tool_names(current_turn)
        read_paths = self._loaded_file_paths(current_turn)
        counts = Counter(tool_names)
        system_text = "\n".join(
            str(message.content) for message in messages if message.type == "system"
        ).casefold()

        if "incident analyst subagent" in system_text:
            if self._is_netlog_request(user_message):
                if "/netlog/SKILL.md" not in read_paths:
                    return self._read_file_call("/netlog/SKILL.md")
                if "skill_netlog" in self.bound_tool_names and counts["skill_netlog"] == 0:
                    return self._tool_call("skill_netlog", {"arguments": {}})
                return AIMessage(content=self._latest_tool_content(current_turn))
            if "/iis-logs/SKILL.md" not in read_paths:
                return self._read_file_call("/iis-logs/SKILL.md")
            if (
                self._needs_triage_reference(user_message)
                and "/iis-logs/references/triage.md" not in read_paths
            ):
                return self._read_file_call("/iis-logs/references/triage.md")
            if "skill_iis_logs" in self.bound_tool_names and counts["skill_iis_logs"] == 0:
                return self._tool_call("skill_iis_logs", {"arguments": {}})
            return AIMessage(content=self._latest_tool_content(current_turn))

        memory_answer = self._memory_answer(messages, user_message)
        if memory_answer is not None:
            return AIMessage(content=memory_answer)

        if self._is_netlog_request(user_message):
            if counts["write_todos"] == 0:
                return self._tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Delegate browser NetLog analysis",
                                "status": "in_progress",
                            },
                            {"content": "Build a concise incident report", "status": "pending"},
                        ]
                    },
                )
            if counts["task"] == 0:
                return self._tool_call(
                    "task",
                    {
                        "description": (
                            f"Analyze the uploaded Chromium NetLog for this request: "
                            f"{user_message}. Use the netlog Skill and return only its structured "
                            "JSON result."
                        ),
                        "subagent_type": "incident-analyst",
                    },
                )
            if "/report-summary/SKILL.md" not in read_paths:
                return self._read_file_call("/report-summary/SKILL.md")
            if counts["skill_report_summary"] == 0:
                analysis = self._task_analysis(current_turn)
                return self._tool_call(
                    "skill_report_summary",
                    {
                        "arguments": {
                            "analysis": analysis,
                            "title": "Browser authentication incident summary",
                        }
                    },
                )
            if counts["write_todos"] == 1:
                return self._tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Delegate browser NetLog analysis",
                                "status": "completed",
                            },
                            {
                                "content": "Build a concise incident report",
                                "status": "completed",
                            },
                        ]
                    },
                )
            return AIMessage(content=self._summary_answer(current_turn))

        if self._is_iis_request(user_message):
            if counts["write_todos"] == 0:
                return self._tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {"content": "Delegate IIS evidence analysis", "status": "in_progress"},
                            {"content": "Build a concise incident report", "status": "pending"},
                        ]
                    },
                )
            if counts["task"] == 0:
                return self._tool_call(
                    "task",
                    {
                        "description": (
                            f"Analyze the uploaded IIS evidence for this request: {user_message}. "
                            "Use the iis-logs Skill and return only its structured JSON result."
                        ),
                        "subagent_type": "incident-analyst",
                    },
                )
            if "/report-summary/SKILL.md" not in read_paths:
                return self._read_file_call("/report-summary/SKILL.md")
            if counts["skill_report_summary"] == 0:
                analysis = self._task_analysis(current_turn)
                return self._tool_call(
                    "skill_report_summary",
                    {"arguments": {"analysis": analysis, "title": "IIS incident summary"}},
                )
            if counts["write_todos"] == 1:
                return self._tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {"content": "Delegate IIS evidence analysis", "status": "completed"},
                            {"content": "Build a concise incident report", "status": "completed"},
                        ]
                    },
                )
            return AIMessage(content=self._summary_answer(current_turn))

        if "skill_echo" in self.bound_tool_names and any(
            keyword in user_message.casefold() for keyword in ("echo", "repeat", "回显", "重复")
        ):
            if "/echo/SKILL.md" not in read_paths:
                return self._read_file_call("/echo/SKILL.md")
            if counts["skill_echo"] == 0:
                return self._tool_call("skill_echo", {"arguments": {"text": user_message}})
            return AIMessage(content=self._latest_tool_content(current_turn))

        return AIMessage(
            content=(
                "The demo model could not map this request to an installed Skill. "
                "Configure SAGE_AGENT_MODEL for general model reasoning."
            )
        )

    @staticmethod
    def _current_turn(messages: list[BaseMessage]) -> tuple[str, list[BaseMessage]]:
        index = max(
            (item for item, message in enumerate(messages) if isinstance(message, HumanMessage)),
            default=0,
        )
        human = messages[index] if messages else HumanMessage(content="")
        return str(human.content), messages[index:]

    @staticmethod
    def _completed_tool_names(messages: list[BaseMessage]) -> list[str]:
        calls = {
            call["id"]: call["name"]
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        return [
            calls.get(message.tool_call_id, message.name or "")
            for message in messages
            if isinstance(message, ToolMessage)
        ]

    @staticmethod
    def _loaded_file_paths(messages: list[BaseMessage]) -> set[str]:
        calls = {
            call["id"]: call
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if call.get("name") == "read_file"
        }
        paths = set()
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            call = calls.get(message.tool_call_id)
            if call is None or getattr(message, "status", None) == "error":
                continue
            file_path = call.get("args", {}).get("file_path")
            if isinstance(file_path, str) and not str(message.content).casefold().startswith(
                "error"
            ):
                paths.add(file_path)
        return paths

    @staticmethod
    def _latest_tool_content(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                return str(message.content)
        return "No tool result was produced."

    @staticmethod
    def _tool_result(messages: list[BaseMessage], name: str) -> str | None:
        calls = {
            call["id"]: call["name"]
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and calls.get(message.tool_call_id) == name:
                return str(message.content)
        return None

    @classmethod
    def _task_analysis(cls, messages: list[BaseMessage]) -> dict[str, Any]:
        raw = cls._tool_result(messages, "task") or "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"findings": [raw], "raw": {}}
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            return payload["result"]
        return payload if isinstance(payload, dict) else {"raw": {}}

    @classmethod
    def _summary_answer(cls, messages: list[BaseMessage]) -> str:
        raw = cls._tool_result(messages, "skill_report_summary") or "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        result = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(result, dict) and isinstance(result.get("summary"), str):
            return result["summary"]
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _is_iis_request(message: str) -> bool:
        normalized = message.casefold()
        return any(keyword in normalized for keyword in ("iis", "w3c", "日志", "log"))

    @staticmethod
    def _is_netlog_request(message: str) -> bool:
        normalized = message.casefold()
        return any(
            keyword in normalized
            for keyword in ("netlog", "net-export", "net export", "chrome-net", "浏览器")
        )

    @staticmethod
    def _needs_triage_reference(message: str) -> bool:
        normalized = message.casefold()
        return any(keyword in normalized for keyword in ("report", "summary", "报告", "总结"))

    @staticmethod
    def _memory_answer(messages: list[BaseMessage], current: str) -> str | None:
        current_match = _CODENAME_STATEMENT.search(current)
        if current_match and any(keyword in current.casefold() for keyword in ("remember", "记住")):
            return f"I will remember that the project codename is {current_match.group(1)}."
        if not any(
            phrase in current.casefold()
            for phrase in ("what is the project codename", "项目代号是什么", "项目代号是啥")
        ):
            return None
        for message in reversed(messages[:-1]):
            if isinstance(message, HumanMessage):
                match = _CODENAME_STATEMENT.search(str(message.content))
                if match:
                    return f"The project codename is {match.group(1)}."
        return "No project codename has been stored in this thread."

    @staticmethod
    def _tool_call(name: str, arguments: dict[str, Any]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": arguments,
                    "id": f"call_{uuid4().hex}",
                    "type": "tool_call",
                }
            ],
        )

    @classmethod
    def _read_file_call(cls, file_path: str) -> AIMessage:
        return cls._tool_call(
            "read_file",
            {"file_path": file_path, "offset": 0, "limit": 1000},
        )


def azure_token_provider(settings: AgentModelSettings):
    if settings.azure_openai_credential == "azure-cli":
        credential = AzureCliCredential(
            tenant_id=settings.azure_openai_tenant_id or "", process_timeout=30
        )
    else:
        credential = DefaultAzureCredential()
    return get_bearer_token_provider(credential, settings.azure_openai_token_scope)


def load_agent_model(
    settings: AgentModelSettings | None = None,
) -> tuple[BaseChatModel, str]:
    """Return the configured provider model or the offline demo model."""

    settings = settings or load_agent_settings()
    model_name = settings.model
    if not model_name:
        model = DemoAgentModel()
        return model, model.model_name
    if model_name.startswith("azure_openai:"):
        provider_kwargs: dict[str, Any] = {}
        if settings.azure_openai_auth == "entra-id":
            api_key = azure_token_provider(settings)
        else:
            configured_api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            if not configured_api_key:
                raise SettingsError(
                    "AZURE_OPENAI_API_KEY is required when SAGE_AZURE_OPENAI_AUTH=api-key"
                )
            api_key = SecretStr(configured_api_key)
            endpoint_host = urlparse(settings.azure_openai_endpoint or "").hostname or ""
            if endpoint_host.casefold().endswith(".azure-api.net"):
                provider_kwargs["default_headers"] = {"api-key": configured_api_key}
        model = ChatOpenAI(
            model=settings.azure_deployment_name(),
            base_url=settings.azure_v1_base_url(),
            api_key=api_key,
            use_responses_api=True,
            **provider_kwargs,
            **settings.model_kwargs(),
        )
        return model, model_name
    return init_chat_model(model_name, **settings.model_kwargs()), model_name
