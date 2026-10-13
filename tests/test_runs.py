import asyncio
import sqlite3

import pytest

from sage.domain import Principal, RuntimeContext
from sage.runs import InMemoryRunStore, SqliteRunStore


def test_run_store_records_ordered_terminal_lifecycle() -> None:
    async def scenario() -> object:
        store = InMemoryRunStore()
        context = RuntimeContext.create(
            mode="direct",
            principal=Principal.anonymous(),
            run_id="run_test",
            thread_id="thr_test",
        )
        started = await store.begin(context)
        selected = await store.append(
            context.run_id,
            "route.selected",
            {"skill": "echo"},
        )
        completed = await store.append(
            context.run_id,
            "run.completed",
            {"response": {"status": "completed"}},
        )
        snapshot = await store.get(context.run_id)
        return started, selected, completed, snapshot

    started, selected, completed, snapshot = asyncio.run(scenario())

    assert (started.sequence, selected.sequence, completed.sequence) == (1, 2, 3)
    assert snapshot is not None
    assert snapshot.status == "completed"
    assert [event.name for event in snapshot.events] == [
        "run.started",
        "route.selected",
        "run.completed",
    ]


def test_run_store_rejects_events_after_terminal_state() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())
        await store.begin(context)
        await store.append(context.run_id, "run.failed", {"code": "test"})
        with pytest.raises(ValueError, match="already terminal"):
            await store.append(context.run_id, "tool.started", {"tool": "echo"})

    asyncio.run(scenario())


def test_sqlite_run_store_survives_restart_and_preserves_sequence(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    context = RuntimeContext.create(
        mode="agent",
        principal=Principal.anonymous(),
        solution_id="incident-diagnostics",
        solution_release_id="incident-diagnostics@1.0.0",
    )

    async def write() -> None:
        store = SqliteRunStore(str(database))
        await store.begin(
            context,
            input_data={"message": "diagnose", "artifact_ids": ["art_1"]},
        )
        await store.append(context.run_id, "plan.updated", {"steps": 2})
        await store.append(context.run_id, "run.completed", {"ok": True})

    asyncio.run(write())
    reopened = SqliteRunStore(str(database))
    snapshot = asyncio.run(reopened.get(context.run_id))

    assert snapshot is not None
    assert snapshot.status == "completed"
    assert snapshot.context.solution_id == "incident-diagnostics"
    assert snapshot.input_data == {"artifact_ids": ["art_1"], "message": "diagnose"}
    assert [event.sequence for event in snapshot.events] == [1, 2, 3]
    assert [event.name for event in snapshot.events] == [
        "run.started",
        "plan.updated",
        "run.completed",
    ]
    with pytest.raises(ValueError, match="already terminal"):
        asyncio.run(reopened.append(context.run_id, "tool.started", {}))


def test_sqlite_run_store_migrates_legacy_status_constraint(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            context_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed'))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE run_events (
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        )
        """
    )
    connection.commit()
    connection.close()
    store = SqliteRunStore(str(database))
    context = RuntimeContext.create(mode="agent", principal=Principal.anonymous())

    asyncio.run(store.begin(context))
    asyncio.run(store.append(context.run_id, "run.cancelled", {}))
    snapshot = asyncio.run(store.get(context.run_id))

    assert snapshot is not None
    assert snapshot.status == "cancelled"
    assert snapshot.input_data is None
    assert snapshot.events[0].data["lifecycle"] == "interactive"


def test_sqlite_run_store_adds_input_column_to_existing_current_schema(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            context_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('running', 'completed', 'failed', 'cancelled')
            )
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE run_events (
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        )
        """
    )
    connection.commit()
    connection.close()

    store = SqliteRunStore(str(database))
    context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())
    asyncio.run(store.begin(context, input_data={"message": "persist me"}))
    snapshot = asyncio.run(store.get(context.run_id))

    assert snapshot is not None
    assert snapshot.input_data == {"message": "persist me"}


@pytest.mark.parametrize("store_type", [InMemoryRunStore, SqliteRunStore])
def test_run_store_waits_for_new_event_without_polling(tmp_path, store_type) -> None:
    async def scenario() -> object:
        store = (
            store_type()
            if store_type is InMemoryRunStore
            else store_type(str(tmp_path / "wait-runs.sqlite3"))
        )
        context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())
        await store.begin(context)
        waiting = asyncio.create_task(
            store.wait_for_events(
                context.run_id,
                after_sequence=1,
                timeout_seconds=1,
            )
        )
        await asyncio.sleep(0)
        await store.append(context.run_id, "tool.started", {"tool": "echo"})
        return await waiting

    snapshot = asyncio.run(scenario())

    assert snapshot is not None
    assert snapshot.events[-1].sequence == 2
    assert snapshot.events[-1].name == "tool.started"
