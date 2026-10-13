import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sage.adapters import BuiltinAdapter
from sage.agent_definitions import default_diagnostic_agent
from sage.agent_invocations import AgentTask, AgentTaskResult
from sage.agent_model import DemoAgentModel
from sage.agent_runtime import AgentEvent, DeepAgentRuntime
from sage.app import DEFAULT_SKILLS_ROOT
from sage.attachments import AttachmentRecord
from sage.domain import AgentDefinition, DelegationDefinition, Principal, RuntimeContext
from sage.errors import ExecutionError
from sage.executor import SkillExecutor
from sage.memory import SqliteMemoryStore
from sage.reference_solutions.incident_diagnostics import (
    report_summary,
    require_nonempty_iis_metrics,
)
from sage.registry import SkillRegistry
from sage.result_validation import ResultValidatorRegistry

IIS_FIXTURE = Path(__file__).parent / "fixtures" / "iis" / "sample.log"


def _incident_executor() -> SkillExecutor:
    return SkillExecutor(
        builtin_adapter=BuiltinAdapter({"report-summary": report_summary}),
        result_validators=ResultValidatorRegistry(
            {"iis-w3c-nonempty": require_nonempty_iis_metrics}
        ),
    )


def _runtime_context(
    thread_id: str,
    principal_id: str = "anonymous",
) -> RuntimeContext:
    return RuntimeContext.create(
        mode="agent",
        principal=(
            Principal.anonymous()
            if principal_id == "anonymous"
            else Principal(
                workspace_id="ws_default",
                principal_id=principal_id,
                roles=frozenset({"user"}),
                authentication_method="local",
            )
        ),
        thread_id=thread_id,
    )


class ScriptedProviderModel(BaseChatModel):
    bound_tool_names: tuple[str, ...] = ()
    model_name: str = "scripted-provider"

    @property
    def _llm_type(self) -> str:
        return "scripted-provider"

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        del tool_choice, kwargs
        names = tuple(str(getattr(tool, "name", type(tool).__name__)) for tool in tools)
        return self.model_copy(update={"bound_tool_names": names})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        completed = self._completed_tools(messages)
        if "read_file" not in completed:
            message = self._tool_call(
                "read_file",
                {"file_path": "/echo/SKILL.md", "offset": 0, "limit": 1000},
            )
        elif "skill_echo" not in completed:
            message = self._tool_call(
                "skill_echo",
                {"arguments": {"text": "provider parsed the request"}},
            )
        else:
            message = AIMessage(content="Provider synthesized the governed Skill result.")
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _completed_tools(messages: list[BaseMessage]) -> set[str]:
        calls = {
            call["id"]: call["name"]
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        return {
            calls[message.tool_call_id]
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id in calls
        }

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


class EagerProviderModel(ScriptedProviderModel):
    model_name: str = "eager-provider"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        if "skill_echo" not in self._completed_tools(messages):
            message = self._tool_call(
                "skill_echo",
                {"arguments": {"text": "skip loading"}},
            )
        else:
            tool_result = next(
                message for message in reversed(messages) if isinstance(message, ToolMessage)
            )
            message = AIMessage(content=str(tool_result.content))
        return ChatResult(generations=[ChatGeneration(message=message)])


class DoubleSkillProviderModel(ScriptedProviderModel):
    model_name: str = "double-skill-provider"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        completed = self._completed_tools(messages)
        if "read_file" not in completed:
            message = self._tool_call(
                "read_file",
                {"file_path": "/echo/SKILL.md", "offset": 0, "limit": 1000},
            )
        else:
            calls = {
                call["id"]: call["name"]
                for item in messages
                if isinstance(item, AIMessage)
                for call in item.tool_calls
            }
            completed_echoes = sum(
                calls.get(item.tool_call_id) == "skill_echo"
                for item in messages
                if isinstance(item, ToolMessage)
            )
            if completed_echoes < 2:
                message = self._tool_call(
                    "skill_echo",
                    {"arguments": {"text": f"call {completed_echoes + 1}"}},
                )
            else:
                message = AIMessage(content="Finished after the bounded Skill calls.")
        return ChatResult(generations=[ChatGeneration(message=message)])


class DelegatingProviderModel(ScriptedProviderModel):
    model_name: str = "delegating-provider"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        if "delegate_reviewer" not in self._completed_tools(messages):
            message = self._tool_call(
                "delegate_reviewer",
                {
                    "goal": "Independently verify the finding.",
                    "input": {"finding": "The service is healthy."},
                    "artifact_ids": [],
                },
            )
        else:
            message = AIMessage(content="The independent reviewer accepted the finding.")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_deep_agent_plans_delegates_and_runs_multiple_skills() -> None:
    async def scenario() -> tuple[list[object], object]:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            _incident_executor(),
            model=DemoAgentModel(),
            model_name="test-demo-model",
            definition=default_diagnostic_agent(),
        )
        attachment = AttachmentRecord(
            attachment_id="fixture",
            filename="sample.log",
            path=IIS_FIXTURE,
            size=IIS_FIXTURE.stat().st_size,
            media_type="text/iis-w3c",
            suffix=".log",
        )
        events = []
        result = None
        async for event, completed in runtime.stream(
            "Use a subagent to analyze this IIS log and create a concise report",
            _runtime_context("agent-runtime-test"),
            (attachment,),
        ):
            events.append(event)
            result = completed or result
        return events, result

    events, result = asyncio.run(scenario())
    event_names = [event.name for event in events]

    assert result is not None
    assert "plan.updated" in event_names
    assert "subagent.started" in event_names
    assert "subagent.completed" in event_names
    assert event_names.count("skill.loading") == 3
    assert event_names.count("skill.loaded") == 2
    assert event_names.count("skill.resource.loaded") == 1
    assert event_names.count("tool.started") == 2
    assert event_names.index("skill.loaded") < event_names.index("tool.started")
    resource_event = next(event for event in events if event.name == "skill.resource.loaded")
    assert resource_event.data["resource"] == "/iis-logs/references/triage.md"
    assert result.skills_used == ["iis-logs", "report-summary"]
    assert result.tool_calls == 2
    assert result.subagent_calls == 1
    assert result.model_calls >= 10
    assert "Analyzed 5 IIS requests" in result.answer


