from pathlib import Path

from sage.domain import Principal, RuntimeContext
from sage.plans import SqlitePlanStore


def test_plan_projection_has_stable_steps_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "plans.sqlite3"
    context = RuntimeContext.create(
        mode="agent",
        principal=Principal.anonymous(),
        solution_id="incident-diagnostics",
        solution_release_id="incident-diagnostics@1.0.0",
    )
    store = SqlitePlanStore(str(database))

    first = store.project_todos(
        context,
        [
            {"content": "Collect evidence", "status": "in_progress"},
            {"content": "Create report", "status": "pending"},
        ],
    )
    second = store.project_todos(
        context,
        [
            {"content": "Collect evidence", "status": "completed"},
            {"content": "Create report", "status": "completed"},
        ],
    )
    restored = SqlitePlanStore(str(database)).get_for_run(context.run_id)

    assert first.version == 1
    assert second.version == 2
    assert second.status == "completed"
    assert [step.step_id for step in first.steps] == [step.step_id for step in second.steps]
    assert restored == second