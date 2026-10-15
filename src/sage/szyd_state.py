"""Scoped, optimistic case persistence for the SZYD compliance workflow."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sage.digests import canonical_digest
from sage.domain import RuntimeContext
from sage.errors import ExecutionError


@dataclass(frozen=True)
class SzydCase:
    response: dict[str, Any]
    waiting_run_id: str | None


def case_scope(context: RuntimeContext) -> str:
    return canonical_digest(
        [
            context.principal.workspace_id,
            context.principal.principal_id,
            context.solution_id,
            context.solution_release_id,
            context.generation_id,
            context.agent_id,
            context.thread_id,
        ]
    )


class SzydCaseStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    scope TEXT PRIMARY KEY, case_id TEXT UNIQUE NOT NULL,
                    revision INTEGER NOT NULL, response TEXT NOT NULL,
                    waiting_run_id TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    scope TEXT NOT NULL, request_key TEXT NOT NULL, digest TEXT NOT NULL,
                    revision INTEGER NOT NULL, response TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scope, request_key), UNIQUE (scope, revision)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self, context: RuntimeContext, case_id: str | None = None) -> SzydCase | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE scope = ?", (case_scope(context),)
            ).fetchone()
        if case_id is not None and (row is None or row["case_id"] != case_id):
            raise ExecutionError("SZYD case not found in this scope")
        return SzydCase(json.loads(row["response"]), row["waiting_run_id"]) if row else None

    def replay(self, context: RuntimeContext, key: str, digest: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT digest, response FROM transitions WHERE scope = ? AND request_key = ?",
                (case_scope(context), key),
            ).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise ExecutionError("SZYD idempotency conflict: request changed")
        return json.loads(row["response"])

    def save(
        self,
        context: RuntimeContext,
        response: dict,
        *,
        expected_revision: int,
        key: str,
        digest: str,
        waiting_run_id: str | None,
    ) -> dict:
        scope = case_scope(context)
        result = {
            **response,
            "case_id": response["state"]["run_id"],
            "revision": expected_revision + 1,
        }
        encoded = json.dumps(result, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT digest, response FROM transitions WHERE scope = ? AND request_key = ?",
                (scope, key),
            ).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise ExecutionError("SZYD idempotency conflict: request changed")
                return json.loads(receipt["response"])
            current = connection.execute(
                "SELECT revision FROM cases WHERE scope = ?", (scope,)
            ).fetchone()
            if (current["revision"] if current else 0) != expected_revision:
                raise ExecutionError("SZYD revision conflict; inspect before continuing")
            connection.execute(
                """INSERT INTO cases (scope, case_id, revision, response, waiting_run_id)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET
                   revision=excluded.revision, response=excluded.response,
                   waiting_run_id=excluded.waiting_run_id, updated_at=CURRENT_TIMESTAMP""",
                (scope, result["case_id"], result["revision"], encoded, waiting_run_id),
            )
            connection.execute(
                "INSERT INTO transitions (scope, request_key, digest, revision, response) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, key, digest, result["revision"], encoded),
            )
        return result
