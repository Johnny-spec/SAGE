"""Message routing and normalized Skill execution."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from time import perf_counter

from sage.agent_factory import AgentRuntimeFactory
from sage.agent_runtime import AgentRunResult
from sage.api_models import MessageRequest, MessageResponse, RouteInfo, UsageInfo
from sage.arguments import build_skill_arguments
from sage.attachments import AttachmentRecord, AttachmentStore
from sage.capabilities import CapabilityViewResolver
from sage.domain import Principal, RuntimeContext
from sage.domain.context import STARTUP_GENERATION_ID
from sage.errors import (
    AttachmentNotFoundError,
    ExecutionError,
    PolicyDeniedError,
    SageError,
    TargetNotFoundError,
)
from sage.executor import SkillExecutor
from sage.plans import PlanStore
from sage.results import (
    ResultUnavailableError,
    build_result_view_from_response,
    result_available_data,
)
from sage.routing import RouteDecision, RuleRouter
from sage.runs import RunEvent, RunStore


@dataclass(frozen=True, slots=True)
class PreparedRun:
    decision: RouteDecision
    arguments: dict[str, object]


class MessageService:
    def __init__(
        self,
        router: RuleRouter,
        executor: SkillExecutor,
        attachments: AttachmentStore,
        agent_runtimes: AgentRuntimeFactory,
        capabilities: CapabilityViewResolver,
        runs: RunStore,
        generation_id: str = STARTUP_GENERATION_ID,
        solution_id: str | None = None,
        solution_release_id: str | None = None,
        plans: PlanStore | None = None,
    ) -> None:
        self._router = router
        self._executor = executor
        self._attachments = attachments
        self._agent_runtimes = agent_runtimes
        self._capabilities = capabilities
        self._runs = runs
        self._generation_id = generation_id
        self._solution_id = solution_id
        self._solution_release_id = solution_release_id
        self._plans = plans

    async def run(
        self,
        request: MessageRequest,
        *,
        principal: Principal,
        parent_run_id: str | None = None,
        parent_context: RuntimeContext | None = None,
        approved_capability_ids: frozenset[str] = frozenset(),
        run_id: str | None = None,
        input_data: Mapping[str, object] | None = None,
        started_event: asyncio.Event | None = None,
    ) -> MessageResponse:
        started = perf_counter()
        resolved_parent_run_id = (
            parent_context.run_id if parent_context is not None else parent_run_id
        )
        agent_id = (
            request.agent_id or self._agent_runtimes.catalog.default_agent_id
            if request.mode == "agent"
            else None
        )
        context = RuntimeContext.create(
            mode=request.mode,
            principal=principal,
            run_id=run_id,
            thread_id=request.thread_id,
            lifecycle=(
                parent_context.lifecycle if parent_context is not None else "interactive"
            ),
            generation_id=self._generation_id,
            solution_id=self._solution_id,
            solution_release_id=self._solution_release_id,
            agent_id=agent_id,
            root_run_id=(
                parent_context.root_run_id or parent_context.run_id
                if parent_context is not None
                else None
            ),
            parent_run_id=resolved_parent_run_id,
            approved_capability_ids=approved_capability_ids,
        )
        run_input = dict(input_data or self._input_projection(request))
        await self._runs.begin(
            context,
            input_data=run_input,
        )
        if started_event is not None:
            started_event.set()
        try:
            if request.mode == "agent":
                agent_runtime = self._agent_runtimes.get(request.agent_id)
                records = self._resolve_attachments(request, context)
                result = await agent_runtime.run(request.message, context, records)
                if self._plans is not None and result.plan:
                    self._plans.project_todos(context, result.plan)
                response = self._agent_completed(result, started, context)
            else:
                prepared = self._prepare(request, context)
                if prepared is None:
                    response = self._clarification(started, context)
                else:
                    result = await self._executor.execute(
                        prepared.decision.definition,
                        prepared.arguments,
                        context=context,
                    )
                    response = self._completed(
                        prepared.decision,
                        result,
                        started,
                        context,
                    )
            await self._record_completion(context, response, run_input)
            return response
        except SageError as exc:
            await self._runs.append(
                context.run_id,
                "run.failed",
                {
                    "run_id": context.run_id,
                    "code": self._error_code(exc),
                    "message": str(exc),
                },
            )
            raise
        except asyncio.CancelledError:
            await self._runs.append(
                context.run_id,
                "run.cancelled",
                {"run_id": context.run_id, "code": "operation_cancelled"},
            )
            raise

    async def stream(
        self,
        request: MessageRequest,
        *,
        principal: Principal,
    ) -> AsyncIterator[RunEvent]:
        started = perf_counter()
        context = RuntimeContext.create(
            mode=request.mode,
            principal=principal,
            thread_id=request.thread_id,
            generation_id=self._generation_id,
            solution_id=self._solution_id,
            solution_release_id=self._solution_release_id,
            agent_id=(
                request.agent_id or self._agent_runtimes.catalog.default_agent_id
                if request.mode == "agent"
                else None
            ),
        )
        run_input = self._input_projection(request)
        started_event = await self._runs.begin(
            context,
            input_data=run_input,
        )
        yield started_event
        try:
            if request.mode == "agent":
                agent_runtime = self._agent_runtimes.get(request.agent_id)
                records = self._resolve_attachments(request, context)
                completed: AgentRunResult | None = None
                yield (
                    await self._record_event(
                        context,
                        "agent.started",
                        {
                            "run_id": context.run_id,
                            "thread_id": context.thread_id,
                            "model": agent_runtime.model_name,
                        },
                    )
                )
                async for event, result in agent_runtime.stream(
                    request.message,
                    context,
                    records,
                ):
                    completed = result or completed
                    if event.name == "plan.updated" and self._plans is not None:
                        todos = event.data.get("todos")
                        if isinstance(todos, list):
                            self._plans.project_todos(
                                context,
                                [dict(item) for item in todos if isinstance(item, dict)],
                            )
                    yield (
                        await self._record_event(
                            context,
                            event.name,
                            {"run_id": context.run_id, **event.data},
                        )
                    )
                if completed is None:
                    raise ExecutionError("Agent run did not produce a result")
                response = self._agent_completed(completed, started, context)
                for event in await self._record_completion(context, response, run_input):
                    yield event
                return

            prepared = self._prepare(request, context)
            if prepared is None:
                response = self._clarification(started, context)
                for event in await self._record_completion(context, response, run_input):
                    yield event
                return

            decision = prepared.decision
            yield (
                await self._record_event(
                    context,
                    "route.selected",
                    {
                        "run_id": context.run_id,
                        "skill": decision.definition.name,
                        "method": decision.method,
                        "reason": decision.reason,
                    },
                )
            )
            yield (
                await self._record_event(
                    context,
                    "skill.started",
                    {
                        "run_id": context.run_id,
                        "skill": decision.definition.name,
                        "version": decision.definition.version,
                    },
                )
            )
            result = await self._executor.execute(
                decision.definition,
                prepared.arguments,
                context=context,
            )
            yield (
                await self._record_event(
                    context,
                    "skill.completed",
                    {
                        "run_id": context.run_id,
                        "skill": decision.definition.name,
                        "ok": bool(result.get("ok", True)),
                    },
                )
            )
            response = self._completed(decision, result, started, context)
            for event in await self._record_completion(context, response, run_input):
                yield event
        except SageError as exc:
            yield (
                await self._record_event(
                    context,
                    "run.failed",
                    {
                        "run_id": context.run_id,
                        "code": self._error_code(exc),
                        "message": str(exc),
                    },
                )
            )

    async def _record_event(
        self,
        context: RuntimeContext,
        name: str,
        data: dict[str, object],
    ) -> RunEvent:
        return await self._runs.append(context.run_id, name, data)

    async def _record_completion(
        self,
        context: RuntimeContext,
        response: MessageResponse,
        input_data: Mapping[str, object],
    ) -> tuple[RunEvent, ...]:
        response_data = response.model_dump(mode="json")
        events: list[RunEvent] = []
        try:
            view = build_result_view_from_response(context, input_data, response_data)
            events.append(
                await self._runs.append(
                    context.run_id,
                    "result.available",
                    result_available_data(view),
                )
            )
        except ResultUnavailableError as exc:
            events.append(
                await self._runs.append(
                    context.run_id,
                    "result.projection.failed",
                    {
                        "run_id": context.run_id,
                        "code": "result_projection_failed",
                        "message": str(exc),
                    },
                )
            )
        events.append(
            await self._runs.append(
                context.run_id,
                "run.completed",
                {"run_id": context.run_id, "response": response_data},
            )
        )
        return tuple(events)

    @staticmethod
    def _input_projection(request: MessageRequest) -> dict[str, object]:
        return {
            "kind": "message",
            "request": request.model_dump(mode="json", exclude_none=True),
        }

    def _prepare(
        self,
        request: MessageRequest,
        context: RuntimeContext,
    ) -> PreparedRun | None:
        records = self._resolve_attachments(request, context)
        route_types = tuple(route_type for record in records for route_type in record.route_types)
        capabilities = self._capabilities.resolve(context)
        decision = self._router.route(
            capabilities,
            request.message,
            request.skill,
            route_types,
        )
        if decision is None:
            return None

        arguments = build_skill_arguments(
            decision.definition.manifest.spec.input_schema,
            request.message,
            request.arguments,
            records,
            tuple(decision.definition.manifest.spec.routing.attachment_types),
        )
        return PreparedRun(decision, arguments)

    @staticmethod
    def _clarification(started: float, context: RuntimeContext) -> MessageResponse:
        return MessageResponse(
            status="clarification_required",
            message="I could not select one Skill safely. Specify a Skill or clarify the request.",
            run_id=context.run_id,
            thread_id=context.thread_id,
            usage=UsageInfo(duration_ms=int((perf_counter() - started) * 1000)),
        )

    @staticmethod
    def _completed(
        decision: RouteDecision,
        result: dict[str, object],
        started: float,
        context: RuntimeContext,
    ) -> MessageResponse:
        return MessageResponse(
            status="completed",
            message=f"Executed Skill '{decision.definition.name}'.",
            run_id=context.run_id,
            thread_id=context.thread_id,
            route=RouteInfo(
                skill=decision.definition.name,
                method=decision.method,
                reason=decision.reason,
            ),
            result=result,
            usage=UsageInfo(
                model_calls=0,
                tool_calls=1,
                duration_ms=int((perf_counter() - started) * 1000),
            ),
        )

    @staticmethod
    def _agent_completed(
        result: AgentRunResult,
        started: float,
        context: RuntimeContext,
    ) -> MessageResponse:
        return MessageResponse(
            status="completed",
            message=result.answer,
            run_id=context.run_id,
            thread_id=context.thread_id,
            route=RouteInfo(
                skill="deep-agent",
                method="llm",
                reason="Deep Agent planned and executed governed Skill tools.",
            ),
            result={
                "plan": result.plan,
                "skills_used": result.skills_used,
                "tool_results": result.tool_results,
                "subagents": result.subagents_used,
            },
            usage=UsageInfo(
                model_calls=result.model_calls,
                tool_calls=result.tool_calls,
                subagent_calls=result.subagent_calls,
                duration_ms=int((perf_counter() - started) * 1000),
            ),
        )

    def _resolve_attachments(
        self,
        request: MessageRequest,
        context: RuntimeContext,
    ) -> tuple[AttachmentRecord, ...]:
        artifact_ids = tuple(dict.fromkeys((*request.attachment_ids, *request.artifact_ids)))
        return tuple(
            self._attachments.get(
                item,
                workspace_id=context.principal.workspace_id,
                solution_id=context.solution_id,
                enforce_scope=True,
            )
            for item in artifact_ids
        )

    @staticmethod
    def _error_code(error: SageError) -> str:
        if isinstance(error, TargetNotFoundError):
            return "target_not_found"
        if isinstance(error, AttachmentNotFoundError):
            return "attachment_not_found"
        if isinstance(error, PolicyDeniedError):
            return "policy_denied"
        if isinstance(error, ExecutionError):
            return "execution_failed"
        return "run_failed"
