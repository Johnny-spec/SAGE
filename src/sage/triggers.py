"""Scheduled and event-driven trigger contracts for Solution workflows."""

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy


class TriggerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_id: str = Field(min_length=1, max_length=200)
    solution_id: str = Field(min_length=1, max_length=64)
    kind: Literal["schedule", "event"]
    workflow_type: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    cron: str | None = Field(default=None, max_length=200)
    event_type: str | None = Field(default=None, max_length=200)
    concurrency_policy: Literal["allow", "forbid", "replace"] = "forbid"
    payload_template: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind_configuration(self) -> "TriggerDefinition":
        if self.kind == "schedule" and not self.cron:
            raise ValueError("Scheduled Trigger requires cron")
        if self.kind == "schedule" and self.event_type is not None:
            raise ValueError("Scheduled Trigger cannot declare event_type")
        if self.kind == "event" and not self.event_type:
            raise ValueError("Event Trigger requires event_type")
        if self.kind == "event" and self.cron is not None:
            raise ValueError("Event Trigger cannot declare cron")
        return self


class TriggerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1)
    solution_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TriggerProvider(Protocol):
    async def register(
        self,
        definition: TriggerDefinition,
        workflow_payload: dict[str, Any],
    ) -> None: ...

    async def unregister(self, trigger_id: str) -> None: ...

    async def dispatch(self, event: TriggerEvent) -> tuple[str, ...]: ...


class TemporalTriggerProvider:
    def __init__(
        self,
        client: Client,
        *,
        task_queue: str,
        engine_workflow_type: str = "sage-solution-workflow",
    ) -> None:
        self._client = client
        self._task_queue = task_queue
        self._engine_workflow_type = engine_workflow_type
        self._event_triggers: dict[str, tuple[TriggerDefinition, dict[str, Any]]] = {}

    async def register(
        self,
        definition: TriggerDefinition,
        workflow_payload: dict[str, Any],
    ) -> None:
        payload = _trigger_payload(definition, workflow_payload)
        if definition.kind == "event":
            self._event_triggers[definition.trigger_id] = (definition, payload)
            return
        overlap = {
            "allow": ScheduleOverlapPolicy.ALLOW_ALL,
            "forbid": ScheduleOverlapPolicy.SKIP,
            "replace": ScheduleOverlapPolicy.CANCEL_OTHER,
        }[definition.concurrency_policy]
        await self._client.create_schedule(
            definition.trigger_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    self._engine_workflow_type,
                    payload,
                    id=f"sage-trigger-{definition.trigger_id}",
                    task_queue=self._task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=[definition.cron or ""]),
                policy=SchedulePolicy(overlap=overlap),
            ),
        )

    async def unregister(self, trigger_id: str) -> None:
        if self._event_triggers.pop(trigger_id, None) is not None:
            return
        await self._client.get_schedule_handle(trigger_id).delete()

    async def dispatch(self, event: TriggerEvent) -> tuple[str, ...]:
        workflow_ids: list[str] = []
        for definition, template_payload in self._event_triggers.values():
            if (
                not definition.enabled
                or definition.event_type != event.event_type
                or definition.solution_id != event.solution_id
            ):
                continue
            workflow_id = f"sage-event-{definition.trigger_id}-{event.event_id}"
            payload = dict(template_payload)
            payload["input"] = {
                **dict(template_payload.get("input") or {}),
                **event.payload,
                "trigger_event_id": event.event_id,
                "triggered_at": event.occurred_at.isoformat(),
            }
            await self._client.start_workflow(
                self._engine_workflow_type,
                payload,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            workflow_ids.append(workflow_id)
        return tuple(workflow_ids)


def _trigger_payload(
    definition: TriggerDefinition,
    workflow_payload: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(workflow_payload)
    payload["input"] = {
        **dict(workflow_payload.get("input") or {}),
        **definition.payload_template,
        "trigger_id": definition.trigger_id,
    }
    return payload