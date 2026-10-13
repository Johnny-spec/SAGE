"""Run lifecycle storage port and the development in-memory implementation."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from sage.domain import Principal, RuntimeContext

RunStatus = Literal["running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class RunEvent:
    sequence: int
    name: str
    data: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    context: RuntimeContext
    status: RunStatus
    input_data: dict[str, object] | None
    events: tuple[RunEvent, ...]


class RunStore(Protocol):
    async def begin(
        self,
        context: RuntimeContext,
        input_data: Mapping[str, object] | None = None,
    ) -> RunEvent: ...

    async def append(
        self,
        run_id: str,
        name: str,
        data: Mapping[str, object],
    ) -> RunEvent: ...

    async def get(self, run_id: str) -> RunSnapshot | None: ...

    async def wait_for_events(
        self,
        run_id: str,
        *,
        after_sequence: int,
        timeout_seconds: float,
    ) -> RunSnapshot | None: ...


@dataclass(slots=True)
class _MutableRun:
    context: RuntimeContext
    status: RunStatus
    input_data: dict[str, object] | None
    events: list[RunEvent]


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, _MutableRun] = {}
        self._lock = asyncio.Lock()
        self._changes: dict[str, asyncio.Event] = {}

    async def begin(
        self,
        context: RuntimeContext,
        input_data: Mapping[str, object] | None = None,
    ) -> RunEvent:
        data: dict[str, object] = {
            "run_id": context.run_id,
            "thread_id": context.thread_id,
            "mode": context.mode,
            "lifecycle": context.lifecycle,
            "generation_id": context.generation_id,
        }
        if context.solution_id is not None:
            data["solution_id"] = context.solution_id
        if context.solution_release_id is not None:
            data["solution_release_id"] = context.solution_release_id
        if context.agent_id is not None:
            data["agent_id"] = context.agent_id
        if context.root_run_id is not None:
            data["root_run_id"] = context.root_run_id
        if context.parent_run_id is not None:
            data["parent_run_id"] = context.parent_run_id
        event = RunEvent(1, "run.started", data, datetime.now(UTC))
        async with self._lock:
            if context.run_id in self._runs:
                raise ValueError(f"run already exists: {context.run_id}")
            self._runs[context.run_id] = _MutableRun(
                context,
                "running",
                deepcopy(dict(input_data)) if input_data is not None else None,
                [event],
            )
        self._changes.setdefault(context.run_id, asyncio.Event()).set()
        return event

    async def append(
        self,
        run_id: str,
        name: str,
        data: Mapping[str, object],
    ) -> RunEvent:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ValueError(f"run not found: {run_id}")
            if run.status != "running":
                raise ValueError(f"run is already terminal: {run_id}")
            event = RunEvent(
                sequence=len(run.events) + 1,
                name=name,
                data=deepcopy(dict(data)),
                created_at=datetime.now(UTC),
            )
            run.events.append(event)
            if name == "run.completed":
                run.status = "completed"
            elif name == "run.failed":
                run.status = "failed"
            elif name == "run.cancelled":
                run.status = "cancelled"
        self._changes.setdefault(run_id, asyncio.Event()).set()
        return event

    async def get(self, run_id: str) -> RunSnapshot | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return RunSnapshot(
                context=run.context,
                status=run.status,
                input_data=deepcopy(run.input_data),
                events=tuple(deepcopy(run.events)),
            )

    async def wait_for_events(
        self,
        run_id: str,
        *,
        after_sequence: int,
        timeout_seconds: float,
    ) -> RunSnapshot | None:
        snapshot = await self.get(run_id)
        if _has_new_or_terminal(snapshot, after_sequence):
            return snapshot
        changed = self._changes.setdefault(run_id, asyncio.Event())
        changed.clear()
        snapshot = await self.get(run_id)
        if _has_new_or_terminal(snapshot, after_sequence):
            return snapshot
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout_seconds)
        return await self.get(run_id)


class SqliteRunStore:
    """Durable single-node RunStore used by the self-hosted runtime."""

    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = asyncio.Lock()
        self._changes: dict[str, asyncio.Event] = {}
        self._initialize()

    async def begin(
        self,
        context: RuntimeContext,
        input_data: Mapping[str, object] | None = None,
    ) -> RunEvent:
        event = RunEvent(
            sequence=1,
            name="run.started",
            data=_started_data(context),
            created_at=datetime.now(UTC),
        )
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        INSERT INTO runs(run_id, context_json, status, input_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            context.run_id,
                            _context_json(context),
                            "running",
                            _input_json(input_data),
                        ),
                    )
                    _insert_event(connection, context.run_id, event)
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"run already exists: {context.run_id}") from exc
            self._changes.setdefault(context.run_id, asyncio.Event()).set()
        return event

    async def append(
        self,
        run_id: str,
        name: str,
        data: Mapping[str, object],
    ) -> RunEvent:
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"run not found: {run_id}")
                if row[0] != "running":
                    raise ValueError(f"run is already terminal: {run_id}")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                event = RunEvent(
                    sequence=int(sequence),
                    name=name,
                    data=deepcopy(dict(data)),
                    created_at=datetime.now(UTC),
                )
                _insert_event(connection, run_id, event)
                if name == "run.completed":
                    connection.execute(
                        "UPDATE runs SET status = 'completed' WHERE run_id = ?",
                        (run_id,),
                    )
                elif name == "run.failed":
                    connection.execute(
                        "UPDATE runs SET status = 'failed' WHERE run_id = ?",
                        (run_id,),
                    )
                elif name == "run.cancelled":
                    connection.execute(
                        "UPDATE runs SET status = 'cancelled' WHERE run_id = ?",
                        (run_id,),
                    )
        self._changes.setdefault(run_id, asyncio.Event()).set()
        return event

    async def get(self, run_id: str) -> RunSnapshot | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT context_json, status, input_json FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                events = tuple(
                    RunEvent(
                        sequence=int(event_row[0]),
                        name=str(event_row[1]),
                        data=json.loads(event_row[2]),
                        created_at=datetime.fromisoformat(event_row[3]),
                    )
                    for event_row in connection.execute(
                        """
                        SELECT sequence, name, data_json, created_at
                        FROM run_events
                        WHERE run_id = ?
                        ORDER BY sequence
                        """,
                        (run_id,),
                    )
                )
                return RunSnapshot(
                    context=_context_from_json(row[0]),
                    status=cast(RunStatus, row[1]),
                    input_data=json.loads(row[2]) if row[2] is not None else None,
                    events=events,
                )

    async def wait_for_events(
        self,
        run_id: str,
        *,
        after_sequence: int,
        timeout_seconds: float,
    ) -> RunSnapshot | None:
        snapshot = await self.get(run_id)
        if _has_new_or_terminal(snapshot, after_sequence):
            return snapshot
        changed = self._changes.setdefault(run_id, asyncio.Event())
        changed.clear()
        snapshot = await self.get(run_id)
        if _has_new_or_terminal(snapshot, after_sequence):
            return snapshot
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout_seconds)
        return await self.get(run_id)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone()
            if schema is not None and "cancelled" not in str(schema[0]):
                runs = connection.execute(
                    "SELECT run_id, context_json, status FROM runs"
                ).fetchall()
                events = connection.execute(
                    """
                    SELECT run_id, sequence, name, data_json, created_at
                    FROM run_events ORDER BY run_id, sequence
                    """
                ).fetchall()
                connection.execute("DROP TABLE run_events")
                connection.execute("DROP TABLE runs")
                self._create_schema(connection)
                connection.executemany(
                    """
                    INSERT INTO runs(run_id, context_json, status, input_json)
                    VALUES (?, ?, ?, NULL)
                    """,
                    runs,
                )
                connection.executemany(
                    """
                    INSERT INTO run_events(run_id, sequence, name, data_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    events,
                )
            else:
                self._create_schema(connection)
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
                }
                if "input_json" not in columns:
                    connection.execute("ALTER TABLE runs ADD COLUMN input_json TEXT")

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                context_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('running', 'completed', 'failed', 'cancelled')
                ),
                input_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _started_data(context: RuntimeContext) -> dict[str, object]:
    data: dict[str, object] = {
        "run_id": context.run_id,
        "thread_id": context.thread_id,
        "mode": context.mode,
        "lifecycle": context.lifecycle,
        "generation_id": context.generation_id,
    }
    if context.solution_id is not None:
        data["solution_id"] = context.solution_id
    if context.solution_release_id is not None:
        data["solution_release_id"] = context.solution_release_id
    if context.agent_id is not None:
        data["agent_id"] = context.agent_id
    if context.root_run_id is not None:
        data["root_run_id"] = context.root_run_id
    if context.parent_run_id is not None:
        data["parent_run_id"] = context.parent_run_id
    return data


