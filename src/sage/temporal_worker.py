"""Temporal worker for generic SAGE Solution Workflow Templates."""

import asyncio
import os
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from sage.temporal_workflow import SageSolutionWorkflow


class SageStepActivities:
    def __init__(
        self,
        *,
        api_base_url: str,
        admin_token: str | None,
        bearer_token: str | None,
        token_scope: str | None,
        client: httpx.AsyncClient | None = None,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=api_base_url,
            timeout=330,
        )
        self._owns_client = client is None
        self._admin_token = admin_token
        self._bearer_token = bearer_token
        self._token_scope = token_scope
        self._credential = credential or (
            DefaultAzureCredential() if token_scope and not bearer_token else None
        )
        self._owns_credential = credential is None and self._credential is not None

    @activity.defn(name="execute-sage-step")
    async def execute_step(self, request: dict[str, Any]) -> dict[str, Any]:
        skill_run = request.get("skill_run")
        if isinstance(skill_run, dict):
            mode = skill_run.get("mode")
            action = mode if mode in {"model", "tool"} else "result"
            return await self._post(
                f"/v1/admin/skill-runs/{skill_run['run_id']}/{action}",
                {"output": request.get("output")},
                await self._admin_headers(),
            )
        test = request.get("mcp_test")
        if isinstance(test, dict):
            return await self._post(
                f"/v1/admin/mcp/servers/{test['server_id']}/tool-tests/{test['test_id']}/dispatch",
                {},
                await self._admin_headers(),
            )
        context = request.get("context")
        step = request.get("step")
        step_input = request.get("input", {})
        if not isinstance(context, dict) or not isinstance(step, dict):
            raise ApplicationError("Invalid SAGE step request", non_retryable=True)
        solution_id = str(context.get("solution_id") or "")
        step_id = str(step.get("step_id") or "")
        kind = step.get("kind")
        target = str(step.get("target") or "")
        prior_results = request.get("prior_results", {})
        if not step_id or not target or not context.get("run_id"):
            raise ApplicationError("Incomplete SAGE step request", non_retryable=True)
        if kind in {"skill", "tool"}:
            approval_task_id = (
                next(
                    (
                        value.get("task_id")
                        for value in prior_results.values()
                        if isinstance(value, dict)
                        and value.get("kind") == "approval"
                        and value.get("status") == "approved"
                        and isinstance(value.get("task_id"), str)
                    ),
                    None,
                )
                if isinstance(prior_results, dict)
                else None
            )
            result = await self._post(
                "/v1/invocations",
                {
                    "solution_id": solution_id or None,
                    "capability_id": target if kind == "tool" else f"skill:{target}",
                    "input": step_input,
                    "idempotency_key": f"{context['run_id']}:{step_id}",
                    "parent_run_id": context["run_id"],
                    "approval_task_id": approval_task_id,
                },
                await self._public_headers(),
            )
            return self._validate_output(step, result)
        if kind == "agent":
            values = dict(step_input) if isinstance(step_input, dict) else {}
            goal = str(values.pop("message", f"Execute workflow step {step_id}"))
            artifact_ids = values.pop("artifact_ids", [])
            if not isinstance(artifact_ids, list):
                raise ApplicationError(
                    "Agent step artifact_ids must be an array",
                    non_retryable=True,
                )
            result = await self._post(
                "/v1/agent-invocations",
                {
                    "parent_run_id": context["run_id"],
                    "agent_id": target,
                    "goal": goal,
                    "input": values,
                    "artifact_ids": artifact_ids,
                    "idempotency_key": f"{context['run_id']}:{step_id}",
                    "turn_index": request.get("turn_index", 0),
                },
                await self._public_headers(),
            )
            return self._validate_output(step, result)
        if kind == "human-task":
            values = dict(step_input) if isinstance(step_input, dict) else {}
            roles = values.pop("assigned_roles", ["user"])
            result = await self._post(
                "/v1/admin/human-tasks",
                {
                    "run_id": context["run_id"],
                    "step_id": step_id,
                    "kind": values.pop("task_kind", "approval"),
                    "title": values.pop("title", target),
                    "instructions": values.pop("instructions", ""),
                    "assigned_roles": roles,
                    "subject": values.pop("subject", {}),
                    "payload_schema": values.pop("payload_schema", {}),
                },
                await self._admin_headers(),
            )
            return self._validate_output(step, result)
        raise ApplicationError(
            f"Unsupported Workflow step kind: {kind}",
            non_retryable=True,
        )

    @staticmethod
    def _validate_output(
        step: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if result.get("status") == "awaiting-approval":
            return result
        schema = step.get("output_schema", {})
        if not isinstance(schema, dict) or not schema:
            return result
        output = result.get("output", result)
        try:
            Draft202012Validator(schema).validate(output)
        except ValidationError as exc:
            path = "/" + "/".join(str(value) for value in exc.absolute_path)
            raise ApplicationError(
                f"Workflow step output failed schema validation at {path}: {exc.message}",
                non_retryable=True,
            ) from exc
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_credential and self._credential is not None:
            await self._credential.close()

    async def _public_headers(self) -> dict[str, str]:
        token = self._bearer_token
        if token is None and self._credential is not None and self._token_scope is not None:
            token = (await self._credential.get_token(self._token_scope)).token
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _admin_headers(self) -> dict[str, str]:
        if self._admin_token:
            return {"Authorization": f"Bearer {self._admin_token}"}
        return await self._public_headers()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await self._client.post(path, json=payload, headers=headers)
        if response.is_error:
            non_retryable = 400 <= response.status_code < 500 and response.status_code != 429
            raise ApplicationError(
                f"SAGE API returned HTTP {response.status_code}: {response.text[:500]}",
                non_retryable=non_retryable,
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ApplicationError("SAGE API returned a non-object result", non_retryable=True)
        return value


async def run_worker() -> None:
    target = os.getenv("SAGE_TEMPORAL_TARGET")
    if not target:
        raise ValueError("SAGE_TEMPORAL_TARGET is required")
    namespace = os.getenv("SAGE_TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("SAGE_TEMPORAL_TASK_QUEUE", "sage-workflows")
    client = await Client.connect(
        target,
        namespace=namespace,
        api_key=os.getenv("SAGE_TEMPORAL_API_KEY") or None,
        tls=os.getenv("SAGE_TEMPORAL_TLS", "false").casefold() in {"1", "true", "yes"},
    )
    activities = SageStepActivities(
        api_base_url=os.getenv("SAGE_API_BASE_URL", "http://127.0.0.1:8000"),
        admin_token=os.getenv("SAGE_ADMIN_TOKEN") or None,
        bearer_token=os.getenv("SAGE_WORKER_BEARER_TOKEN") or None,
        token_scope=os.getenv("SAGE_WORKER_TOKEN_SCOPE") or None,
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SageSolutionWorkflow],
        activities=[activities.execute_step],
    )
    try:
        await worker.run()
    finally:
        await activities.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