def test_agent_invokes_published_delegation_through_governed_callback() -> None:
    tasks: list[AgentTask] = []

    async def invoke_agent(task: AgentTask, principal: Principal) -> AgentTaskResult:
        tasks.append(task)
        return AgentTaskResult(
            invocation_id="ainv_1",
            run_id="run_child",
            parent_run_id=task.parent_run_id,
            agent_id=task.target_agent_id,
            output={"verdict": "PASS"},
            usage={
                "model_calls": 1,
                "tool_calls": 0,
                "subagent_calls": 0,
                "duration_ms": 10,
            },
        )

    definition = AgentDefinition(
        agent_id="coordinator",
        name="Coordinator",
        version="1.0.0",
        instructions="Use the reviewer delegation to verify the finding.",
        capability_ids=(),
        delegations=(
            DelegationDefinition(
                alias="reviewer",
                target_agent_id="review-agent",
                description="Independently verify a proposed finding.",
            ),
        ),
    )
    runtime = DeepAgentRuntime(
        SkillRegistry.load(DEFAULT_SKILLS_ROOT),
        SkillExecutor(),
        model=DelegatingProviderModel(),
        model_name="provider:delegating",
        definition=definition,
        agent_invoker=invoke_agent,
    )
    events: list[AgentEvent] = []
    result = None

    async def scenario() -> None:
        nonlocal result
        async for event, completed in runtime.stream(
            "Verify this finding independently.",
            _runtime_context("delegation-test"),
            (),
        ):
            events.append(event)
            result = completed or result

    asyncio.run(scenario())

    assert result is not None
    assert len(tasks) == 1
    task = tasks[0]
    assert task.target_agent_id == "review-agent"
    assert task.idempotency_key.endswith(":reviewer:1")
    assert result.subagent_calls == 1
    assert result.subagents_used == ["reviewer"]
    names = [event.name for event in events]
    assert names.count("subagent.started") == 1
    assert names.count("subagent.completed") == 1


def test_provider_style_model_runs_real_progressive_skill_loop() -> None:
    async def scenario() -> tuple[list[object], object, str]:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            SkillExecutor(),
            model=ScriptedProviderModel(),
            model_name="provider:test-model",
            definition=default_diagnostic_agent(),
        )
        events = []
        result = None
        async for event, completed in runtime.stream(
            "Parse this request and echo a governed result",
            _runtime_context("provider-runtime-test"),
            (),
        ):
            events.append(event)
            result = completed or result
        return events, result, runtime.model_name

    events, result, model_name = asyncio.run(scenario())
    event_names = [event.name for event in events]

    assert result is not None
    assert model_name == "provider:test-model"
    assert event_names.count("model.started") == 3
    assert event_names.count("skill.loading") == 1
    assert event_names.count("skill.loaded") == 1
    assert event_names.count("tool.started") == 1
    assert event_names.index("skill.loaded") < event_names.index("tool.started")
    assert result.skills_used == ["echo"]
    assert result.model_calls == 3
    assert result.answer == "Provider synthesized the governed Skill result."


