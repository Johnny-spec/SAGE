"""Standalone Skill execution through the existing Temporal worker and Agent runtime."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from sage.agent_invocations import AgentTask, AgentTaskResult
from sage.api_models import (
    DurableInvocationInput,
    DurableInvocationResponse,
    InvocationRequest,
    InvocationResult,
)
from sage.capabilities import skill_descriptor
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest, directory_digest
from sage.domain import Principal, RuntimeContext
from sage.durable import DurableExecutionPort, DurableRunRequest
from sage.errors import ExecutionError
from sage.execution_approvals import ExecutionApprovals
from sage.human_tasks import SqliteHumanTaskStore
from sage.invocations import (
    InvocationConflictError,
    InvocationInProgressError,
    SqliteInvocationStore,
)
from sage.policy import PolicyEnforcer
from sage.results import (
    ResultUnavailableError,
    build_result_view_from_output,
    result_available_data,
)
from sage.runs import RunStore
from sage.runtime_manager import RuntimeManager, RuntimeSnapshot
from sage.workflows import load_workflow_template


class SkillOrchestrator:
    def __init__(
        self,
        store: LocalControlStore,
        runtimes: RuntimeManager,
        runs: RunStore,
        invocations: SqliteInvocationStore,
        backend: DurableExecutionPort | None,
        task_queue: str,
        tasks: SqliteHumanTaskStore,
    ) -> None:
        self.store = store
        self.runtimes = runtimes
        self.runs = runs
        self.invocations = invocations
        self.backend = backend
        self.task_queue = task_queue
        self.approvals = ExecutionApprovals(tasks, invocations, runs)
        self._model_runtimes: dict[str, Any] = {}

    def definition(self, snapshot: RuntimeSnapshot, name: str):
        definition = snapshot.registry.get(name)
        if definition is None:
            raise HTTPException(status_code=404, detail="Skill is not in the locked catalog")
        if directory_digest(definition.package_root) != snapshot.generation.skill_digests.get(name):
            raise HTTPException(status_code=409, detail="Skill package changed after publication")
        return definition

    async def start_tool(
        self,
        payload: InvocationRequest,
        snapshot: RuntimeSnapshot,
        principal: Principal,
    ) -> InvocationResult:
        if self.backend is None:
            raise HTTPException(
                status_code=503, detail="High-risk calls require Temporal and a worker"
            )
        if not payload.idempotency_key:
            raise HTTPException(status_code=422, detail="High-risk calls require idempotency_key")
        tool = next(
            (
                item
                for item in snapshot.capabilities.resolve(
                    RuntimeContext.create(mode="direct", principal=principal),
                ).tools
                if item.tool_id == payload.capability_id
            ),
            None,
        )
        if tool is None:
            raise HTTPException(status_code=404, detail="Native Tool unavailable")
        try:
            Draft202012Validator(tool.input_schema).validate(payload.input)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="Tool input schema mismatch") from exc
        identity = canonical_digest(
            {
                "workspace": principal.workspace_id,
                "owner": principal.principal_id,
                "solution": snapshot.solution_id,
                "key": payload.idempotency_key,
            }
        ).removeprefix("sha256:")
        invocation_id = f"native_{identity}"
        run_id, workflow_id = f"run_native_{identity}", f"sage-native-{identity}"
        context = RuntimeContext.create(
            mode="direct",
            lifecycle="durable",
            principal=principal,
            run_id=run_id,
            thread_id=run_id,
            generation_id=snapshot.generation.generation_id,
            solution_id=snapshot.solution_id,
            request_id=workflow_id,
            solution_release_id=snapshot.solution_release.release_id
            if snapshot.solution_release
            else None,
        )
        digest = canonical_digest(self.approvals.subject(context, tool, payload.input, "direct"))
        try:
            reservation = self.invocations.reserve(invocation_id, digest)
        except (InvocationConflictError, InvocationInProgressError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if reservation.result_json:
            return InvocationResult.model_validate_json(reservation.result_json)
        task = self.approvals.prepare(context, tool, payload.input, "direct")
        if task is None:
            raise HTTPException(status_code=409, detail="Tool does not require durable approval")
        await self.runs.begin(
            context,
            input_data={
                "kind": "capability",
                "capability_id": tool.tool_id,
                "input": payload.input,
                "artifact_ids": payload.artifact_ids,
            },
        )
        await self.runs.append(
            run_id,
            "skill.orchestration.prepared",
            {
                "execution_type": "tool",
                "tool_id": tool.tool_id,
                "input": payload.input,
                "request_digest": digest,
            },
        )
        try:
            await self.backend.start(
                DurableRunRequest(
                    workflow_id=workflow_id,
                    workflow_type="sage-solution-workflow",
                    task_queue=self.task_queue,
                    execution_timeout_seconds=86400,
                    payload={"skill_run": {"run_id": run_id, "mode": "tool"}},
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Durable start outcome is uncertain"
            ) from exc
        response = InvocationResult(
            status="awaiting-approval",
            invocation_id=invocation_id,
            run_id=run_id,
            solution_id=context.solution_id,
            solution_release_id=context.solution_release_id,
            capability_id=tool.tool_id,
            output={},
            usage={},
            approval_task_id=task.task_id,
        )
        self.invocations.complete(invocation_id, response.model_dump_json())
        return response

    async def tool(self, run_id: str) -> dict[str, Any]:
        run, snapshot, _ = await self.resolve(run_id)
        if run.status == "completed":
            return next(
                event.data for event in run.events if event.name == "skill.orchestration.result"
            )
        if run.status != "running":
            raise HTTPException(status_code=409, detail="Tool run is terminal")
        prepared = next(
            event.data for event in run.events if event.name == "skill.orchestration.prepared"
        )
        if prepared["execution_type"] != "tool":
            raise HTTPException(status_code=409, detail="Not a direct Tool run")
        tool = snapshot.generation.tool_definitions[prepared["tool_id"]]
        task = self.approvals.prepare(run.context, tool, prepared["input"], "direct")
        if task and task.status == "pending":
            return {"status": "awaiting-approval", "tasks": [{"task_id": task.task_id}]}
        try:
            output = await self.approvals.dispatch(
                snapshot.tool_executor,
                run.context,
                tool,
                prepared["input"],
                "direct",
                task,
            )
        except InvocationInProgressError as exc:
            raise HTTPException(status_code=409, detail="Tool call is already running") from exc
        except Exception as exc:
            await self.runs.append(run_id, "run.failed", {"error": type(exc).__name__})
            raise HTTPException(
                status_code=409, detail="Tool not executed or outcome requires reconciliation"
            ) from exc
        return await self.finish(run_id, output)

    async def start(
        self,
        payload: DurableInvocationInput,
        snapshot: RuntimeSnapshot,
        principal: Principal,
    ) -> DurableInvocationResponse:
        definition = self.definition(snapshot, payload.skill) if payload.skill else None
        agent = snapshot.agent_catalog.get(payload.agent_id) if payload.agent_id else None
        execution = definition.manifest.spec.execution if definition else None
        execution_type = execution.type if execution else "model"
        if execution_type not in {"workflow", "model"}:
            raise HTTPException(status_code=422, detail="Skill is not an orchestration target")
        if self.backend is None:
            raise HTTPException(status_code=503, detail="Temporal and a SAGE worker are required")
        identity = canonical_digest(
            {
                "workspace": principal.workspace_id,
                "owner": principal.principal_id,
                "solution": payload.solution_id,
                "key": payload.idempotency_key,
            }
        ).removeprefix("sha256:")
        run_id, workflow_id = f"run_{identity}", f"sage-{identity}"
        context = RuntimeContext.create(
            mode="agent" if execution_type == "model" else "direct",
            lifecycle="durable",
            principal=principal,
            run_id=run_id,
            thread_id=run_id,
            skill_id=definition.name if definition else None,
            agent_id=agent.agent_id if agent else None,
            generation_id=snapshot.generation.generation_id,
            solution_id=snapshot.solution_id,
            solution_release_id=snapshot.solution_release.release_id
            if snapshot.solution_release
            else None,
            request_id=workflow_id,
        )
        if definition and not definition.manifest.spec.policy.execution_enabled:
            raise HTTPException(status_code=403, detail="Skill execution is disabled")
        if definition:
            decision = PolicyEnforcer().evaluate(context, skill_descriptor(definition), "execute")
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)
        capability_ids = execution.capability_ids if execution else agent.capability_ids
        view = snapshot.capabilities.resolve(context, capability_ids)
        if {item.capability_id for item in view.descriptors} != set(capability_ids):
            raise HTTPException(status_code=403, detail="Declared capabilities are unavailable")
        for dependency in view.skills:
            if dependency.manifest.spec.execution.type in {"workflow", "model"}:
                raise HTTPException(
                    status_code=422, detail="Nested orchestrated Skills are unsupported"
                )
        if (
            execution_type == "model"
            and (
                agent is not None
                or any(item.risk in {"write-high", "destructive"} for item in view.descriptors)
            )
            and not snapshot.agent_runtimes.has_persistent_checkpointer
        ):
            raise HTTPException(
                status_code=503,
                detail="Durable Agents and high-risk model Skills require Postgres checkpoints",
            )
        try:
            Draft202012Validator(
                definition.manifest.spec.input_schema if definition else {"type": "object"}
            ).validate(payload.payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="Skill input schema mismatch") from exc
        request_digest = canonical_digest(
            {
                "generation": context.generation_id,
                "release": context.solution_release_id,
                "request": payload.model_dump(),
            }
        )
        reservation_id = f"durable_{identity}"
        try:
            reservation = self.invocations.reserve(reservation_id, request_digest)
        except (InvocationConflictError, InvocationInProgressError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if reservation.result_json:
            return DurableInvocationResponse.model_validate_json(reservation.result_json)
        existing = await self.runs.get(run_id)
        if existing is not None:
            raise HTTPException(
                status_code=409, detail="Durable start outcome requires reconciliation"
            )
        workflow = (
            load_workflow_template(definition.package_root / (execution.workflow or ""))
            if execution_type == "workflow"
            else None
        )
        await self.runs.begin(
            context,
            input_data={
                "kind": "durable",
                "request": payload.model_dump(mode="json", exclude_none=True),
            },
        )
        await self.runs.append(
            run_id,
            "skill.orchestration.prepared",
            {
                "skill": definition.name if definition else None,
                "agent_id": agent.agent_id if agent else None,
                "execution_type": execution_type,
                "input": payload.payload,
                "request_digest": request_digest,
            },
        )
        try:
            handle = await self.backend.start(
                DurableRunRequest(
                    workflow_id=workflow_id,
                    workflow_type="sage-solution-workflow",
                    task_queue=self.task_queue,
                    execution_timeout_seconds=payload.execution_timeout_seconds or 86400,
                    payload={
                        "skill_run": {"run_id": run_id, "mode": execution_type},
                        "context": {"run_id": run_id, "solution_id": context.solution_id},
                        "input": payload.payload,
                        "workflow": workflow.model_dump(mode="json") if workflow else None,
                    },
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Durable start outcome is uncertain"
            ) from exc
        response = DurableInvocationResponse(
            run_id=run_id,
            workflow_id=workflow_id,
            temporal_run_id=handle.run_id,
            status=handle.status,
            solution_id=context.solution_id or "",
            solution_release_id=context.solution_release_id or "",
        )
        self.invocations.complete(reservation_id, response.model_dump_json())
        return response

    async def model(self, run_id: str) -> dict[str, Any]:
        run, snapshot, definition = await self.resolve(run_id)
        if run.status == "completed":
            return next(
                event.data for event in run.events if event.name == "skill.orchestration.result"
            )
        if run.status != "running":
            raise HTTPException(status_code=409, detail="Skill run is terminal")
        if definition and definition.manifest.spec.execution.type != "model":
            raise HTTPException(status_code=409, detail="Not a model Skill run")
        prepared = next(
            event for event in run.events if event.name == "skill.orchestration.prepared"
        )
        if prepared.data["execution_type"] != "model":
            raise HTTPException(status_code=409, detail="Not a model run")
        runtime = self._model_runtimes.get(run_id)
        if runtime is None:
            runtime = (
                snapshot.agent_runtimes.for_skill(definition, self.approvals)
                if definition
                else snapshot.agent_runtimes.get(
                    run.context.agent_id, approvals=self.approvals, agent_invoker=self.delegate,
                )
            )
            self._model_runtimes[run_id] = runtime
        previous = next(
            (event.data for event in reversed(run.events) if event.name == "skill.model.turn"), None
        )
        if previous and previous["status"] == "awaiting-approval":
            tasks = [self.approvals.tasks.get(item["task_id"]) for item in previous["tasks"]]
            if any(
                task is None
                or task.status not in {"pending", "approved"}
                or (task.expires_at and task.expires_at <= datetime.now(UTC))
                for task in tasks
            ):
                await self.runs.append(run_id, "run.failed", {"error": "approval_not_granted"})
                raise HTTPException(
                    status_code=409, detail="Approval rejected, expired or unavailable"
                )
            if any(task.status == "pending" for task in tasks):
                return previous
        elif previous and previous["status"] == "completed":
            return await self.finish_model(run_id, definition, previous["answer"])
        turn_number = sum(event.name == "skill.model.turn" for event in run.events)
        turn_id = f"model_{run_id}_{turn_number}"
        try:
            self.invocations.reserve(
                turn_id, canonical_digest({"run": run_id, "turn": turn_number})
            )
        except (InvocationConflictError, InvocationInProgressError) as exc:
            raise HTTPException(
                status_code=409, detail="Model turn is running or requires reconciliation"
            ) from exc
        message = json.dumps(
            {
                "input": prepared.data["input"],
                **({"output_schema": definition.manifest.spec.output_schema} if definition else {}),
            },
            ensure_ascii=False,
        )
        timeout_seconds = definition.manifest.spec.policy.timeout_seconds if definition else 300
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await runtime.durable_turn(
                    message,
                    run.context,
                    resume=previous is not None,
                    counters=previous.get("counters") if previous else None,
                )
        except (Exception, asyncio.CancelledError) as exc:
            self.invocations.fail(turn_id, "outcome_uncertain")
            latest = await self.runs.get(run_id)
            if latest and latest.status == "running":
                await self.runs.append(run_id, "run.failed", {"error": type(exc).__name__})
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(
                status_code=409, detail="Model turn failed; reconcile before retrying"
            ) from exc
        await self.runs.append(run_id, "skill.model.turn", result)
        self.invocations.complete(turn_id, json.dumps(result))
        if result["status"] == "awaiting-approval":
            return result
        return await self.finish_model(run_id, definition, result["answer"])

    async def finish_model(self, run_id: str, definition: Any, answer: str):
        try:
            output = json.loads(answer) if definition else {"answer": answer}
            return await self.finish(run_id, output)
        except (ValueError, ExecutionError) as exc:
            await self.runs.append(run_id, "run.failed", {"error": "invalid_model_output"})
            raise HTTPException(
                status_code=422, detail="Model output failed Skill validation"
            ) from exc

    async def delegate(self, task: AgentTask, caller: Principal) -> AgentTaskResult:
        snapshot, parent = await self.runtimes.resolve_child(
            task.parent_run_id, caller, target_agent_id=task.target_agent_id,
        )
        if parent.lifecycle != "durable" or task.artifact_ids:
            raise ExecutionError("Durable delegation requires a durable parent and JSON input")
        if not snapshot.agent_runtimes.has_persistent_checkpointer:
            raise HTTPException(status_code=503, detail="Durable delegation requires checkpoints")
        ancestor = parent
        visited: set[str] = set()
        while ancestor is not None:
            if ancestor.run_id in visited or ancestor.agent_id == task.target_agent_id:
                raise ExecutionError("Delegation cannot revisit an ancestor Agent")
            visited.add(ancestor.run_id)
            record = await self.runs.get(ancestor.run_id)
            if record is None or record.status != "running":
                raise ExecutionError("Delegation ancestor is no longer running")
            if not ancestor.parent_run_id:
                break
            record = await self.runs.get(ancestor.parent_run_id)
            if record is None:
                raise ExecutionError("Delegation ancestor is unavailable")
            ancestor = record.context
        identity = canonical_digest({"parent": parent.run_id, "key": task.idempotency_key})
        child_id = "run_delegate_" + identity.removeprefix("sha256:")[:32]
        request_digest = canonical_digest(task.model_dump(exclude={"turn_index"}))
        child = await self.runs.get(child_id)
        if child is None:
            reservation_id = "delegate_" + identity.removeprefix("sha256:")
            self.invocations.reserve(reservation_id, request_digest)
            context = RuntimeContext.create(
                run_id=child_id, thread_id=child_id, mode="agent", lifecycle="durable",
                principal=parent.principal, generation_id=parent.generation_id,
                solution_id=parent.solution_id, solution_release_id=parent.solution_release_id,
                agent_id=task.target_agent_id, parent_run_id=parent.run_id,
                root_run_id=parent.root_run_id, request_id=parent.request_id,
            )
            await self.runs.begin(
                context,
                input_data={
                    "kind": "agent-delegation",
                    "request": task.model_dump(mode="json"),
                },
            )
            await self.runs.append(child_id, "skill.orchestration.prepared", {
                "execution_type": "model", "input": {"goal": task.goal, "input": task.input},
                "delegation_digest": request_digest,
            })
            self.invocations.complete(reservation_id, json.dumps({"run_id": child_id}))
            await self.runs.append(parent.run_id, "agent.handoff.started", {
                "agent_id": task.target_agent_id, "child_run_id": child_id,
            })
            child = await self.runs.get(child_id)
        prepared = next((event for event in child.events
                         if event.name == "skill.orchestration.prepared"), None)
        if prepared is None or prepared.data.get("delegation_digest") != request_digest:
            raise ExecutionError("Delegation intent changed or requires reconciliation")
        if child.status not in {"running", "completed"}:
            raise ExecutionError("Delegated run is terminal")
        turns = [event.data for event in child.events if event.name == "skill.model.turn"]
        if task.turn_index > len(turns):
            raise ExecutionError("Delegation turn is out of sequence")
        if task.turn_index < len(turns):
            turn = turns[task.turn_index]
        else:
            turn = await self.model(child_id)
            child = await self.runs.get(child_id)
            turns = [event.data for event in child.events if event.name == "skill.model.turn"]
            if task.turn_index < len(turns):
                turn = turns[task.turn_index]
        pending = turn["status"] == "awaiting-approval"
        output = {}
        if not pending:
            completed = await self.finish(child_id, {"answer": turn.get("answer", "")})
            output = {"message": completed["output"]["answer"]}
            parent_run = await self.runs.get(parent.run_id)
            if not any(event.name == "agent.handoff.completed"
                       and event.data.get("child_run_id") == child_id
                       for event in parent_run.events):
                await self.runs.append(parent.run_id, "agent.handoff.completed", {
                    "agent_id": task.target_agent_id, "child_run_id": child_id,
                })
        return AgentTaskResult(
            invocation_id="ainv_" + identity.removeprefix("sha256:")[:32],
            run_id=child_id, parent_run_id=parent.run_id,
            solution_id=parent.solution_id, solution_release_id=parent.solution_release_id,
            agent_id=task.target_agent_id, output=output,
            usage={key: value for key, value in turn.get("counters", {}).items()
                   if isinstance(value, int)},
            status="awaiting-approval" if pending else "completed",
            tasks=turn.get("tasks", []),
        )

    async def resolve(self, run_id: str):
        run = await self.runs.get(run_id)
        if (
            run is None
            or run.context.lifecycle != "durable"
            or not any(event.name == "skill.orchestration.prepared" for event in run.events)
        ):
            raise HTTPException(status_code=404, detail="Orchestrated Skill Run not found")
        snapshot = self.runtimes.get(run.context.generation_id, run.context.solution_id)
        definition = (
            self.definition(snapshot, run.context.skill_id) if run.context.skill_id else None
        )
        return run, snapshot, definition

    async def finish(self, run_id: str, output: Any) -> dict[str, Any]:
        run, snapshot, definition = await self.resolve(run_id)
        if run.status == "completed":
            return next(
                event.data for event in run.events if event.name == "skill.orchestration.result"
            )
        if run.status != "running":
            raise HTTPException(status_code=409, detail="Skill run is terminal")
        spec = definition.manifest.spec if definition else None
        if not isinstance(output, dict):
            raise ExecutionError("Skill output must be a JSON object")
        if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > (
            spec.policy.max_output_bytes if spec else 256 * 1024
        ):
            raise ExecutionError("Skill output exceeds its size limit")
        try:
            Draft202012Validator(spec.output_schema if spec else {"type": "object"}).validate(
                output
            )
        except ValidationError as exc:
            raise ExecutionError("Skill output schema mismatch") from exc
        if definition:
            snapshot.executor.validate_result(definition, output)
        result = {"status": "completed", "output": output, "run_id": run_id}
        await self.runs.append(run_id, "skill.orchestration.result", result)
        capability_id = (
            f"agent:{run.context.agent_id}"
            if run.context.agent_id
            else f"skill:{definition.name}"
            if definition
            else None
        )
        try:
            view = build_result_view_from_output(
                run.context,
                run.input_data,
                output,
                capability_id=capability_id,
            )
            await self.runs.append(
                run_id,
                "result.available",
                result_available_data(view),
            )
        except ResultUnavailableError as exc:
            await self.runs.append(
                run_id,
                "result.projection.failed",
                {
                    "run_id": run_id,
                    "code": "result_projection_failed",
                    "message": str(exc),
                },
            )
        await self.runs.append(
            run_id,
            "run.completed",
            {
                "skill": definition.name if definition else None,
                "agent_id": run.context.agent_id,
            },
        )
        return result