def _context_json(context: RuntimeContext) -> str:
    return json.dumps(
        {
            "run_id": context.run_id,
            "thread_id": context.thread_id,
            "principal": {
                "workspace_id": context.principal.workspace_id,
                "principal_id": context.principal.principal_id,
                "roles": sorted(context.principal.roles),
                "authentication_method": context.principal.authentication_method,
            },
            "mode": context.mode,
            "lifecycle": context.lifecycle,
            "generation_id": context.generation_id,
            "solution_id": context.solution_id,
            "solution_release_id": context.solution_release_id,
            "agent_id": context.agent_id,
            "skill_id": context.skill_id,
            "root_run_id": context.root_run_id,
            "parent_run_id": context.parent_run_id,
            "approved_capability_ids": sorted(context.approved_capability_ids),
            "request_id": context.request_id,
            "created_at": context.created_at.isoformat(),
        },
        separators=(",", ":"),
    )


def _input_json(input_data: Mapping[str, object] | None) -> str | None:
    if input_data is None:
        return None
    return json.dumps(
        dict(input_data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _context_from_json(value: str) -> RuntimeContext:
    data = json.loads(value)
    principal_data = data["principal"]
    return RuntimeContext(
        run_id=data["run_id"],
        thread_id=data["thread_id"],
        principal=Principal(
            workspace_id=principal_data["workspace_id"],
            principal_id=principal_data["principal_id"],
            roles=frozenset(principal_data["roles"]),
            authentication_method=principal_data["authentication_method"],
        ),
        mode=data["mode"],
        lifecycle=data.get("lifecycle", "interactive"),
        generation_id=data["generation_id"],
        solution_id=data.get("solution_id"),
        solution_release_id=data.get("solution_release_id"),
        agent_id=data.get("agent_id"),
        skill_id=data.get("skill_id"),
        root_run_id=data.get("root_run_id"),
        parent_run_id=data.get("parent_run_id"),
        approved_capability_ids=frozenset(data.get("approved_capability_ids", ())),
        request_id=data.get("request_id"),
        created_at=datetime.fromisoformat(data["created_at"]),
    )


def _insert_event(
    connection: sqlite3.Connection,
    run_id: str,
    event: RunEvent,
) -> None:
    connection.execute(
        """
        INSERT INTO run_events(run_id, sequence, name, data_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            run_id,
            event.sequence,
            event.name,
            json.dumps(event.data, separators=(",", ":"), default=str),
            event.created_at.isoformat(),
        ),
    )


def _has_new_or_terminal(snapshot: RunSnapshot | None, after_sequence: int) -> bool:
    return snapshot is None or snapshot.status != "running" or (
        bool(snapshot.events) and snapshot.events[-1].sequence > after_sequence
    )
