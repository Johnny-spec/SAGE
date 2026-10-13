"""Persistent append-only audit records without request payload disclosure."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    actor_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    outcome: Literal["success", "denied", "failed"]
    status_code: int
    details_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    occurred_at: datetime


class SqliteAuditStore:
    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def append(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        action: str,
        resource: str,
        outcome: Literal["success", "denied", "failed"],
        status_code: int,
        details_digest: str,
    ) -> AuditRecord:
        record = AuditRecord(
            audit_id=f"audit_{uuid4().hex}",
            actor_id=actor_id,
            workspace_id=workspace_id,
            action=action,
            resource=resource,
            outcome=outcome,
            status_code=status_code,
            details_digest=details_digest,
            occurred_at=datetime.now(UTC),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_records(audit_id, record_json, occurred_at) VALUES (?, ?, ?)",
                (record.audit_id, record.model_dump_json(), record.occurred_at.isoformat()),
            )
        return record

    def list(self, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Audit limit must be between 1 and 1000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM audit_records
                ORDER BY occurred_at DESC, audit_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(AuditRecord.model_validate_json(row[0]) for row in rows)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_records (
                    audit_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
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