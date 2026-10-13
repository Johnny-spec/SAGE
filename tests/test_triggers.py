import asyncio

import pytest
from pydantic import ValidationError
from temporalio.client import ScheduleOverlapPolicy

from sage.triggers import TemporalTriggerProvider, TriggerDefinition, TriggerEvent


def test_schedule_and_event_trigger_contracts_are_distinct() -> None:
    scheduled = TriggerDefinition(
        trigger_id="daily-news",
        solution_id="news-intelligence",
        kind="schedule",
        workflow_type="build-daily-brief",
        cron="0 8 * * *",
    )
    event = TriggerDefinition(
        trigger_id="document-arrived",
        solution_id="contract-review",
        kind="event",
        workflow_type="review-contract",
        event_type="artifact.created",
    )

    assert scheduled.concurrency_policy == "forbid"
    assert event.event_type == "artifact.created"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "schedule", "event_type": "artifact.created"},
        {"kind": "event", "cron": "0 8 * * *"},
    ],
)
def test_trigger_rejects_mixed_or_missing_configuration(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TriggerDefinition(
            trigger_id="invalid",
            solution_id="solution",
            workflow_type="workflow",
            **payload,
        )


class FakeScheduleHandle:
    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeTemporalClient:
    def __init__(self) -> None:
        self.schedules: list[tuple[str, object]] = []
        self.started: list[tuple[object, object, dict[str, object]]] = []
        self.handle = FakeScheduleHandle()

    async def create_schedule(self, schedule_id: str, schedule: object) -> None:
        self.schedules.append((schedule_id, schedule))

    def get_schedule_handle(self, trigger_id: str) -> FakeScheduleHandle:
        assert trigger_id
        return self.handle

    async def start_workflow(
        self,
        workflow: object,
        payload: object,
        **options: object,
    ) -> object:
        self.started.append((workflow, payload, options))
        return object()


def test_temporal_trigger_provider_registers_cron_overlap_policy() -> None:
    client = FakeTemporalClient()
    provider = TemporalTriggerProvider(  # type: ignore[arg-type]
        client,
        task_queue="sage-workflows",
    )
    definition = TriggerDefinition(
        trigger_id="daily-news",
        solution_id="news-intelligence",
        kind="schedule",
        workflow_type="build-news-brief",
        cron="0 8 * * *",
        concurrency_policy="forbid",
    )

    asyncio.run(provider.register(definition, {"workflow": {"steps": []}}))

    schedule_id, schedule = client.schedules[0]
    assert schedule_id == "daily-news"
    assert schedule.spec.cron_expressions == ["0 8 * * *"]  # type: ignore[attr-defined]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP  # type: ignore[attr-defined]


def test_temporal_event_trigger_uses_event_id_for_workflow_id() -> None:
    client = FakeTemporalClient()
    provider = TemporalTriggerProvider(  # type: ignore[arg-type]
        client,
        task_queue="sage-workflows",
    )
    definition = TriggerDefinition(
        trigger_id="document-arrived",
        solution_id="contract-review",
        kind="event",
        workflow_type="review-contract",
        event_type="artifact.created",
    )
    event = TriggerEvent(
        event_id="evt_42",
        event_type="artifact.created",
        workspace_id="ws_team",
        solution_id="contract-review",
        payload={"artifact_id": "art_42"},
    )

    asyncio.run(provider.register(definition, {"workflow": {"steps": []}}))
    workflow_ids = asyncio.run(provider.dispatch(event))

    assert workflow_ids == ("sage-event-document-arrived-evt_42",)
    assert client.started[0][2]["id"] == workflow_ids[0]