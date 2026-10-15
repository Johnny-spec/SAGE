"""Immutable runtime snapshots and active Generation resolution."""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel

from sage.adapters import (
    AdapterRegistry,
    BuiltinAdapter,
    HttpAdapter,
    McpAdapter,
)
from sage.adapters.base import SkillAdapter
from sage.adapters.builtin import BuiltinHandler
from sage.adapters.szyd import runtime_python_adapters
from sage.agent_catalog import AgentCatalog
from sage.agent_drafts import agent_from_dict
from sage.agent_factory import AgentRuntimeFactory
from sage.agent_invocations import AgentTask, AgentTaskResult
from sage.api_models import MessageRequest, MessageResponse
from sage.attachments import AttachmentStore
from sage.capabilities import CapabilityViewResolver
from sage.control_models import GenerationRecord, SolutionReleaseRecord
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest, solution_release_digest
from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError, PolicyDeniedError, TargetNotFoundError
from sage.executor import SkillExecutor
from sage.gateways import HttpEndpointRegistry, McpGatewayClient
from sage.invocations import SqliteInvocationStore
from sage.memory import MemoryStore
from sage.plans import PlanStore
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry
from sage.result_validation import ResultValidatorRegistry
from sage.routing import RuleRouter
from sage.runs import RunEvent, RunStore
from sage.service import MessageService
from sage.settings import AgentModelSettings
from sage.solution_packages import validate_release_assets
from sage.tools import ToolExecutor


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    generation: GenerationRecord
    solution_id: str | None
    solution_release: SolutionReleaseRecord | None
    registry: SkillRegistry
    agent_catalog: AgentCatalog
    capabilities: CapabilityViewResolver
    executor: SkillExecutor
    agent_runtimes: AgentRuntimeFactory
    message_service: MessageService
    tool_executor: ToolExecutor | None = None


