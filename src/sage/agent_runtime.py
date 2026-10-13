"""Deep Agent orchestration over the governed SAGE Skill registry."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.state import StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.tools import ToolRuntime
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from sage.agent_invocations import AgentInvoke, AgentTask
from sage.agent_model import load_agent_model
from sage.arguments import build_skill_arguments
from sage.attachments import AttachmentRecord
from sage.capabilities import CapabilityView, CapabilityViewResolver
from sage.domain import AgentDefinition, RuntimeContext
from sage.errors import ExecutionError, PolicyDeniedError
from sage.execution_approvals import ExecutionApprovals
from sage.executor import SkillExecutor
from sage.memory import MemoryStore, memory_context_text
from sage.models import SkillDefinition
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry
from sage.settings import AgentModelSettings, load_agent_settings
from sage.tools import McpToolDefinition, ToolExecutor


class SkillToolInput(BaseModel):
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Skill arguments. Never include host filesystem paths.",
    )


class DelegationToolInput(BaseModel):
    goal: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    name: str
    data: dict[str, Any]


@dataclass(slots=True)
class AgentRunResult:
    answer: str
    thread_id: str
    plan: list[dict[str, str]]
    skills_used: list[str]
    tool_results: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    subagent_calls: int
    subagents_used: list[str]


@dataclass(slots=True)
class AgentTrace:
    queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    plan: list[dict[str, str]] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    subagent_calls: int = 0
    subagent_runs: dict[str, str] = field(default_factory=dict)
    subagents_used: list[str] = field(default_factory=list)
    delegation_calls: dict[str, int] = field(default_factory=dict)
    file_reads: dict[str, str] = field(default_factory=dict)

    async def emit(self, name: str, data: dict[str, Any]) -> None:
        await self.queue.put(AgentEvent(name=name, data=data))


@dataclass(frozen=True, slots=True)
class AgentContext:
    message: str
    attachments: tuple[AttachmentRecord, ...]
    trace: AgentTrace
    runtime_context: RuntimeContext
    capabilities: CapabilityView


class AgentLifecycleCallback(AsyncCallbackHandler):
    def __init__(self, trace: AgentTrace, model_name: str, coordinator_name: str) -> None:
        self._trace = trace
        self._model_name = model_name
        self._coordinator_name = coordinator_name
        self._model_runs: dict[str, str] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, parent_run_id, tags, kwargs
        self._trace.model_calls += 1
        agent_name = self._agent_name(metadata)
        self._model_runs[str(run_id)] = agent_name
        await self._trace.emit(
            "model.started",
            {
                "model": self._model_name,
                "agent": agent_name,
                "call": self._trace.model_calls,
            },
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, tags, kwargs
        agent_name = self._model_runs.pop(str(run_id), self._coordinator_name)
        await self._trace.emit(
            "model.completed",
            {"model": self._model_name, "agent": agent_name},
        )
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                for call in message.tool_calls:
                    await self._project_model_tool_call(call)

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        run_key = str(run_id)
        file_path = self._trace.file_reads.pop(run_key, None)
        if file_path is not None and not self._tool_output_failed(output):
            skill_name, is_instructions = self._skill_from_virtual_path(file_path)
            if skill_name is not None:
                event_name = "skill.loaded" if is_instructions else "skill.resource.loaded"
                await self._trace.emit(
                    event_name,
                    {
                        "skill": skill_name,
                        "resource": file_path,
                        "content_bytes": len(str(output).encode("utf-8")),
                    },
                )
        subagent_name = self._trace.subagent_runs.pop(run_key, None)
        if subagent_name is not None:
            await self._trace.emit("subagent.completed", {"agent": subagent_name})

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, tags, metadata, kwargs
        tool_name = serialized.get("name")
        if tool_name == "task":
            self._trace.subagent_runs[str(run_id)] = self._subagent_name(inputs, input_str)
        elif tool_name == "read_file":
            file_path = self._read_file_path(inputs, input_str)
            if file_path is not None:
                self._trace.file_reads[str(run_id)] = file_path

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        self._trace.subagent_runs.pop(str(run_id), None)
        await self._trace.emit(
            "agent.step.failed",
            {"error": type(error).__name__},
        )

    async def _project_model_tool_call(self, call: ToolCall) -> None:
        name = call.get("name")
        arguments = call.get("args", {})
        if name == "write_todos" and isinstance(arguments, dict):
            todos = arguments.get("todos", [])
            if isinstance(todos, list):
                self._trace.plan = [dict(item) for item in todos if isinstance(item, dict)]
                await self._trace.emit("plan.updated", {"todos": self._trace.plan})
        elif name == "task" and isinstance(arguments, dict):
            subagent = str(arguments.get("subagent_type", "subagent"))
            self._trace.subagent_calls += 1
            if subagent not in self._trace.subagents_used:
                self._trace.subagents_used.append(subagent)
            await self._trace.emit("subagent.started", {"agent": subagent})
        elif name == "read_file" and isinstance(arguments, dict):
            file_path = arguments.get("file_path")
            if isinstance(file_path, str):
                skill_name, is_instructions = self._skill_from_virtual_path(file_path)
                if skill_name is not None:
                    await self._trace.emit(
                        "skill.loading",
                        {
                            "skill": skill_name,
                            "resource": file_path,
                            "kind": "instructions" if is_instructions else "resource",
                        },
                    )

    @staticmethod
    def _read_file_path(inputs: dict[str, Any] | None, input_str: str) -> str | None:
        if inputs and isinstance(inputs.get("file_path"), str):
            return inputs["file_path"]
        try:
            parsed = json.loads(input_str)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("file_path"), str):
            return parsed["file_path"]
        return None

    @staticmethod
    def _subagent_name(inputs: dict[str, Any] | None, input_str: str) -> str:
        if inputs and isinstance(inputs.get("subagent_type"), str):
            return inputs["subagent_type"]
        try:
            parsed = json.loads(input_str)
        except json.JSONDecodeError:
            return "subagent"
        if isinstance(parsed, dict) and isinstance(parsed.get("subagent_type"), str):
            return parsed["subagent_type"]
        return "subagent"

    @staticmethod
    def _skill_from_virtual_path(file_path: str) -> tuple[str | None, bool]:
        parts = [part for part in file_path.replace("\\", "/").split("/") if part]
        if len(parts) < 2:
            return None, False
        return parts[0], len(parts) == 2 and parts[1].casefold() == "skill.md"

    @staticmethod
    def _tool_output_failed(output: Any) -> bool:
        status = getattr(output, "status", None)
        if status == "error":
            return True
        return str(getattr(output, "content", output)).casefold().startswith("error")

    def _agent_name(self, metadata: dict[str, Any] | None) -> str:
        if metadata:
            value = metadata.get("lc_agent_name")
            if isinstance(value, str):
                return value
        return self._coordinator_name


class DeepAgentRuntime:
    def __init__(
        self,
        registry: SkillRegistry,
        executor: SkillExecutor,
        definition: AgentDefinition,
        model: BaseChatModel | None = None,
        model_name: str | None = None,
        settings: AgentModelSettings | None = None,
        capabilities: CapabilityViewResolver | None = None,
        memory_store: MemoryStore | None = None,
        checkpointer: Any | None = None,
        agent_invoker: AgentInvoke | None = None,
        tool_executor: ToolExecutor | None = None,
        skill_scope: SkillDefinition | None = None,
        execution_approvals: ExecutionApprovals | None = None,
    ) -> None:
        settings = settings or load_agent_settings()
        if model is None:
            model, configured_name = load_agent_model(settings)
            model_name = model_name or configured_name
        self._model_name = model_name or getattr(model, "model_name", "agent-model")
        self._settings = settings
        self._model = model
        self._executor = executor
        self._tool_executor = tool_executor
        self._definition = definition
        self._memory_store = memory_store
        self._agent_invoker = agent_invoker
        self._capabilities = capabilities or CapabilityViewResolver(
            registry,
            PolicyEnforcer(),
        )
        self._exclusive_delegations = self._definition.exclusive_delegations
        self._skill_scope = skill_scope
        self._execution_approvals = execution_approvals
        skill_roots = {definition.package_root.parent.resolve() for definition in registry}
        if len(skill_roots) > 1:
            raise ExecutionError("All Agent Skills must share one registry root")
        self._skills_backend = (
            StateBackend()
            if skill_scope is not None or not skill_roots
            else FilesystemBackend(root_dir=skill_roots.pop(), virtual_mode=True)
        )
        if self._definition.memory_policy == "none" and execution_approvals is None:
            self._checkpointer = False
        else:
            self._checkpointer = checkpointer or InMemorySaver()
        self._agents: dict[tuple[str, ...], Any] = {}

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def settings(self) -> AgentModelSettings:
        return self._settings

    @property
    def definition(self) -> AgentDefinition:
        return self._definition

    async def run(
        self,
        message: str,
        runtime_context: RuntimeContext,
        attachments: tuple[AttachmentRecord, ...],
    ) -> AgentRunResult:
        result: AgentRunResult | None = None
        async for event, completed in self.stream(message, runtime_context, attachments):
            del event
            if completed is not None:
                result = completed
        if result is None:
            raise ExecutionError("Agent run did not produce a result")
        return result

    async def stream(
        self,
        message: str,
        runtime_context: RuntimeContext,
        attachments: tuple[AttachmentRecord, ...],
    ) -> AsyncIterator[tuple[AgentEvent, AgentRunResult | None]]:
        capabilities = self._capabilities.resolve(
            runtime_context,
            self._definition.capability_ids,
        )
        agent = self._agent_for(capabilities)
        trace = AgentTrace()
        callback = AgentLifecycleCallback(
            trace,
            self._model_name,
            self._definition.name,
        )
        context = AgentContext(
            message=message,
            attachments=attachments,
            trace=trace,
            runtime_context=runtime_context,
            capabilities=capabilities,
        )
        invoke_task = asyncio.create_task(self._invoke(agent, message, context, callback))

        while not invoke_task.done() or not trace.queue.empty():
            try:
                event = await asyncio.wait_for(trace.queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield event, None

        state = await invoke_task
        while not trace.queue.empty():
            yield trace.queue.get_nowait(), None
        completed = AgentRunResult(
            answer=self._answer(state),
            thread_id=runtime_context.thread_id,
            plan=trace.plan,
            skills_used=trace.skills_used,
            tool_results=trace.tool_results,
            model_calls=trace.model_calls,
            tool_calls=trace.tool_calls,
            subagent_calls=trace.subagent_calls,
            subagents_used=trace.subagents_used,
        )
        yield (
            AgentEvent(
                "agent.completed",
                {"thread_id": runtime_context.thread_id},
            ),
            completed,
        )

    async def _invoke(
        self,
        agent: Any,
        message: str,
        context: AgentContext,
        callback: AgentLifecycleCallback,
        resume: bool = False,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        memory_text = self._user_memory_text(context.runtime_context)
        if memory_text is not None:
            messages.append({"role": "user", "content": memory_text})
        messages.append({"role": "user", "content": message})
        inputs: dict[str, Any] = {"messages": messages}
        if self._skill_scope is not None:
            definitions = [self._skill_scope, *context.capabilities.skills]
            inputs["files"] = {
                f"/{definition.name}/SKILL.md": create_file_data(
                    (definition.package_root / "SKILL.md").read_text(encoding="utf-8")
                )
                for definition in definitions
            }
        try:
            return await agent.ainvoke(
                Command(resume=True) if resume else inputs,
                config={
                    "configurable": {
                        "thread_id": self._checkpoint_thread_id(context.runtime_context)
                    },
                    "callbacks": [callback],
                    "recursion_limit": min(
                        self._settings.recursion_limit,
                        self._definition.max_steps,
                    ),
                },
                context=context,
            )
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError(self._safe_agent_error(exc)) from exc

    async def durable_turn(
        self,
        message: str,
        runtime_context: RuntimeContext,
        *,
        resume: bool = False,
        counters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capabilities = self._capabilities.resolve(runtime_context, self._definition.capability_ids)
        trace = AgentTrace(**(counters or {}))
        context = AgentContext(
            message=message,
            attachments=(),
            trace=trace,
            runtime_context=runtime_context,
            capabilities=capabilities,
        )
        state = await self._invoke(
            self._agent_for(capabilities),
            message,
            context,
            AgentLifecycleCallback(trace, self._model_name, self._definition.name),
            resume=resume,
        )
        counters = {
            "model_calls": trace.model_calls,
            "tool_calls": trace.tool_calls,
            "subagent_calls": trace.subagent_calls,
            "delegation_calls": trace.delegation_calls,
            "subagents_used": trace.subagents_used,
        }
        pending = state.get("__interrupt__")
        if pending:
            return {
                "status": "awaiting-approval",
                "tasks": [item.value for item in pending],
                "counters": counters,
            }
        return {"status": "completed", "answer": self._answer(state), "counters": counters}

    def _user_memory_text(self, context: RuntimeContext) -> str | None:
        if (
            self._definition.memory_policy != "thread-and-user"
            or self._memory_store is None
            or context.solution_id is None
        ):
            return None
        records = self._memory_store.list(
            workspace_id=context.principal.workspace_id,
            solution_id=context.solution_id,
            scope="user",
            scope_id=context.principal.principal_id,
        )
        return memory_context_text(records)

    def _agent_for(self, capabilities: CapabilityView) -> Any:
        if self._definition.capability_ids and not any(
            capabilities.contains(capability_id)
            for capability_id in self._definition.capability_ids
        ):
            raise ExecutionError(
                f"Agent {self._definition.agent_id} has no accessible bound capabilities. "
                "Check the published Generation, enabled status, and caller permissions. "
                "Write-risk tools are hidden from ordinary users; only correctly classified "
                "read-only tools are available to anonymous local chat. "
                "This is a capability configuration or authorization issue, "
                "not an MCP connection error."
            )
        cache_key = (
            f"agent:{self._definition.agent_id}@{self._definition.version}",
            f"generation:{capabilities.generation_id}",
            *(
                f"{descriptor.capability_id}@{descriptor.version}"
                for descriptor in capabilities.descriptors
            ),
        )
        cached = self._agents.get(cache_key)
        if cached is not None:
            return cached

        tools_by_id = {
            f"skill:{definition.name}": self._create_skill_tool(definition)
            for definition in capabilities.skills
        }
        tools_by_id.update(
            {tool.tool_id: self._create_native_tool(tool) for tool in capabilities.tools}
        )
        delegation_tools = [
            self._create_delegation_tool(definition) for definition in self._definition.delegations
        ]
        subagents = []
        for definition in self._definition.subagents:
            tools = [
                tools_by_id[capability_id]
                for capability_id in definition.capability_ids
                if capability_id in tools_by_id
            ]
            if not tools:
                continue
            subagents.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "system_prompt": definition.instructions,
                    "tools": tools,
                    "skills": ["/"],
                    "middleware": [
                        FilesystemMiddleware(
                            backend=self._skills_backend,
                            tools=["read_file", "ls", "glob", "grep"],
                        )
                    ],
                }
            )
        agent = create_deep_agent(
            model=self._model,
            tools=[*tools_by_id.values(), *delegation_tools],
            backend=self._skills_backend,
            skills=["/"],
            system_prompt=self._definition.instructions,
            middleware=cast(
                Any,
                [
                    FilesystemMiddleware(
                        backend=self._skills_backend,
                        tools=["read_file", "ls", "glob", "grep"],
                    ),
                    TodoListMiddleware(),
                    ModelCallLimitMiddleware(
                        run_limit=self._definition.max_model_calls,
                        exit_behavior="error",
                    ),
                    ToolCallLimitMiddleware(
                        tool_name="task",
                        run_limit=0 if self._skill_scope else self._definition.max_subagent_calls,
                        exit_behavior="error",
                    ),
                ],
            ),
            subagents=cast(Any, subagents),
            context_schema=AgentContext,
            checkpointer=self._checkpointer,
            name=self._definition.name,
        )
        self._agents[cache_key] = agent
        return agent

    def _checkpoint_thread_id(self, context: RuntimeContext) -> str:
        return ":".join(
            (
                context.principal.workspace_id,
                context.principal.principal_id,
                self._definition.agent_id,
                context.generation_id,
                context.solution_id or "_",
                context.solution_release_id or "_",
                context.thread_id,
            )
        )

    def _create_skill_tool(self, definition: SkillDefinition) -> BaseTool:
        visible_schema = self._visible_input_schema(definition)
        description = (
            f"{definition.description} Governed input schema: "
            f"{json.dumps(visible_schema, ensure_ascii=False)}"
        )

        async def execute_skill(
            arguments: dict[str, Any],
            runtime: ToolRuntime[AgentContext],
        ) -> str:
            trace = runtime.context.trace
            capability_id = f"skill:{definition.name}"
            if trace.tool_calls >= self._definition.max_tool_calls:
                await trace.emit(
                    "tool.failed",
                    {
                        "tool": definition.name,
                        "message": "Governed Skill call budget exceeded",
                    },
                )
                return json.dumps(
                    {
                        "error": "tool_call_budget_exceeded",
                        "skill": definition.name,
                    }
                )
            if not runtime.context.capabilities.contains(capability_id):
                await trace.emit(
                    "tool.failed",
                    {"tool": definition.name, "message": "Capability is not in the run view"},
                )
                return json.dumps(
                    {
                        "error": "capability_not_available",
                        "skill": definition.name,
                    }
                )
            required_path = f"/{definition.name}/SKILL.md"
            if self._skill_scope and not self._was_file_loaded(
                runtime.state, f"/{self._skill_scope.name}/SKILL.md",
            ):
                return json.dumps({
                    "error": "skill_instructions_not_loaded",
                    "required_path": f"/{self._skill_scope.name}/SKILL.md",
                })
            if not self._was_file_loaded(runtime.state, required_path):
                await trace.emit(
                    "skill.load.required",
                    {"skill": definition.name, "resource": required_path},
                )
                return json.dumps(
                    {
                        "error": "skill_instructions_not_loaded",
                        "required_action": (
                            f"Call read_file for {required_path} before invoking this Skill."
                        ),
                    }
                )
            agent_name = self._tool_agent_name(runtime)
            required_agent = self._exclusive_delegations.get(capability_id)
            if required_agent is not None and agent_name != required_agent:
                message = (
                    f"{definition.name} must be delegated to {required_agent}; "
                    "the coordinator must use the returned structured result"
                )
                await trace.emit(
                    "tool.failed",
                    {"tool": definition.name, "message": message},
                )
                return json.dumps(
                    {
                        "error": "subagent_required",
                        "skill": definition.name,
                        "message": message,
                    },
                    ensure_ascii=False,
                )
            await trace.emit("tool.started", {"tool": definition.name})
            try:
                executable_arguments = build_skill_arguments(
                    definition.manifest.spec.input_schema,
                    runtime.context.message,
                    arguments,
                    runtime.context.attachments,
                    tuple(definition.manifest.spec.routing.attachment_types),
                )
                context = runtime.context.runtime_context
                if self._execution_approvals is not None and context.lifecycle == "durable":
                    call_id = str(runtime.tool_call_id)
                    task = self._execution_approvals.prepare(
                        context, definition, executable_arguments, call_id
                    )
                    if task is not None:
                        interrupt({"task_id": task.task_id, "request_digest": task.request_digest})
                    trace.tool_calls += 1
                    result = await self._execution_approvals.dispatch(
                        self._executor, context, definition, executable_arguments, call_id, task
                    )
                else:
                    trace.tool_calls += 1
                    result = await self._executor.execute(
                        definition, executable_arguments, context=context,
                    )
            except (ExecutionError, PolicyDeniedError) as exc:
                await trace.emit(
                    "tool.failed",
                    {"tool": definition.name, "message": str(exc)},
                )
                if runtime.context.runtime_context.lifecycle == "durable":
                    raise
                return json.dumps(
                    {
                        "error": "skill_execution_rejected",
                        "skill": definition.name,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
            safe_result = self._safe_result(result)
            if definition.name not in trace.skills_used:
                trace.skills_used.append(definition.name)
            trace.tool_results.append(
                {
                    "skill": definition.name,
                    "ok": bool(result.get("ok", True)),
                    "result": safe_result if definition.name == "report-summary" else None,
                }
            )
            await trace.emit(
                "tool.completed",
                {"tool": definition.name, "ok": bool(result.get("ok", True))},
            )
            return json.dumps(
                {"skill": definition.name, "result": safe_result},
                ensure_ascii=False,
            )

        return StructuredTool.from_function(
            coroutine=execute_skill,
            name=f"skill_{definition.name.replace('-', '_')}",
            description=description,
            args_schema=SkillToolInput,
            infer_schema=False,
        )

    def _create_native_tool(self, definition: McpToolDefinition) -> BaseTool:
        async def execute_tool(
            arguments: dict[str, Any], runtime: ToolRuntime[AgentContext]
        ) -> str:
            trace = runtime.context.trace
            tool_id = definition.tool_id
            if self._tool_executor is None or not runtime.context.capabilities.contains(tool_id):
                return json.dumps({"error": "capability_not_available", "tool": tool_id})
            if self._skill_scope and not self._was_file_loaded(
                runtime.state, f"/{self._skill_scope.name}/SKILL.md",
            ):
                return json.dumps({
                    "error": "skill_instructions_not_loaded",
                    "required_path": f"/{self._skill_scope.name}/SKILL.md",
                })
            if trace.tool_calls >= self._definition.max_tool_calls:
                return json.dumps({"error": "tool_call_budget_exceeded", "tool": tool_id})
            required_agent = self._exclusive_delegations.get(tool_id)
            if required_agent and self._tool_agent_name(runtime) != required_agent:
                return json.dumps(
                    {"error": "subagent_required", "tool": tool_id, "agent": required_agent}
                )
            await trace.emit("tool.started", {"tool": tool_id, "kind": "mcp-tool"})
            try:
                context = runtime.context.runtime_context
                if self._execution_approvals is not None and context.lifecycle == "durable":
                    call_id = str(runtime.tool_call_id)
                    task = self._execution_approvals.prepare(
                        context, definition, arguments, call_id
                    )
                    if task is not None:
                        interrupt({"task_id": task.task_id, "request_digest": task.request_digest})
                    trace.tool_calls += 1
                    result = await self._execution_approvals.dispatch(
                        self._tool_executor,
                        context,
                        definition,
                        arguments,
                        call_id,
                        task,
                    )
                else:
                    trace.tool_calls += 1
                    result = await self._tool_executor.execute(tool_id, arguments, context=context)
            except (ExecutionError, PolicyDeniedError) as exc:
                await trace.emit("tool.failed", {"tool": tool_id, "message": str(exc)})
                if runtime.context.runtime_context.lifecycle == "durable":
                    raise
                return json.dumps(
                    {"error": "tool_execution_rejected", "tool": tool_id, "message": str(exc)}
                )
            safe_result = self._safe_result(result)
            trace.tool_results.append({"tool": tool_id, "kind": "mcp-tool", "ok": True})
            await trace.emit("tool.completed", {"tool": tool_id, "kind": "mcp-tool", "ok": True})
            return json.dumps({"tool": tool_id, "result": safe_result}, ensure_ascii=False)

        suffix = hashlib.sha256(definition.tool_id.encode("utf-8")).hexdigest()[:24]
        return StructuredTool.from_function(
            coroutine=execute_tool,
            name=f"mcp_{suffix}",
            description=(
                f"{definition.binding.server_alias}/{definition.binding.tool_name}: "
                f"{definition.description}"
            ),
            args_schema={
                "type": "object",
                "properties": {"arguments": definition.input_schema},
                "required": ["arguments"],
                "additionalProperties": False,
            },
            infer_schema=False,
        )

    def _create_delegation_tool(self, definition: Any) -> BaseTool:
        async def delegate_agent(
            goal: str,
            input: dict[str, Any],
            artifact_ids: list[str],
            runtime: ToolRuntime[AgentContext],
        ) -> str:
            trace = runtime.context.trace
            calls = trace.delegation_calls.get(definition.alias, 0)
            if calls >= definition.max_calls or trace.subagent_calls >= (
                self._definition.max_subagent_calls
            ):
                await trace.emit(
                    "subagent.failed",
                    {
                        "agent": definition.target_agent_id,
                        "message": "Governed Agent delegation budget exceeded",
                    },
                )
                return json.dumps(
                    {
                        "error": "agent_delegation_budget_exceeded",
                        "agent": definition.target_agent_id,
                    }
                )
            if self._agent_invoker is None:
                return json.dumps(
                    {
                        "error": "agent_invoker_unavailable",
                        "agent": definition.target_agent_id,
                    }
                )
            call_number = calls + 1
            await trace.emit(
                "subagent.started",
                {
                    "agent": definition.target_agent_id,
                    "alias": definition.alias,
                },
            )
            try:
                durable = runtime.context.runtime_context.lifecycle == "durable"
                task = AgentTask(
                        parent_run_id=runtime.context.runtime_context.run_id,
                        target_agent_id=definition.target_agent_id,
                        goal=goal,
                        input=input,
                        artifact_ids=tuple(artifact_ids),
                        idempotency_key=(
                            f"{runtime.context.runtime_context.run_id}:"
                            f"{runtime.tool_call_id}" if durable else
                            f"{runtime.context.runtime_context.run_id}:"
                            f"{definition.alias}:{call_number}"
                        ),
                    )
                result = await self._agent_invoker(
                    task,
                    runtime.context.runtime_context.principal,
                )
                while result.status == "awaiting-approval":
                    for approval in result.tasks:
                        interrupt(approval)
                    task = task.model_copy(update={"turn_index": task.turn_index + 1})
                    result = await self._agent_invoker(
                        task, runtime.context.runtime_context.principal,
                    )
            except (ExecutionError, PolicyDeniedError) as exc:
                await trace.emit(
                    "subagent.failed",
                    {"agent": definition.target_agent_id, "message": str(exc)},
                )
                if runtime.context.runtime_context.lifecycle == "durable":
                    raise
                return json.dumps(
                    {
                        "error": "agent_delegation_failed",
                        "agent": definition.target_agent_id,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
            trace.delegation_calls[definition.alias] = call_number
            trace.subagent_calls += 1
            if definition.alias not in trace.subagents_used:
                trace.subagents_used.append(definition.alias)
            await trace.emit(
                "subagent.completed",
                {
                    "agent": definition.target_agent_id,
                    "alias": definition.alias,
                    "child_run_id": result.run_id,
                },
            )
            return json.dumps(
                {
                    "agent": definition.target_agent_id,
                    "child_run_id": result.run_id,
                    "output": result.output,
                },
                ensure_ascii=False,
            )

        tool_name = f"delegate_{definition.alias.replace('-', '_')}"
        return StructuredTool.from_function(
            coroutine=delegate_agent,
            name=tool_name,
            description=(
                f"{definition.description} Delegates to the governed published Agent "
                f"{definition.target_agent_id}."
            ),
            args_schema=DelegationToolInput,
            infer_schema=False,
        )

    @staticmethod
    def _was_file_loaded(state: dict[str, Any], required_path: str) -> bool:
        calls = {
            call["id"]: call
            for message in state.get("messages", [])
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if call.get("name") == "read_file"
        }
        for message in state.get("messages", []):
            if not isinstance(message, ToolMessage):
                continue
            call = calls.get(message.tool_call_id)
            if call is None or call.get("args", {}).get("file_path") != required_path:
                continue
            if getattr(message, "status", None) == "error":
                continue
            if not str(message.content).casefold().startswith("error"):
                return True
        return False

    @staticmethod
    def _tool_agent_name(runtime: ToolRuntime[AgentContext]) -> str:
        metadata = runtime.config.get("metadata", {})
        value = metadata.get("lc_agent_name") if isinstance(metadata, dict) else None
        return value if isinstance(value, str) else "sage-coordinator"

    @staticmethod
    def _visible_input_schema(definition: SkillDefinition) -> dict[str, Any]:
        schema = definition.manifest.spec.input_schema
        properties = schema.get("properties", {})
        visible_properties = {
            key: value
            for key, value in properties.items()
            if isinstance(key, str) and not key.casefold().endswith("_path")
        }
        required = [
            key
            for key in schema.get("required", [])
            if isinstance(key, str) and not key.casefold().endswith("_path")
        ]
        return {"type": "object", "properties": visible_properties, "required": required}

    @classmethod
    def _safe_result(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._safe_result(item)
                for key, item in value.items()
                if not cls._private_result_key(str(key))
            }
        if isinstance(value, list):
            return [cls._safe_result(item) for item in value]
        return value

    @staticmethod
    def _private_result_key(key: str) -> bool:
        normalized = key.casefold()
        return normalized in {"path", "source_files"} or normalized.endswith("_path")

    @staticmethod
    def _safe_agent_error(error: Exception) -> str:
        error_name = type(error).__name__
        messages = {
            "AuthenticationError": (
                "Azure OpenAI authentication failed. For api-key check AZURE_OPENAI_API_KEY; "
                "for entra-id check Azure login, tenant and token scope. "
                "Restart after changing settings."
            ),
            "PermissionDeniedError": (
                "Azure OpenAI access denied. Check resource data-plane roles and network access."
            ),
            "ClientAuthenticationError": (
                "Entra token acquisition failed. Check Azure login and tenant."
            ),
            "CredentialUnavailableError": (
                "No Azure credential is available. Sign in using Azure CLI."
            ),
            "NotFoundError": (
                "Azure OpenAI deployment was not found. Check SAGE_AGENT_MODEL and endpoint."
            ),
            "RateLimitError": "Azure OpenAI rate limit was reached. Retry after the quota resets.",
            "APITimeoutError": "Azure OpenAI request timed out. Retry or increase the timeout.",
            "APIConnectionError": (
                "Could not connect to Azure OpenAI. Check the endpoint and network access."
            ),
            "BadRequestError": (
                "Azure OpenAI rejected the Agent request. Check deployment tool-calling support."
            ),
        }
        return messages.get(error_name, f"Agent execution failed safely: {error_name}")

    @staticmethod
    def _answer(state: dict[str, Any]) -> str:
        for message in reversed(state.get("messages", [])):
            if isinstance(message, AIMessage) and message.text:
                return message.text
        return "The agent completed without a final text response."
