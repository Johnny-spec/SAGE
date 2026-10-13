from datetime import UTC, datetime, timedelta
from pathlib import Path

from sage.memory import SqliteMemoryStore


def test_memory_is_solution_scoped_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    store = SqliteMemoryStore(str(database))
    saved = store.put(
        workspace_id="ws_team",
        solution_id="contract-review",
        scope="user",
        scope_id="usr_alice",
        key="output-style",
        value="Use a concise risk table",
        created_by="usr_alice",
    )

    reopened = SqliteMemoryStore(str(database))
    own = reopened.list(
        workspace_id="ws_team",
        solution_id="contract-review",
        scope="user",
        scope_id="usr_alice",
    )
    other_solution = reopened.list(
        workspace_id="ws_team",
        solution_id="news-intelligence",
        scope="user",
        scope_id="usr_alice",
    )

    assert own == (saved,)
    assert other_solution == ()


def test_memory_upsert_expiry_and_scoped_delete(tmp_path: Path) -> None:
    store = SqliteMemoryStore(str(tmp_path / "memory.sqlite3"))
    original = store.put(
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        scope="user",
        scope_id="usr_alice",
        key="language",
        value="English",
        created_by="usr_alice",
    )
    updated = store.put(
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        scope="user",
        scope_id="usr_alice",
        key="language",
        value="Chinese",
        created_by="usr_alice",
    )
    store.put(
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        scope="user",
        scope_id="usr_alice",
        key="expired",
        value="ignore",
        created_by="usr_alice",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    visible = store.list(
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        scope="user",
        scope_id="usr_alice",
    )
    wrong_scope_deleted = store.delete(
        updated.memory_id,
        workspace_id="ws_team",
        solution_id="contract-review",
        scope_id="usr_alice",
    )
    deleted = store.delete(
        updated.memory_id,
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        scope_id="usr_alice",
    )

    assert original.memory_id == updated.memory_id
    assert [(record.key, record.value) for record in visible] == [("language", "Chinese")]
    assert wrong_scope_deleted is False
    assert deleted is True
