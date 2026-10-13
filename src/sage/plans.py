"""Persistent, queryable Plan projections for Agent and Workflow runs."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sage.digests import canonical_digest
from sage.domain import RuntimeContext

PlanStatus = Literal["running", "completed", "failed", "cancelled", "paused"]
PlanStepStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting",
    "completed",
    "failed",
    "cancelled",
    "skipped",
]


class PlanStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    title: str
    kind: str = "agent-task"
    status: PlanStepStatus = "pending"
    depends_on: tuple[str, ...] = ()
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()


class PlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    run_id: str
    workspace_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    generation_id: str
    version: int = Field(ge=1)
    status: PlanStatus
    steps: tuple[PlanStepRecord, ...]
    created_at: datetime
    updated_at: datetime


class PlanStore(Protocol):
    def project_todos(
        self,
        context: RuntimeContext,
        todos: list[dict[str, str]],
    ) -> PlanRecord: ...

    def get_for_run(self, run_id: str) -> PlanRecord | None: ...


class SqlitePlanStore:
    """Durable Plan snapshot store with optimistic version increments."""

    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def project_todos(
        self,
        context: RuntimeContext,
        todos: list[dict[str, str]],
    ) -> PlanRecord:
        now = datetime.now(UTC)
        steps = tuple(_step_from_todo(todo, index) for index, todo in enumerate(todos))
        all_completed = steps and all(step.status == "completed" for step in steps)
        status: PlanStatus = "completed" if all_completed else "running"
        plan_id = f"plan_{context.run_id.removeprefix('run_')}"
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT version, created_at FROM plans WHERE run_id = ?",
                (context.run_id,),
            ).fetchone()
            version = int(existing[0]) + 1 if existing else 1
            created_at = datetime.fromisoformat(existing[1]) if existing else now
            record = PlanRecord(
                plan_id=plan_id,
                run_id=context.run_id,
                workspace_id=context.principal.workspace_id,
                solution_id=context.solution_id,
                solution_release_id=context.solution_release_id,
                generation_id=context.generation_id,
                version=version,
                status=status,
                steps=steps,
                created_at=created_at,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO plans(run_id, plan_id, version, record_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    version = excluded.version,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.run_id,
                    record.plan_id,
                    record.version,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_for_run(self, run_id: str) -> PlanRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM plans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return PlanRecord.model_validate_json(row[0]) if row else None

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    run_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _step_from_todo(todo: dict[str, str], index: int) -> PlanStepRecord:
    title = str(todo.get("content") or todo.get("title") or f"Step {index + 1}")
    raw_status = str(todo.get("status") or "pending")
    statuses: dict[str, PlanStepStatus] = {
        "pending": "pending",
        "in_progress": "running",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    step_id = str(todo.get("id") or _stable_step_id(index, title))
    raw_dependencies = todo.get("depends_on", ())
    depends_on = (
        tuple(str(item) for item in raw_dependencies)
        if isinstance(raw_dependencies, (list, tuple))
        else ()
    )
    return PlanStepRecord(
        step_id=step_id,
        title=title,
        status=statuses.get(raw_status, "pending"),
        depends_on=depends_on,
    )


def _stable_step_id(index: int, title: str) -> str:
    digest = canonical_digest({"index": index, "title": title})
    return f"step_{digest.removeprefix('sha256:')[:16]}"
