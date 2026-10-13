"""Persistent idempotency ledger for governed capability invocations."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict


class InvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    request_digest: str
    status: Literal["running", "completed", "failed"]
    result_json: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class InvocationConflictError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


class InvocationInProgressError(ValueError):
    """Raised when the same idempotent invocation is still executing."""


class SqliteInvocationStore:
    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def reserve(self, invocation_id: str, request_digest: str) -> InvocationRecord:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT invocation_id, request_digest, status, result_json,
                       error_code, created_at, updated_at
                FROM invocations WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            if row is not None:
                record = _record_from_row(row)
                if record.request_digest != request_digest:
                    raise InvocationConflictError(
                        "Idempotency key was already used for a different invocation"
                    )
                if record.status == "running":
                    raise InvocationInProgressError("Invocation is already running")
                if record.status == "failed":
                    if record.error_code == "outcome_uncertain":
                        raise InvocationConflictError(
                            "Previous external operation outcome is uncertain; "
                            "reconcile before retrying"
                        )
                    connection.execute(
                        """
                        UPDATE invocations
                        SET status = 'running', error_code = NULL, updated_at = ?
                        WHERE invocation_id = ?
                        """,
                        (now.isoformat(), invocation_id),
                    )
                    return record.model_copy(
                        update={"status": "running", "error_code": None, "updated_at": now}
                    )
                return record
            record = InvocationRecord(
                invocation_id=invocation_id,
                request_digest=request_digest,
                status="running",
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO invocations(
                    invocation_id, request_digest, status, result_json,
                    error_code, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    record.invocation_id,
                    record.request_digest,
                    record.status,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            return record

    def complete(self, invocation_id: str, result_json: str) -> InvocationRecord:
        return self._finish(invocation_id, "completed", result_json=result_json)

    def fail(self, invocation_id: str, error_code: str) -> InvocationRecord:
        return self._finish(invocation_id, "failed", error_code=error_code)

    def get(self, invocation_id: str) -> InvocationRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT invocation_id, request_digest, status, result_json,
                       error_code, created_at, updated_at
                FROM invocations WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _finish(
        self,
        invocation_id: str,
        status: Literal["completed", "failed"],
        *,
        result_json: str | None = None,
        error_code: str | None = None,
    ) -> InvocationRecord:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE invocations
                SET status = ?, result_json = ?, error_code = ?, updated_at = ?
                WHERE invocation_id = ? AND status = 'running'
                """,
                (status, result_json, error_code, now.isoformat(), invocation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Invocation is not running: {invocation_id}")
        record = self.get(invocation_id)
        if record is None:
            raise ValueError(f"Invocation not found: {invocation_id}")
        return record

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
                    result_json TEXT,
                    error_code TEXT,
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


def _record_from_row(row: tuple[object, ...]) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=str(row[0]),
        request_digest=str(row[1]),
        status=str(row[2]),
        result_json=str(row[3]) if row[3] is not None else None,
        error_code=str(row[4]) if row[4] is not None else None,
        created_at=datetime.fromisoformat(str(row[5])),
        updated_at=datetime.fromisoformat(str(row[6])),
    )
