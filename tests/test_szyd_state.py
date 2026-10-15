from dataclasses import replace

import pytest

from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError
from sage.szyd_state import SzydCaseStore


def context():
    return RuntimeContext.create(
        mode="agent",
        principal=Principal.anonymous(),
        thread_id="thread",
        solution_id="szyd-compliance",
        agent_id="szyd-agent",
    )


def test_store_reopen_replay_and_optimistic_conflicts(tmp_path):
    path = tmp_path / "cases.sqlite3"
    store = SzydCaseStore(path)
    scope = context()
    result = store.save(
        scope,
        {"state": {"run_id": "case"}},
        expected_revision=0,
        key="turn",
        digest="digest",
        waiting_run_id=scope.run_id,
    )
    reopened = SzydCaseStore(path)
    assert reopened.load(scope).response == result
    assert reopened.replay(scope, "turn", "digest") == result
    with pytest.raises(ExecutionError, match="idempotency conflict"):
        reopened.replay(scope, "turn", "changed")
    with pytest.raises(ExecutionError, match="revision conflict"):
        reopened.save(
            scope, result, expected_revision=0, key="other", digest="new", waiting_run_id=None
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_id", "other"),
        ("solution_id", "other"),
        ("generation_id", "other"),
        ("solution_release_id", "other"),
        ("agent_id", "other"),
        ("principal", Principal(workspace_id="ws_default", principal_id="other")),
        ("principal", Principal(workspace_id="other", principal_id="anonymous")),
    ],
)
def test_case_scope_cannot_be_crossed(tmp_path, field, value):
    store = SzydCaseStore(tmp_path / "cases.sqlite3")
    scope = context()
    store.save(
        scope,
        {"state": {"run_id": "case"}},
        expected_revision=0,
        key="turn",
        digest="digest",
        waiting_run_id=None,
    )
    other = replace(scope, **{field: value})
    assert store.load(other) is None
    with pytest.raises(ExecutionError, match="not found"):
        store.load(other, "case")