class RuntimeManager:
    def __init__(
        self,
        store: LocalControlStore,
        attachments: AttachmentStore,
        runs: RunStore,
        *,
        agent_model: BaseChatModel | None = None,
        agent_model_name: str | None = None,
        agent_settings: AgentModelSettings | None = None,
        mcp_gateway: McpGatewayClient | None = None,
        http_endpoints: HttpEndpointRegistry | None = None,
        memory_store: MemoryStore | None = None,
        plan_store: PlanStore | None = None,
        builtin_handlers: Mapping[str, BuiltinHandler] | None = None,
        result_validators: ResultValidatorRegistry | None = None,
        checkpointer: Any | None = None,
        invocation_store: SqliteInvocationStore | None = None,
        python_adapter: SkillAdapter | None = None,
        allow_local_subprocess: bool = True,
    ) -> None:
        self._store = store
        self._attachments = attachments
        self._runs = runs
        self._agent_model = agent_model
        self._agent_model_name = agent_model_name
        self._agent_settings = agent_settings
        self._mcp_gateway = mcp_gateway
        self._http_endpoints = http_endpoints
        self._memory_store = memory_store
        self._plan_store = plan_store
        self._builtin_handlers = builtin_handlers
        self._result_validators = result_validators
        self._checkpointer = checkpointer
        self._invocation_store = invocation_store
        self._python_adapter = python_adapter
        self._allow_local_subprocess = allow_local_subprocess
        self._snapshots: dict[tuple[str, str | None], RuntimeSnapshot] = {}
        self._lock = RLock()
        active_id = self._store.snapshot().active_generation_id
        self._active = self.get(active_id)

    @property
    def active(self) -> RuntimeSnapshot:
        with self._lock:
            return self._active

    def get(self, generation_id: str, solution_id: str | None = None) -> RuntimeSnapshot:
        with self._lock:
            cache_key = (generation_id, solution_id)
            cached = self._snapshots.get(cache_key)
            if cached is not None:
                return cached
            generation = self._store.snapshot().generations.get(generation_id)
            if generation is None:
                raise KeyError(generation_id)
            snapshot = self._build(generation, solution_id)
            self._snapshots[cache_key] = snapshot
            return snapshot

    def activate(self, generation_id: str) -> RuntimeSnapshot:
        snapshot = self.get(generation_id)

        def update(state: object) -> None:
            if generation_id not in state.generations:  # type: ignore[attr-defined]
                raise KeyError(generation_id)
            state.active_generation_id = generation_id  # type: ignore[attr-defined]
            active_solution_ids = set(  # type: ignore[attr-defined]
                state.generations[generation_id].solution_releases
            )
            for solution_id, solution in state.solutions.items():  # type: ignore[attr-defined]
                if solution.status == "disabled":
                    continue
                solution.status = "active" if solution_id in active_solution_ids else "draft"

        self._store.update(update)
        with self._lock:
            self._active = snapshot
        return snapshot

    def resolve_active_solution(
        self,
        solution_id: str | None,
        principal: Principal,
        *,
        required_profile: str | None = None,
    ) -> RuntimeSnapshot:
        state = self._store.snapshot()
        generation = state.generations[state.active_generation_id]
        self._require_solution_access(
            state,
            generation,
            solution_id,
            principal,
            required_profile=required_profile,
        )
        return self.get(generation.generation_id, solution_id)

    def resolve(
        self,
        request: MessageRequest,
        principal: Principal,
    ) -> tuple[RuntimeSnapshot, MessageRequest]:
        thread_id = request.thread_id or f"thr_{uuid4().hex}"
        solution_scope = request.solution_id or "_legacy"
        binding_key = ":".join(
            (principal.workspace_id, principal.principal_id, solution_scope, thread_id)
        )
        legacy_binding_key = ":".join((principal.workspace_id, principal.principal_id, thread_id))

        def bind(state: object) -> str:
            bindings = state.thread_bindings  # type: ignore[attr-defined]
            generation_id = bindings.get(binding_key)
            if generation_id is None and request.solution_id is None:
                generation_id = bindings.get(legacy_binding_key)
            if generation_id is None:
                generation_id = state.active_generation_id  # type: ignore[attr-defined]
            generation = state.generations[generation_id]  # type: ignore[attr-defined]
            required_profile = "interactive" if request.mode == "agent" else "direct"
            self._require_solution_access(
                state,
                generation,
                request.solution_id,
                principal,
                required_profile=required_profile,
            )
            if binding_key not in bindings:
                bindings[binding_key] = generation_id
            return generation_id

        generation_id = self._store.update(bind)
        snapshot = self.get(generation_id, request.solution_id)
        if request.mode == "agent" and request.agent_id:
            try:
                snapshot.agent_catalog.get(request.agent_id)
            except TargetNotFoundError as exc:
                if generation_id != self.active.generation.generation_id:
                    raise TargetNotFoundError(
                        f"Agent not found in this thread's pinned Generation: {request.agent_id}. "
                        "Start a new thread to use the current published Agents."
                    ) from exc
                raise
        return snapshot, request.model_copy(
            update={"thread_id": thread_id}
        )

    async def resolve_child(
        self,
        parent_run_id: str,
        caller: Principal,
        *,
        target_agent_id: str | None = None,
    ) -> tuple[RuntimeSnapshot, RuntimeContext]:
        parent = await self._runs.get(parent_run_id)
        if parent is None or parent.status != "running":
            raise TargetNotFoundError("Parent Run not found")
        same_owner = parent.context.principal.principal_id == caller.principal_id
        delegated_caller = caller.roles.intersection(
            {"workflow_worker", "platform_admin", "capability_admin"}
        )
        if parent.context.principal.workspace_id != caller.workspace_id or (
            not same_owner and not delegated_caller
        ):
            raise TargetNotFoundError("Parent Run not found")
        try:
            snapshot = self.get(
                parent.context.generation_id,
                parent.context.solution_id,
            )
        except KeyError as exc:
            raise TargetNotFoundError("Parent Run Generation not found") from exc
        release = snapshot.solution_release
        resolved_release_id = release.release_id if release is not None else None
        if resolved_release_id != parent.context.solution_release_id:
            raise TargetNotFoundError("Parent Run Solution Release not found")
        if target_agent_id is not None:
            snapshot.agent_catalog.get(target_agent_id)
            if parent.context.agent_id is not None:
                parent_definition = snapshot.agent_catalog.get(parent.context.agent_id)
                allowed_targets = {
                    delegation.target_agent_id for delegation in parent_definition.delegations
                }
                if target_agent_id not in allowed_targets:
                    raise PolicyDeniedError(
                        f"Agent {parent_definition.agent_id} cannot delegate to {target_agent_id}"
                    )
        return snapshot, parent.context

    async def invoke_agent(
        self,
        task: AgentTask,
        caller: Principal,
    ) -> AgentTaskResult:
        snapshot, parent_context = await self.resolve_child(
            task.parent_run_id,
            caller,
            target_agent_id=task.target_agent_id,
        )
        request_digest = canonical_digest(
            {
                "workspace_id": parent_context.principal.workspace_id,
                "principal_id": parent_context.principal.principal_id,
                "generation_id": parent_context.generation_id,
                "solution_release_id": parent_context.solution_release_id,
                "parent_run_id": parent_context.run_id,
                "agent_id": task.target_agent_id,
                "goal": task.goal,
                "input": task.input,
                "artifact_ids": task.artifact_ids,
            }
        )
        invocation_id = (
            "ainv_"
            + canonical_digest(
                {
                    "workspace_id": parent_context.principal.workspace_id,
                    "principal_id": parent_context.principal.principal_id,
                    "parent_run_id": parent_context.run_id,
                    "idempotency_key": task.idempotency_key,
                }
            ).removeprefix("sha256:")[:32]
        )
        if self._invocation_store is not None:
            reservation = self._invocation_store.reserve(invocation_id, request_digest)
            if reservation.status == "completed" and reservation.result_json is not None:
                return AgentTaskResult.model_validate_json(reservation.result_json)
        task_message = task.goal
        if task.input:
            task_message += "\n\nStructured input (JSON):\n" + json.dumps(
                task.input,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        await self._runs.append(
            parent_context.run_id,
            "agent.handoff.started",
            {"invocation_id": invocation_id, "agent_id": task.target_agent_id},
        )
        try:
            response = await snapshot.message_service.run(
                MessageRequest(
                    solution_id=snapshot.solution_id,
                    mode="agent",
                    agent_id=task.target_agent_id,
                    message=task_message,
                    artifact_ids=list(task.artifact_ids),
                ),
                principal=parent_context.principal,
                parent_context=parent_context,
            )
            if response.run_id is None:
                raise ExecutionError("Agent invocation did not produce a Run")
            child_run = await self._runs.get(response.run_id)
            if child_run is None:
                raise ExecutionError("Agent invocation Run was not recorded")
            output: dict[str, object] = {"message": response.message}
            if response.result is not None:
                output["result"] = response.result
            result = AgentTaskResult(
                invocation_id=invocation_id,
                run_id=response.run_id,
                parent_run_id=parent_context.run_id,
                solution_id=child_run.context.solution_id,
                solution_release_id=child_run.context.solution_release_id,
                agent_id=task.target_agent_id,
                output=output,
                usage=response.usage.model_dump(mode="json"),
            )
            await self._runs.append(
                parent_context.run_id,
                "agent.handoff.completed",
                {
                    "invocation_id": invocation_id,
                    "agent_id": task.target_agent_id,
                    "child_run_id": response.run_id,
                },
            )
            if self._invocation_store is not None:
                self._invocation_store.complete(invocation_id, result.model_dump_json())
            return result
        except BaseException:
            if self._invocation_store is not None:
                self._invocation_store.fail(invocation_id, "agent_invocation_failed")
            await self._runs.append(
                parent_context.run_id,
                "agent.handoff.failed",
                {"invocation_id": invocation_id, "agent_id": task.target_agent_id},
            )
            raise

    @staticmethod
    def _require_solution_access(
        state: object,
        generation: GenerationRecord,
        solution_id: str | None,
        principal: Principal,
        *,
        required_profile: str | None,
    ) -> None:
        if solution_id is None:
            if generation.solution_releases:
                raise TargetNotFoundError("Select a Solution before starting a governed run")
            return
        release_id = generation.solution_releases.get(solution_id)
        release = state.solution_releases.get(release_id or "")  # type: ignore[attr-defined]
        if release is None:
            raise TargetNotFoundError(f"Solution not found: {solution_id}")
        if not principal.roles.intersection(release.allowed_roles):
            raise PolicyDeniedError(f"Solution access denied: {solution_id}")
        if required_profile and required_profile not in release.supported_profiles:
            raise PolicyDeniedError(
                f"Solution {solution_id} does not support {required_profile} runs"
            )

    def _build(
        self,
        generation: GenerationRecord,
        solution_id: str | None = None,
    ) -> RuntimeSnapshot:
        registry = SkillRegistry.load(Path(generation.skills_root))
        tools = tuple(generation.tool_definitions.values())
        definitions = tuple(
            agent_from_dict(value) for value in generation.agent_definitions.values()
        )
        release: SolutionReleaseRecord | None = None
        default_agent_id: str | None = generation.default_agent_id
        if solution_id is not None:
            state = self._store.snapshot()
            release_id = generation.solution_releases.get(solution_id)
            release = state.solution_releases.get(release_id or "")
            if release is None or release.solution_id != solution_id:
                raise TargetNotFoundError(f"Solution not found: {solution_id}")
            actual_release_digest = solution_release_digest(
                solution_id=release.solution_id,
                version=release.version,
                entry_agent_id=release.entry_agent_id,
                allowed_roles=release.allowed_roles,
                supported_profiles=release.supported_profiles,
                components=[item.model_dump(mode="json") for item in release.components],
                component_paths=release.component_paths,
                package_metadata=release.package_metadata,
            )
            if actual_release_digest != release.content_digest:
                raise ValueError(f"Solution Release digest mismatch: {release.release_id}")
            validate_release_assets(self._store, release)
            for component in release.components:
                if component.kind == "mcp-tool":
                    tool = generation.tool_definitions.get(component.component_id)
                    if (
                        tool is None
                        or tool.version != component.version
                        or tool.content_digest != component.content_digest
                    ):
                        raise ValueError(
                            f"Solution Release Tool lock mismatch: {component.component_id}"
                        )
                if component.kind == "skill" and (
                    generation.skill_versions.get(component.component_id) != component.version
                    or generation.skill_digests.get(component.component_id)
                    != component.content_digest
                ):
                    raise ValueError(
                        f"Solution Release Skill lock mismatch: {component.component_id}"
                    )
                if component.kind == "agent":
                    agent = generation.agent_definitions.get(component.component_id)
                    if (
                        agent is None
                        or agent.get("version") != component.version
                        or canonical_digest(agent) != component.content_digest
                    ):
                        raise ValueError(
                            f"Solution Release Agent lock mismatch: {component.component_id}"
                        )
            locked_skill_ids = {
                item.component_id for item in release.components if item.kind == "skill"
            }
            locked_agent_ids = {
                item.component_id for item in release.components if item.kind == "agent"
            }
            registry = SkillRegistry.from_definitions(
                definition for definition in registry if definition.name in locked_skill_ids
            )
            definitions = tuple(
                definition for definition in definitions if definition.agent_id in locked_agent_ids
            )
            default_agent_id = release.entry_agent_id
            locked_tool_ids = {
                item.component_id for item in release.components if item.kind == "mcp-tool"
            }
            tools = tuple(tool for tool in tools if tool.tool_id in locked_tool_ids)
        if not definitions and solution_id is None:
            raise ValueError(f"Generation has no published Agents: {generation.generation_id}")
        catalog = AgentCatalog(
            definitions,
            default_agent_id=default_agent_id,
        )
        policy = PolicyEnforcer()
        adapters: dict[str, SkillAdapter] = {
            "builtin": BuiltinAdapter(self._builtin_handlers),
        }
        adapters.update(runtime_python_adapters(
            self._python_adapter, self._allow_local_subprocess, self._store.root, self._attachments,
        ))
        if self._mcp_gateway is not None:
            adapters["mcp"] = McpAdapter(self._mcp_gateway)
        if self._http_endpoints is not None:
            adapters["http"] = HttpAdapter(self._http_endpoints)
        capabilities = CapabilityViewResolver(
            registry,
            policy,
            tools,
            enabled_execution_types={*adapters, "workflow", "model"},
        )
        tool_executor = None
        if tools:
            if self._mcp_gateway is None:
                raise ValueError("Native MCP tools require an MCP gateway")
            tool_executor = ToolExecutor(
                tools,
                generation_id=generation.generation_id,
                gateway=self._mcp_gateway,
                allowed_tool_ids=[tool.tool_id for tool in tools],
                policy=policy,
            )
        executor = SkillExecutor(
            adapter_registry=AdapterRegistry(adapters),
            policy_enforcer=policy,
            result_validators=self._result_validators,
        )
        agent_runtimes = AgentRuntimeFactory(
            registry,
            executor,
            catalog,
            capabilities,
            model=self._agent_model,
            model_name=self._agent_model_name,
            settings=self._agent_settings,
            memory_store=self._memory_store,
            checkpointer=self._checkpointer,
            agent_invoker=self.invoke_agent,
            tool_executor=tool_executor,
        )
        service = MessageService(
            RuleRouter(),
            executor,
            self._attachments,
            agent_runtimes,
            capabilities,
            self._runs,
            generation_id=generation.generation_id,
            solution_id=solution_id,
            solution_release_id=release.release_id if release is not None else None,
            plans=self._plan_store,
        )
        return RuntimeSnapshot(
            generation=generation,
            solution_id=solution_id,
            solution_release=release,
            registry=registry,
            agent_catalog=catalog,
            capabilities=capabilities,
            executor=executor,
            agent_runtimes=agent_runtimes,
            message_service=service,
            tool_executor=tool_executor,
        )


class DynamicMessageService:
    def __init__(self, runtimes: RuntimeManager) -> None:
        self._runtimes = runtimes

    async def run(
        self,
        request: MessageRequest,
        *,
        principal: Principal,
        parent_run_id: str | None = None,
        approved_capability_ids: frozenset[str] = frozenset(),
    ) -> MessageResponse:
        parent_context: RuntimeContext | None = None
        if parent_run_id is None:
            snapshot, resolved = self._runtimes.resolve(request, principal)
            execution_principal = principal
        else:
            snapshot, parent_context = await self._runtimes.resolve_child(
                parent_run_id,
                principal,
                target_agent_id=request.agent_id if request.mode == "agent" else None,
            )
            resolved = request.model_copy(
                update={
                    "solution_id": parent_context.solution_id,
                    "thread_id": request.thread_id or f"thr_{uuid4().hex}",
                }
            )
            execution_principal = parent_context.principal
        return await snapshot.message_service.run(
            resolved,
            principal=execution_principal,
            parent_run_id=parent_run_id,
            parent_context=parent_context,
            approved_capability_ids=approved_capability_ids,
        )

    async def stream(
        self,
        request: MessageRequest,
        *,
        principal: Principal,
    ) -> AsyncIterator[RunEvent]:
        snapshot, resolved = self._runtimes.resolve(request, principal)
        async for event in snapshot.message_service.stream(resolved, principal=principal):
            yield event
