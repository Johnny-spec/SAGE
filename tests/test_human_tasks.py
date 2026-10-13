from pathlib import Path

import pytest

from sage.human_tasks import HumanTaskError, SqliteHumanTaskStore


def test_human_approval_is_role_scoped_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "human-tasks.sqlite3"
    store = SqliteHumanTaskStore(str(database))
    task = store.create(
        run_id="run_1",
        workspace_id="ws_team",
        solution_id="contract-review",
        kind="approval",
        title="Review high-risk finding",
        instructions="Review cited evidence before approval.",
        assigned_roles=("legal-reviewer",),
    )

    with pytest.raises(HumanTaskError, match="not assigned"):
        store.decide(
            task.task_id,
            actor_id="usr_reader",
            actor_roles=frozenset({"user"}),
            decision={},
            approve=True,
            reason="Not authorized",
        )
    reopened = SqliteHumanTaskStore(str(database))
    approved = reopened.decide(
        task.task_id,
        actor_id="usr_legal",
        actor_roles=frozenset({"user", "legal-reviewer"}),
        decision={"finding_id": "finding_1"},
        approve=True,
        reason="Evidence reviewed.",
    )

    assert approved.status == "approved"
    assert approved.decided_by == "usr_legal"
    with pytest.raises(HumanTaskError, match="already been decided"):
        reopened.decide(
            task.task_id,
            actor_id="usr_legal",
            actor_roles=frozenset({"legal-reviewer"}),
            decision={},
            approve=False,
            reason="Changed mind",
        )


def test_human_task_listing_is_solution_scoped(tmp_path: Path) -> None:
    store = SqliteHumanTaskStore(str(tmp_path / "human-tasks.sqlite3"))
    store.create(
        run_id="run_1",
        workspace_id="ws_team",
        solution_id="news-intelligence",
        kind="input",
        title="Choose sources",
        instructions="",
        assigned_roles=("news-editor",),
    )

    visible = store.list_for_principal(
        workspace_id="ws_team",
        solution_id="news-intelligence",
        roles=frozenset({"news-editor"}),
    )
    hidden = store.list_for_principal(
        workspace_id="ws_team",
        solution_id="contract-review",
        roles=frozenset({"news-editor"}),
    )

    assert len(visible) == 1
    assert hidden == ()