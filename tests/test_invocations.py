from pathlib import Path

import pytest

from sage.invocations import (
    InvocationConflictError,
    InvocationInProgressError,
    SqliteInvocationStore,
)


def test_invocation_store_replays_completed_result_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "invocations.sqlite3"
    store = SqliteInvocationStore(str(database))
    reserved = store.reserve("inv_1", "sha256:request")
    completed = store.complete("inv_1", '{"status":"completed"}')

    restored = SqliteInvocationStore(str(database)).reserve(
        "inv_1",
        "sha256:request",
    )

    assert reserved.status == "running"
    assert completed.status == "completed"
    assert restored.result_json == '{"status":"completed"}'


def test_invocation_store_rejects_running_and_changed_request(tmp_path: Path) -> None:
    store = SqliteInvocationStore(str(tmp_path / "invocations.sqlite3"))
    store.reserve("inv_1", "sha256:first")

    with pytest.raises(InvocationInProgressError):
        store.reserve("inv_1", "sha256:first")
    with pytest.raises(InvocationConflictError):
        store.reserve("inv_1", "sha256:second")