def test_provider_cannot_execute_skill_before_loading_instructions() -> None:
    async def scenario() -> tuple[list[str], object]:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            SkillExecutor(),
            model=EagerProviderModel(),
            model_name="provider:eager-model",
            definition=default_diagnostic_agent(),
        )
        events = []
        result = None
        async for event, completed in runtime.stream(
            "Execute echo without loading instructions",
            _runtime_context("provider-load-gate-test"),
            (),
        ):
            events.append(event.name)
            result = completed or result
        return events, result

    events, result = asyncio.run(scenario())

    assert result is not None
    assert "skill.load.required" in events
    assert "tool.started" not in events
    assert result.skills_used == []
    assert result.tool_calls == 0
    assert "skill_instructions_not_loaded" in result.answer


def test_authentication_errors_are_safe_and_actionable() -> None:
    authentication_error = type("AuthenticationError", (Exception,), {})()

    message = DeepAgentRuntime._safe_agent_error(authentication_error)

    assert "authentication failed" in message
    assert "AZURE_OPENAI_API_KEY" in message


def test_agent_checkpoint_is_isolated_by_principal() -> None:
    async def scenario() -> str:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            SkillExecutor(),
            model=DemoAgentModel(),
            model_name="test-demo-model",
            definition=default_diagnostic_agent(),
        )
        await runtime.run(
            "Remember that the project codename is Orion",
            _runtime_context("shared-thread", "usr_alice"),
            (),
        )
        result = await runtime.run(
            "What is the project codename?",
            _runtime_context("shared-thread", "usr_bob"),
            (),
        )
        return result.answer

    assert asyncio.run(scenario()) == "No project codename has been stored in this thread."


def test_agent_memory_policy_none_disables_thread_checkpoint() -> None:
    async def scenario() -> str:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            SkillExecutor(),
            model=DemoAgentModel(),
            model_name="test-demo-model",
            definition=replace(default_diagnostic_agent(), memory_policy="none"),
        )
        await runtime.run(
            "Remember that the project codename is Orion",
            _runtime_context("memory-disabled"),
            (),
        )
        result = await runtime.run(
            "What is the project codename?",
            _runtime_context("memory-disabled"),
            (),
        )
        return result.answer

    assert asyncio.run(scenario()) == "No project codename has been stored in this thread."


def test_agent_reads_explicit_user_memory_only_within_solution(tmp_path: Path) -> None:
    store = SqliteMemoryStore(str(tmp_path / "memory.sqlite3"))
    store.put(
        workspace_id="ws_default",
        solution_id="enterprise-qa",
        scope="user",
        scope_id="anonymous",
        key="project",
        value="project codename is Orion",
        created_by="anonymous",
    )
    runtime = DeepAgentRuntime(
        SkillRegistry.load(DEFAULT_SKILLS_ROOT),
        SkillExecutor(),
        model=DemoAgentModel(),
        model_name="test-demo-model",
        definition=replace(default_diagnostic_agent(), memory_policy="thread-and-user"),
        memory_store=store,
    )

    async def ask(solution_id: str) -> str:
        context = RuntimeContext.create(
            mode="agent",
            principal=Principal.anonymous(),
            thread_id=f"memory-{solution_id}",
            solution_id=solution_id,
            solution_release_id=f"{solution_id}@1.0.0",
        )
        result = await runtime.run("What is the project codename?", context, ())
        return result.answer

    assert asyncio.run(ask("enterprise-qa")) == "The project codename is Orion."
    assert asyncio.run(ask("contract-review")) == (
        "No project codename has been stored in this thread."
    )


def test_agent_model_call_budget_is_enforced() -> None:
    runtime = DeepAgentRuntime(
        SkillRegistry.load(DEFAULT_SKILLS_ROOT),
        _incident_executor(),
        model=EagerProviderModel(),
        model_name="provider:eager-model",
        definition=replace(default_diagnostic_agent(), max_model_calls=1),
    )

    with pytest.raises(ExecutionError):
        asyncio.run(
            runtime.run(
                "Keep trying after the first model call",
                _runtime_context("model-budget"),
                (),
            )
        )


def test_agent_governed_skill_call_budget_is_enforced() -> None:
    async def scenario() -> tuple[list[object], object]:
        runtime = DeepAgentRuntime(
            SkillRegistry.load(DEFAULT_SKILLS_ROOT),
            _incident_executor(),
            model=DoubleSkillProviderModel(),
            model_name="provider:double-skill",
            definition=replace(default_diagnostic_agent(), max_tool_calls=1),
        )
        events = []
        result = None
        async for event, completed in runtime.stream(
            "Execute echo twice",
            _runtime_context("tool-budget"),
            (),
        ):
            events.append(event)
            result = completed or result
        return events, result

    events, result = asyncio.run(scenario())

    assert result is not None
    assert result.tool_calls == 1
    assert any(
        event.name == "tool.failed"
        and event.data["message"] == "Governed Skill call budget exceeded"
        for event in events
    )
