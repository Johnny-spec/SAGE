"""Persistent human input and approval tasks for durable workflows."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from sage.digests import canonical_digest


class HumanTaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    run_id: str
    workspace_id: str
    solution_id: str
    step_id: str | None = None
    kind: Literal["input", "approval"]
    status: Literal["pending", "approved", "rejected", "submitted", "expired", "cancelled"]
    title: str
    instructions: str = ""
    assigned_roles: tuple[str, ...]
    subject: dict[str, Any] = Field(default_factory=dict)
    payload_schema: dict[str, Any] = Field(default_factory=dict)
    request_digest: str
    decision: dict[str, Any] | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    decided_at: datetime | None = None


class HumanTaskError(ValueError):
    """Raised when a HumanTask transition is invalid."""


class SqliteHumanTaskStore:
    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def create(
        self,
        *,
        run_id: str,
        workspace_id: str,
        solution_id: str,
        kind: Literal["input", "approval"],
        title: str,
        instructions: str,
        assigned_roles: tuple[str, ...],
        subject: dict[str, Any] | None = None,
        payload_schema: dict[str, Any] | None = None,
        step_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> HumanTaskRecord:
        roles = tuple(dict.fromkeys(role.strip() for role in assigned_roles if role.strip()))
        if not roles:
            raise HumanTaskError("HumanTask requires at least one assigned role")
        now = datetime.now(UTC)
        request_payload = {
            "run_id": run_id,
            "workspace_id": workspace_id,
            "solution_id": solution_id,
            "step_id": step_id,
            "kind": kind,
            "title": title.strip(),
            "instructions": instructions.strip(),
            "assigned_roles": roles,
            "subject": subject or {},
            "payload_schema": payload_schema or {},
            "expires_at": expires_at,
        }
        record = HumanTaskRecord(
            task_id=f"ht_{uuid4().hex}",
            **request_payload,
            status="pending",
            request_digest=canonical_digest(request_payload),
            created_at=now,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO human_tasks(task_id, record_json) VALUES (?, ?)",
                (record.task_id, record.model_dump_json()),
            )
        return record

    def decide(
        self,
        task_id: str,
        *,
        actor_id: str,
        actor_roles: frozenset[str],
        decision: dict[str, Any],
        approve: bool | None,
        reason: str,
    ) -> HumanTaskRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM human_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            record = HumanTaskRecord.model_validate_json(row[0])
            now = datetime.now(UTC)
            if record.status != "pending":
                raise HumanTaskError("HumanTask has already been decided")
            if record.expires_at is not None and record.expires_at <= now:
                expired = record.model_copy(update={"status": "expired"})
                connection.execute(
                    "UPDATE human_tasks SET record_json = ? WHERE task_id = ?",
                    (expired.model_dump_json(), task_id),
                )
                raise HumanTaskError("HumanTask has expired")
            if not actor_roles.intersection(record.assigned_roles):
                raise HumanTaskError("Principal is not assigned to this HumanTask")
            if record.kind == "approval" and approve is None:
                raise HumanTaskError("Approval HumanTask requires approve or reject")
            if record.kind == "input" and approve is not None:
                raise HumanTaskError("Input HumanTask does not accept an approval decision")
            status = (
                "submitted"
                if record.kind == "input"
                else "approved"
                if approve
                else "rejected"
            )
            updated = record.model_copy(
                update={
                    "status": status,
                    "decision": decision,
                    "decided_by": actor_id,
                    "decision_reason": reason.strip(),
                    "decided_at": now,
                }
            )
            connection.execute(
                "UPDATE human_tasks SET record_json = ? WHERE task_id = ?",
                (updated.model_dump_json(), task_id),
            )
        return updated

    def get(self, task_id: str) -> HumanTaskRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM human_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return HumanTaskRecord.model_validate_json(row[0]) if row else None

    def list_for_principal(
        self,
        *,
        workspace_id: str,
        solution_id: str,
        roles: frozenset[str],
        status: str | None = "pending",
    ) -> tuple[HumanTaskRecord, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT record_json FROM human_tasks").fetchall()
        records = (
            HumanTaskRecord.model_validate_json(row[0])
            for row in rows
        )
        return tuple(
            sorted(
                (
                    record
                    for record in records
                    if record.workspace_id == workspace_id
                    and record.solution_id == solution_id
                    and roles.intersection(record.assigned_roles)
                    and (status is None or record.status == status)
                ),
                key=lambda item: item.created_at,
            )
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS human_tasks (
                    task_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL
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