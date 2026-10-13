import asyncio
from unittest.mock import AsyncMock, patch

from sage.temporal_worker import SageStepActivities
from sage.temporal_workflow import SageSolutionWorkflow


def test_agent_step_waits_for_each_approval_turn():
    workflow = SageSolutionWorkflow()
    activity = AsyncMock(side_effect=[
        {"status": "awaiting-approval", "tasks": [{"task_id": "first"}]},
        {"status": "awaiting-approval", "tasks": [{"task_id": "second"}]},
        {"status": "completed", "output": {"message": "done"}},
    ])

    async def decide(predicate):
        for task_id in ("first", "second"):
            workflow.human_task_decided({"task_id": task_id, "status": "approved"})
        assert predicate()

    wait = AsyncMock(side_effect=decide)
    with patch("sage.temporal_workflow.workflow.execute_activity", activity), patch(
        "sage.temporal_workflow.workflow.wait_condition", wait,
    ):
        result = asyncio.run(workflow._execute_step(
            "write", {"kind": "agent", "target": "writer"}, {"run_id": "root"}, {}, {},
        ))
    assert result["status"] == "completed"
    assert wait.await_count == 2
    assert [call.args[1].get("turn_index", 0) for call in activity.await_args_list] == [0, 1, 2]
    assert len({call.kwargs["activity_id"] for call in activity.await_args_list}) == 3


def test_worker_passes_agent_turn_index():
    worker = SageStepActivities(
        api_base_url="http://localhost", admin_token=None, bearer_token=None,
        token_scope=None, client=AsyncMock(),
    )
    worker._post = AsyncMock(return_value={
        "status": "awaiting-approval", "tasks": [{"task_id": "first"}],
    })
    result = asyncio.run(worker.execute_step({
        "context": {"run_id": "root"}, "step": {
            "step_id": "write", "kind": "agent", "target": "writer",
        }, "input": {}, "turn_index": 1,
    }))
    assert result["status"] == "awaiting-approval"
    assert worker._post.await_args.args[1]["turn_index"] == 1