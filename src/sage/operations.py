"""Process-bound asynchronous handles for interactive Skill and Agent runs."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sage.api_models import MessageRequest
from sage.digests import canonical_digest
from sage.discovery import agent_is_discoverable
from sage.domain import Principal, RuntimeContext
from sage.errors import PolicyDeniedError, TargetNotFoundError
from sage.invocations import InvocationConflictError, SqliteInvocationStore
from sage.runs import RunSnapshot, RunStore
from sage.runtime_manager import RuntimeManager
from sage.service import MessageService


class OperationError(ValueError):
    """Raised when an interactive operation cannot be created or controlled."""


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str | None = Field(default=None, max_length=64)
    capability_id: str = Field(min_length=1, max_length=2000)
    input: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(default=None, max_length=128)


class OperationLinks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    events: str
    result: str
    cancel: str
    feedback: str


class OperationHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    operation_id: str
    run_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    lifecycle: Literal["interactive"] = "interactive"
    capability_id: str
    generation_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    runtime_attached: bool
    restart_recoverable: Literal[False] = False
    links: OperationLinks


@dataclass(slots=True)
class _OperationEntry:
    task: asyncio.Task[None]
    started: asyncio.Event


class InteractiveOperationService:
    """Runs interactive work after returning an independently consumable Run handle."""

    def __init__(
        self,
        runtimes: RuntimeManager,
        runs: RunStore,
        invocations: SqliteInvocationStore,
    ) -> None:
        self._runtimes = runtimes
        self._runs = runs
        self._invocations = invocations
        self._entries: dict[str, _OperationEntry] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        payload: OperationRequest,
        principal: Principal,
        *,
        idempotency_key: str,
    ) -> tuple[OperationHandle, bool]:
        message_request = self._message_request(payload)
        snapshot, resolved = self._runtimes.resolve(message_request, principal)
        self._require_discoverable(payload.capability_id, snapshot, resolved, principal)
        request_data = payload.model_dump(mode="json", exclude_none=True)
        request_digest = canonical_digest(
            {
                "workspace_id": principal.workspace_id,
                "principal_id": principal.principal_id,
                "generation_id": snapshot.generation.generation_id,
                "solution_release_id": (
                    snapshot.solution_release.release_id if snapshot.solution_release else None
                ),
                "request": request_data,
            }
        )
        identity = canonical_digest(
            {
                "workspace_id": principal.workspace_id,
                "principal_id": principal.principal_id,
                "solution_id": snapshot.solution_id,
                "idempotency_key": idempotency_key,
            }
        ).removeprefix("sha256:")[:32]
        operation_id = f"op_{identity}"
        run_id = f"run_op_{identity}"

        async with self._lock:
            existing = self._invocations.get(operation_id)
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise InvocationConflictError(
                        "Idempotency key was already used for a different operation"
                    )
                entry = self._entries.get(run_id)
                created = False
            else:
                self._invocations.reserve(operation_id, request_digest)
                started = asyncio.Event()
                task = asyncio.create_task(
                    self._execute(
                        operation_id=operation_id,
                        run_id=run_id,
                        capability_id=payload.capability_id,
                        request_data=request_data,
                        message_service=snapshot.message_service,
                        message_request=resolved,
                        principal=principal,
                        started=started,
                    )
                )
                entry = _OperationEntry(task=task, started=started)
                self._entries[run_id] = entry
                created = True

        if entry is not None and not entry.started.is_set() and not entry.task.done():
            started_wait = asyncio.create_task(entry.started.wait())
            done, _ = await asyncio.wait(
                (entry.task, started_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if started_wait not in done:
                started_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await started_wait

        run = await self._runs.get(run_id)
        if run is None:
            raise OperationError("Interactive operation failed before its Run was recorded")
        return self._handle(run), created

    async def get(self, run_id: str) -> OperationHandle:
        run = await self._runs.get(run_id)
        if run is None or not self._is_operation(run):
            raise KeyError(run_id)
        return self._handle(run)

    async def cancel(self, run_id: str) -> OperationHandle:
        run = await self._runs.get(run_id)
        if run is None or not self._is_operation(run):
            raise KeyError(run_id)
        if run.status != "running":
            return self._handle(run)
        async with self._lock:
            entry = self._entries.get(run_id)
        if entry is None or entry.task.done():
            raise OperationError(
                "Interactive operation is no longer attached to this process; "
                "use durable execution when restart recovery is required"
            )
        entry.task.cancel()
        with suppress(asyncio.CancelledError):
            await entry.task
        current = await self._runs.get(run_id)
        if current is None:
            raise KeyError(run_id)
        return self._handle(current)

    async def close(self) -> None:
        async with self._lock:
            entries = tuple(self._entries.values())
        for entry in entries:
            if not entry.task.done():
                entry.task.cancel()
        if entries:
            await asyncio.gather(*(entry.task for entry in entries), return_exceptions=True)

    async def _execute(
        self,
        *,
        operation_id: str,
        run_id: str,
        capability_id: str,
        request_data: dict[str, Any],
        message_service: MessageService,
        message_request: MessageRequest,
        principal: Principal,
        started: asyncio.Event,
    ) -> None:
        try:
            await message_service.run(
                message_request,
                principal=principal,
                run_id=run_id,
                input_data={
                    "kind": "interactive-operation",
                    "operation_id": operation_id,
                    "request": request_data,
                },
                started_event=started,
            )
        except asyncio.CancelledError:
            run = await self._runs.get(run_id)
            if run is not None and run.status == "running":
                await self._runs.append(
                    run_id,
                    "run.cancelled",
                    {"run_id": run_id, "code": "operation_cancelled"},
                )
        except Exception as exc:
            run = await self._runs.get(run_id)
            if run is not None and run.status == "running":
                await self._runs.append(
                    run_id,
                    "run.failed",
                    {
                        "run_id": run_id,
                        "code": "operation_failed",
                        "error_type": type(exc).__name__,
                    },
                )
        finally:
            run = await self._runs.get(run_id)
            reservation = self._invocations.get(operation_id)
            if run is not None and reservation is not None and reservation.status == "running":
                self._invocations.complete(
                    operation_id,
                    json.dumps(
                        {
                            "run_id": run_id,
                            "status": run.status,
                            "capability_id": capability_id,
                        },
                        separators=(",", ":"),
                    ),
                )
            elif run is None and reservation is not None and reservation.status == "running":
                self._invocations.fail(operation_id, "operation_start_failed")
            async with self._lock:
                self._entries.pop(run_id, None)

    def _handle(self, run: RunSnapshot) -> OperationHandle:
        if not self._is_operation(run):
            raise KeyError(run.context.run_id)
        assert run.input_data is not None
        operation_id = str(run.input_data["operation_id"])
        request_data = run.input_data.get("request")
        if not isinstance(request_data, dict):
            raise OperationError("Interactive operation request metadata is invalid")
        capability_id = request_data.get("capability_id")
        if not isinstance(capability_id, str):
            raise OperationError("Interactive operation capability metadata is invalid")
        entry = self._entries.get(run.context.run_id)
        attached = entry is not None and not entry.task.done()
        base = f"/v1/operations/{run.context.run_id}"
        return OperationHandle(
            operation_id=operation_id,
            run_id=run.context.run_id,
            status=run.status,
            capability_id=capability_id,
            generation_id=run.context.generation_id,
            solution_id=run.context.solution_id,
            solution_release_id=run.context.solution_release_id,
            runtime_attached=attached,
            links=OperationLinks(
                status=base,
                events=f"/v1/runs/{run.context.run_id}/events/stream",
                result=f"/v1/runs/{run.context.run_id}/result",
                cancel=f"{base}/cancel",
                feedback=f"/v1/runs/{run.context.run_id}/feedback",
            ),
        )

    @staticmethod
    def _message_request(payload: OperationRequest) -> MessageRequest:
        if payload.capability_id.startswith("skill:"):
            skill_name = payload.capability_id.removeprefix("skill:")
            if not skill_name:
                raise OperationError("Skill capability ID is invalid")
            return MessageRequest(
                solution_id=payload.solution_id,
                mode="direct",
                skill=skill_name,
                message=f"Invoke {payload.capability_id}",
                arguments=payload.input,
                artifact_ids=payload.artifact_ids,
                thread_id=payload.thread_id,
            )
        if payload.capability_id.startswith("agent:"):
            agent_id = payload.capability_id.removeprefix("agent:")
            unknown = set(payload.input) - {"message"}
            message = payload.input.get("message")
            if not agent_id or unknown or not isinstance(message, str) or not message.strip():
                raise OperationError(
                    "Agent operation input must contain only a non-empty message"
                )
            return MessageRequest(
                solution_id=payload.solution_id,
                mode="agent",
                agent_id=agent_id,
                message=message,
                artifact_ids=payload.artifact_ids,
                thread_id=payload.thread_id,
            )
        if payload.capability_id.startswith("tool:mcp:"):
            raise OperationError(
                "Native MCP tools use /v1/invocations; high-risk tools use durable approval"
            )
        raise OperationError("Only Skill and Agent operations are supported")

    @staticmethod
    def _require_discoverable(
        capability_id: str,
        snapshot: Any,
        request: MessageRequest,
        principal: Principal,
    ) -> None:
        context = RuntimeContext.create(
            mode=request.mode,
            principal=principal,
            generation_id=snapshot.generation.generation_id,
            solution_id=snapshot.solution_id,
            solution_release_id=(
                snapshot.solution_release.release_id if snapshot.solution_release else None
            ),
        )
        capabilities = snapshot.capabilities.resolve(context)
        if capability_id.startswith("skill:"):
            definition = capabilities.get_skill(capability_id.removeprefix("skill:"))
            if definition is None:
                raise TargetNotFoundError("Capability is not available to this principal")
            if definition.manifest.spec.policy.risk != "read-only":
                raise PolicyDeniedError(
                    "Write Skills do not support process-bound asynchronous cancellation; "
                    "use the governed invocation path"
                )
            return
        definition = snapshot.agent_catalog.get(capability_id.removeprefix("agent:"))
        if not agent_is_discoverable(definition, capabilities):
            raise TargetNotFoundError("Agent is not available to this principal")
        by_id = {
            descriptor.capability_id: descriptor for descriptor in capabilities.descriptors
        }
        if any(
            by_id[capability_id].risk != "read-only"
            for capability_id in definition.capability_ids
            if capability_id in by_id
        ):
            raise PolicyDeniedError(
                "Agents with write capabilities require the governed durable invocation path"
            )

    @staticmethod
    def _is_operation(run: RunSnapshot) -> bool:
        return bool(run.input_data and run.input_data.get("kind") == "interactive-operation")