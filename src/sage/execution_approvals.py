"""Exact-call approval and dispatch ledger shared by durable native Tool callers."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from sage.capabilities import skill_descriptor
from sage.digests import canonical_digest, directory_digest
from sage.domain import RuntimeContext
from sage.errors import ExecutionError, PolicyDeniedError
from sage.executor import SkillExecutor
from sage.human_tasks import HumanTaskRecord, SqliteHumanTaskStore
from sage.invocations import SqliteInvocationStore
from sage.models import SkillDefinition
from sage.policy import PolicyEnforcer
from sage.runs import RunStore
from sage.sensitive_data import redact
from sage.tools import McpToolDefinition, ToolExecutor


class ExecutionApprovals:
    def __init__(
        self, tasks: SqliteHumanTaskStore, invocations: SqliteInvocationStore,
        runs: RunStore | None = None,
    ) -> None:
        self.tasks = tasks
        self.invocations = invocations
        self._prepare_lock = RLock()
        self.runs = runs

    @staticmethod
    def subject(
        context: RuntimeContext,
        tool: McpToolDefinition | SkillDefinition,
        arguments: dict[str, Any],
        call_id: str,
    ) -> dict[str, Any]:
        schema = (tool.input_schema if isinstance(tool, McpToolDefinition)
                  else tool.manifest.spec.input_schema)
        visible_arguments = redact(arguments, schema=schema)
        if len(json.dumps(visible_arguments, ensure_ascii=False).encode("utf-8")) > 16384:
            visible_arguments = {"omitted": "Arguments exceed approval display limit"}
        return {
            "scope": "published-tool" if isinstance(tool, McpToolDefinition) else "published-skill",
            "run_id": context.run_id,
            "workspace_id": context.principal.workspace_id,
            "principal_id": context.principal.principal_id,
            "generation_id": context.generation_id,
            "solution_id": context.solution_id,
            "solution_release_id": context.solution_release_id,
                "capability_id": ExecutionApprovals.descriptor(tool).capability_id,
                **({"tool_digest": tool.content_digest} if isinstance(tool, McpToolDefinition)
                    else {"skill_digest": directory_digest(tool.package_root)}),
            "input_digest": canonical_digest(arguments),
            "arguments": visible_arguments,
            "call_id": call_id,
            **({"root_run_id": context.root_run_id} if context.parent_run_id else {}),
        }

    @staticmethod
    def descriptor(tool: McpToolDefinition | SkillDefinition):
        return tool.descriptor if isinstance(tool, McpToolDefinition) else skill_descriptor(tool)

    def prepare(
        self,
        context: RuntimeContext,
        tool: McpToolDefinition | SkillDefinition,
        arguments: dict[str, Any],
        call_id: str,
    ) -> HumanTaskRecord | None:
        try:
            schema = (tool.input_schema if isinstance(tool, McpToolDefinition)
                      else tool.manifest.spec.input_schema)
            Draft202012Validator(schema).validate(arguments)
        except ValidationError as exc:
            raise ExecutionError("Tool input schema mismatch") from exc
        with self._prepare_lock:
            return self._prepare(context, tool, arguments, call_id)

    def _prepare(
        self,
        context: RuntimeContext,
        tool: McpToolDefinition | SkillDefinition,
        arguments: dict[str, Any],
        call_id: str,
    ) -> HumanTaskRecord | None:
        clean_context = replace(context, approved_capability_ids=frozenset())
        decision = PolicyEnforcer().evaluate(clean_context, self.descriptor(tool), "execute")
        if decision.allowed:
            return None
        if decision.outcome != "require-approval":
            raise PolicyDeniedError(decision.reason)
        if context.lifecycle != "durable":
            raise PolicyDeniedError("High-risk Tool calls require a durable run")
        subject = self.subject(context, tool, arguments, call_id)
        records = self.tasks.list_for_principal(
            workspace_id=context.principal.workspace_id,
            solution_id=context.solution_id or "_standalone",
            roles=context.principal.roles,
            status=None,
        )
        for task in records:
            if task.run_id == context.run_id and task.subject.get("call_id") == call_id:
                if task.subject != subject:
                    raise PolicyDeniedError("Prepared Tool call changed after approval request")
                return task
        return self.tasks.create(
            run_id=context.run_id,
            workspace_id=context.principal.workspace_id,
            solution_id=context.solution_id or "_standalone",
            kind="approval",
                 title=(f"Execute {tool.binding.server_alias}/{tool.binding.tool_name}"
                     if isinstance(tool, McpToolDefinition) else f"Execute Skill {tool.name}"),
            instructions="Approve only the exact recorded tool call and input digest.",
            assigned_roles=("platform_admin", "capability_admin"),
            subject=subject,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

    async def dispatch(
        self,
        executor: ToolExecutor | SkillExecutor,
        context: RuntimeContext,
        tool: McpToolDefinition | SkillDefinition,
        arguments: dict[str, Any],
        call_id: str,
        task: HumanTaskRecord | None,
    ) -> dict[str, Any]:
        if self.runs is not None:
            run_id = context.run_id
            visited: set[str] = set()
            while run_id:
                if run_id in visited:
                    raise PolicyDeniedError("Run ancestry is invalid")
                visited.add(run_id)
                run = await self.runs.get(run_id)
                if run is None or run.status != "running":
                    raise PolicyDeniedError("Tool run is no longer running")
                run_id = run.context.parent_run_id
        context = replace(context, approved_capability_ids=frozenset())
        subject = self.subject(context, tool, arguments, call_id)
        if task is not None:
            current = self.tasks.get(task.task_id)
            if (
                current is None
                or current.status != "approved"
                or current.subject != subject
                or (current.expires_at and current.expires_at <= datetime.now(UTC))
            ):
                raise PolicyDeniedError(
                    "An unexpired approval for this exact Tool call is required"
                )
            context = replace(
                context, approved_capability_ids=frozenset({self.descriptor(tool).capability_id})
            )
        PolicyEnforcer().require(context, self.descriptor(tool), "execute")
        digest = canonical_digest(subject)
        invocation_id = "toolcall_" + canonical_digest(
            {
                "run": context.run_id,
                "call": call_id,
            }
        ).removeprefix("sha256:")
        reservation = self.invocations.reserve(invocation_id, digest)
        if reservation.result_json is not None:
            return json.loads(reservation.result_json)
        try:
            target = tool.tool_id if isinstance(tool, McpToolDefinition) else tool
            result = await executor.execute(target, arguments, context=context)
        except BaseException:
            self.invocations.fail(invocation_id, "outcome_uncertain")
            raise
        self.invocations.complete(invocation_id, json.dumps(result, ensure_ascii=False))
        return result
