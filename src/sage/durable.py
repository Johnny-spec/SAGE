"""Durable execution port and the official Temporal client adapter."""

from datetime import timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy


class DurableRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=255)
    workflow_type: str = Field(min_length=1, max_length=255)
    task_queue: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=2_592_000)


class DurableRunHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    run_id: str | None = None
    status: str


class DurableExecutionPort(Protocol):
    async def start(self, request: DurableRunRequest) -> DurableRunHandle: ...

    async def describe(self, workflow_id: str) -> DurableRunHandle: ...

    async def result(self, workflow_id: str) -> Any: ...

    async def cancel(self, workflow_id: str, *, reason: str) -> None: ...

    async def signal(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None: ...


class TemporalExecutionAdapter:
    def __init__(self, client: Client, *, engine_workflow_type: str | None = None) -> None:
        self._client = client
        self._engine_workflow_type = engine_workflow_type

    @classmethod
    async def connect(
        cls,
        target_host: str,
        *,
        namespace: str = "default",
        api_key: str | None = None,
        tls: bool = False,
        engine_workflow_type: str = "sage-solution-workflow",
    ) -> "TemporalExecutionAdapter":
        client = await Client.connect(
            target_host,
            namespace=namespace,
            api_key=api_key,
            tls=tls,
        )
        return cls(client, engine_workflow_type=engine_workflow_type)

    async def start(self, request: DurableRunRequest) -> DurableRunHandle:
        handle = await self._client.start_workflow(
            self._engine_workflow_type or request.workflow_type,
            request.payload,
            id=request.workflow_id,
            task_queue=request.task_queue,
            execution_timeout=(
                timedelta(seconds=request.execution_timeout_seconds)
                if request.execution_timeout_seconds is not None
                else None
            ),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return DurableRunHandle(
            workflow_id=handle.id,
            run_id=handle.first_execution_run_id,
            status="running",
        )

    async def describe(self, workflow_id: str) -> DurableRunHandle:
        handle = self._client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        return DurableRunHandle(
            workflow_id=workflow_id,
            run_id=description.run_id,
            status=description.status.name.casefold().replace("_", "-"),
        )

    async def result(self, workflow_id: str) -> Any:
        return await self._client.get_workflow_handle(workflow_id).result()

    async def cancel(self, workflow_id: str, *, reason: str) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.cancel(reason=reason)

    async def signal(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(signal_name, payload)