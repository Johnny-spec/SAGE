"""Generic deterministic Temporal workflow for locked SAGE Workflow Templates."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

_MISSING = object()


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for raw_part in pointer.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _step_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "completed"
    metadata = result.get("_sage")
    if not isinstance(metadata, dict):
        return "completed"
    return str(metadata.get("status", "completed"))


def _condition_matches(
    condition: dict[str, Any],
    workflow_input: dict[str, Any],
    results: dict[str, Any],
) -> bool:
    source = condition.get("source")
    if source == "workflow-input":
        source_value: Any = workflow_input
    elif source == "step-output":
        source_value = results.get(str(condition.get("step_id")), _MISSING)
    else:
        raise ApplicationError("Unsupported Workflow condition source", non_retryable=True)
    actual = (
        _MISSING
        if source_value is _MISSING
        else _json_pointer(source_value, str(condition.get("path", "")))
    )
    operator = condition.get("operator", "equals")
    expected = condition.get("value")
    if operator == "exists":
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    if operator == "equals":
        return actual == expected
    if operator == "not-equals":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "not-in":
        return actual not in expected
    raise ApplicationError(
        f"Unsupported Workflow condition operator: {operator}",
        non_retryable=True,
    )


def _bound_step_input(
    step: dict[str, Any],
    workflow_input: dict[str, Any],
    results: dict[str, Any],
) -> dict[str, Any]:
    step_input = dict(workflow_input) if step.get("inherit_input", True) else {}
    declared_input = step.get("input", {})
    if isinstance(declared_input, dict):
        step_input.update(declared_input)
    bindings = step.get("bindings", {})
    if not isinstance(bindings, dict):
        raise ApplicationError("Workflow step bindings must be an object", non_retryable=True)
    for target, raw_binding in bindings.items():
        if not isinstance(raw_binding, dict):
            raise ApplicationError("Workflow binding must be an object", non_retryable=True)
        source = raw_binding.get("source")
        if source == "literal":
            value = raw_binding.get("value")
        elif source == "workflow-input":
            value = _json_pointer(workflow_input, str(raw_binding.get("path", "")))
        elif source == "step-output":
            step_id = str(raw_binding.get("step_id") or "")
            value = _json_pointer(results.get(step_id, _MISSING), str(raw_binding.get("path", "")))
        elif source == "artifact":
            value = raw_binding.get("artifact_id")
        else:
            raise ApplicationError(
                f"Unsupported Workflow binding source: {source}",
                non_retryable=True,
            )
        if value is _MISSING:
            raise ApplicationError(
                f"Workflow binding {target} could not be resolved",
                non_retryable=True,
            )
        step_input[str(target)] = value
    return step_input


@workflow.defn(name="sage-solution-workflow")
class SageSolutionWorkflow:
    def __init__(self) -> None:
        self._human_decisions: dict[str, dict[str, Any]] = {}

    @workflow.signal(name="human_task_decided")
    def human_task_decided(self, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            self._human_decisions[task_id] = payload

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill_run = payload.get("skill_run")
        if isinstance(skill_run, dict) and skill_run.get("mode") in {"model", "tool"}:
            turn = 0
            while True:
                result = await workflow.execute_activity(
                    "execute-sage-step",
                    {"skill_run": skill_run},
                    start_to_close_timeout=timedelta(seconds=360),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    activity_id=f"{skill_run['run_id']}:{turn}",
                    result_type=dict,
                )
                if result.get("status") != "awaiting-approval":
                    return result
                task_ids = [str(task["task_id"]) for task in result["tasks"]]
                await workflow.wait_condition(
                    lambda task_ids=task_ids: all(
                        task_id in self._human_decisions for task_id in task_ids
                    )
                )
                turn += 1
        test = payload.get("mcp_test")
        if isinstance(test, dict):
            task_id = str(test["task_id"])
            await workflow.wait_condition(lambda: task_id in self._human_decisions)
            return await workflow.execute_activity(
                "execute-sage-step",
                {"mcp_test": test},
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=RetryPolicy(maximum_attempts=1),
                activity_id=str(test["test_id"]),
                result_type=dict,
            )
        template = payload.get("workflow")
        context = payload.get("context")
        workflow_input = payload.get("input", {})
        if not isinstance(template, dict) or not isinstance(context, dict):
            raise ApplicationError(
                "Durable request has no locked Workflow Template",
                non_retryable=True,
            )
        raw_steps = template.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ApplicationError("Workflow Template has no steps", non_retryable=True)
        steps = {
            str(step["step_id"]): step
            for step in raw_steps
            if isinstance(step, dict) and isinstance(step.get("step_id"), str)
        }
        if len(steps) != len(raw_steps):
            raise ApplicationError("Workflow Template contains invalid steps", non_retryable=True)
        pending = set(steps)
        results: dict[str, Any] = {}
        max_concurrency = max(1, min(int(template.get("max_concurrency", 1)), 100))
        while pending:
            ready = sorted(
                step_id
                for step_id in pending
                if set(steps[step_id].get("depends_on", ())) <= set(results)
            )
            if not ready:
                raise ApplicationError(
                    "Workflow Template cannot make progress",
                    non_retryable=True,
                )
            for offset in range(0, len(ready), max_concurrency):
                batch = ready[offset : offset + max_concurrency]
                outcomes = await asyncio.gather(
                    *(
                        self._execute_step(
                            step_id,
                            steps[step_id],
                            context,
                            workflow_input if isinstance(workflow_input, dict) else {},
                            results,
                        )
                        for step_id in batch
                    ),
                    return_exceptions=True,
                )
                for step_id, outcome in zip(batch, outcomes, strict=True):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    results[step_id] = outcome
                    pending.remove(step_id)
        if isinstance(skill_run, dict):
            outputs = template.get("outputs") or {}
            output = (
                {
                    target: _bound_step_input(
                        {"bindings": {target: binding}},
                        workflow_input,
                        results,
                    )[target]
                    for target, binding in outputs.items()
                }
                if outputs
                else {"results": results}
            )
            return await workflow.execute_activity(
                "execute-sage-step",
                {"skill_run": skill_run, "output": output},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=1),
                activity_id=f"{context['run_id']}:result",
                result_type=dict,
            )
        return {
            "workflow_id": template.get("workflow_id"),
            "status": "completed",
            "results": results,
        }

    async def _execute_step(
        self,
        step_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        workflow_input: dict[str, Any],
        results: dict[str, Any],
    ) -> dict[str, Any]:
        dependencies = tuple(str(value) for value in step.get("depends_on", ()))
        if step.get("join_policy", "all") == "all" and any(
            _step_status(results[dependency]) != "completed" for dependency in dependencies
        ):
            return {
                "_sage": {
                    "status": "skipped",
                    "reason": "dependency_not_completed",
                }
            }
        condition = step.get("when")
        if isinstance(condition, dict) and not _condition_matches(
            condition,
            workflow_input,
            results,
        ):
            return {"_sage": {"status": "skipped", "reason": "condition_false"}}
        step_input = _bound_step_input(step, workflow_input, results)
        retry = step.get("retry", {})
        if not isinstance(retry, dict):
            retry = {}
        try:
            result = await workflow.execute_activity(
                "execute-sage-step",
                {
                    "context": context,
                    "step": step,
                    "input": step_input,
                    "prior_results": {
                        dependency: results[dependency] for dependency in dependencies
                    },
                },
                start_to_close_timeout=timedelta(seconds=int(step.get("timeout_seconds", 300))),
                retry_policy=RetryPolicy(
                    maximum_attempts=int(retry.get("maximum_attempts", 1)),
                    initial_interval=timedelta(
                        seconds=int(retry.get("initial_interval_seconds", 1))
                    ),
                    backoff_coefficient=float(retry.get("backoff_coefficient", 2)),
                    maximum_interval=timedelta(
                        seconds=int(retry.get("maximum_interval_seconds", 60))
                    ),
                ),
                activity_id=f"{context['run_id']}:{step_id}",
                result_type=dict,
            )
        except Exception as exc:
            if step.get("failure_policy", "fail-fast") != "continue-with-errors":
                raise
            return {
                "_sage": {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            }
        if step.get("kind") == "human-task":
            task_id = result.get("task_id") if isinstance(result, dict) else None
            if not isinstance(task_id, str):
                raise ApplicationError(
                    "HumanTask activity returned no task id",
                    non_retryable=True,
                )
            await workflow.wait_condition(lambda task_id=task_id: task_id in self._human_decisions)
            result = self._human_decisions[task_id]
        turn_index = 0
        while step.get("kind") in {"tool", "skill", "agent"} and (
            result.get("status") == "awaiting-approval"
        ):
            task_ids = ([str(item["task_id"]) for item in result["tasks"]]
                        if step.get("kind") == "agent" else [str(result["approval_task_id"])])
            if not task_ids:
                raise ApplicationError("Approval response has no tasks", non_retryable=True)
            await workflow.wait_condition(
                lambda task_ids=task_ids: all(
                    task_id in self._human_decisions for task_id in task_ids
                )
            )
            turn_index += 1
            result = await workflow.execute_activity(
                "execute-sage-step",
                {"context": context, "step": step, "input": step_input,
                 "turn_index": turn_index},
                start_to_close_timeout=timedelta(seconds=int(step.get("timeout_seconds", 300))),
                retry_policy=RetryPolicy(maximum_attempts=1),
                activity_id=f"{context['run_id']}:{step_id}:approved:{turn_index}",
                result_type=dict,
            )
        if not isinstance(result, dict):
            raise ApplicationError(
                f"Workflow step {step_id} returned a non-object result",
                non_retryable=True,
            )
        return result
