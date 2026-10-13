"""Governed test calls, independent of Tool registration and publication."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from sage.audit import SqliteAuditStore
from sage.control_models import ControlPlaneState
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.domain import Principal
from sage.durable import DurableExecutionPort, DurableRunRequest
from sage.human_tasks import HumanTaskError, SqliteHumanTaskStore
from sage.mcp_control import McpConnector, McpControlError, McpServerManager
from sage.mcp_test_models import (
    ToolRiskReview,
    ToolRiskReviewInput,
    ToolTestInput,
    ToolTestRecord,
)
from sage.sensitive_data import contains_sensitive, redact


class TestInfrastructureUnavailable(RuntimeError):
    pass


class McpToolTests:
    def __init__(
        self,
        store: LocalControlStore,
        servers: McpServerManager,
        connector: McpConnector,
        tasks: SqliteHumanTaskStore,
        audit: SqliteAuditStore,
        backend: DurableExecutionPort | None,
        task_queue: str,
    ) -> None:
        self.store = store
        self.servers = servers
        self.connector = connector
        self.tasks = tasks
        self.audit = audit
        self.backend = backend
        self.task_queue = task_queue

    @staticmethod
    def review_key(server_id: str, tool_name: str, workspace_id: str) -> str:
        return canonical_digest({"server": server_id, "tool": tool_name, "workspace": workspace_id})

    async def binding(self, server_id: str, payload: ToolRiskReviewInput | ToolTestInput):
        server = await self.servers.ensure_fresh_discovery(server_id)
        if server.config_digest != payload.expected_server_config_digest:
            raise McpControlError("MCP configuration changed; reload the tool")
        tool = next((item for item in server.tools if item.name == payload.tool_name), None)
        if tool is None:
            raise KeyError(payload.tool_name)
        if tool.schema_digest != payload.expected_schema_digest:
            raise McpControlError("MCP schema changed; reload the tool")
        return server, tool

    def audit_event(self, principal: Principal, action: str, resource: str, digest: str):
        self.audit.append(
            actor_id=principal.principal_id,
            workspace_id=principal.workspace_id,
            action=action,
            resource=resource,
            outcome="success",
            status_code=200,
            details_digest=digest,
        )

    async def review(
        self,
        server_id: str,
        payload: ToolRiskReviewInput,
        principal: Principal,
    ) -> ToolRiskReview:
        if not principal.roles.intersection({"platform_admin", "capability_admin"}):
            raise PermissionError("Risk reviews require an administrative role")
        await self.binding(server_id, payload)
        key = self.review_key(server_id, payload.tool_name, principal.workspace_id)
        record = ToolRiskReview(
            **payload.model_dump(),
            server_id=server_id,
            review_id=f"review_{uuid4().hex}",
            actor_id=principal.principal_id,
            workspace_id=principal.workspace_id,
        )
        def save_review(state: ControlPlaneState) -> None:
            ranks = {"read-only": 0, "write-low": 1, "write-high": 2, "destructive": 3}
            previous = state.mcp_test_reviews.get(key)
            known = [registration.definition.risk
                     for registration in state.tool_registrations.values()
                     if registration.definition.binding.server_id == server_id
                     and registration.definition.binding.tool_name == payload.tool_name]
            if previous:
                known.append(previous.risk)
            if any(ranks[risk] > ranks[payload.risk] for risk in known):
                raise PermissionError("Risk review cannot downgrade a known tool risk")
            state.mcp_test_reviews[key] = record

        self.store.update(save_review)
        self.audit_event(
            principal, "mcp.tool.risk_review", key, canonical_digest(record.model_dump())
        )
        return record

    def get(self, test_id: str, principal: Principal) -> ToolTestRecord:
        record = self.store.snapshot().mcp_tests.get(test_id)
        if record is None or (record.workspace_id, record.actor_id) != (
            principal.workspace_id,
            principal.principal_id,
        ):
            raise KeyError(test_id)
        return record

    @staticmethod
    def public(record: ToolTestRecord) -> dict[str, Any]:
        result = record.model_dump(mode="json", exclude={"request"})
        if record.request is not None:
            result["arguments"] = McpToolTests.redact(record.request.arguments, None)
        return result

    async def create(self, server_id: str, payload: ToolTestInput, principal: Principal):
        if not principal.roles.intersection({"platform_admin", "capability_admin"}):
            raise PermissionError("Tool tests require an administrative role")
        server, tool = await self.binding(server_id, payload)
        try:
            Draft202012Validator.check_schema(tool.input_schema)
            Draft202012Validator(tool.input_schema).validate(payload.arguments)
        except (SchemaError, ValidationError) as exc:
            raise ValueError(
                f"Invalid arguments/schema at /{'/'.join(map(str, exc.path))}"
            ) from exc
        key = self.review_key(server_id, payload.tool_name, principal.workspace_id)
        review = self.store.snapshot().mcp_test_reviews.get(key)
        if review is None or (
            review.expected_schema_digest != tool.schema_digest
            or review.expected_server_config_digest != server.config_digest
        ):
            raise PermissionError("Review the current tool risk before testing")
        if review.risk != "read-only" and not payload.confirm_write:
            raise PermissionError("Explicit confirmation is required for a real write operation")
        high_risk = review.risk in {"write-high", "destructive"}
        if high_risk and self.backend is None:
            raise TestInfrastructureUnavailable(
                "High-risk tests require Temporal and a SAGE worker"
            )
        secret = os.getenv(server.secret_ref or "") or server.dev_secret
        encoded = json.dumps(payload.arguments, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 262144:
            raise ValueError("Tool arguments exceed 256 KiB")
        if (secret and secret in encoded) or self.sensitive(payload.arguments, tool.input_schema):
            raise ValueError(
                "Test intents cannot persist secret inputs; use server-side references"
            )
        digest = canonical_digest(
            {
                "server": server_id,
                "request": payload.model_dump(),
                "review_id": review.review_id,
                "workspace_id": principal.workspace_id,
                "actor_id": principal.principal_id,
            }
        )
        test_id = (
            "test_"
            + canonical_digest(
                {
                    "owner": principal.principal_id,
                    "workspace": principal.workspace_id,
                    "server": server_id,
                    "key": payload.idempotency_key,
                }
            ).removeprefix("sha256:")[:32]
        )

        def reserve(state: ControlPlaneState) -> tuple[ToolTestRecord, bool]:
            existing = state.mcp_tests.get(test_id)
            if existing:
                if existing.request_digest != digest:
                    raise McpControlError(
                        "Idempotency key belongs to different test arguments/review"
                    )
                return existing, False
            task = (
                self.tasks.create(
                    run_id=test_id,
                    workspace_id=principal.workspace_id,
                    solution_id="_admin-mcp-test",
                    kind="approval",
                    title=f"Test {server.alias}/{tool.name}",
                    instructions="This executes a real MCP write operation.",
                    assigned_roles=("platform_admin", "capability_admin"),
                    subject={
                        "scope": "admin-mcp-test",
                        "test_id": test_id,
                        "request_digest": digest,
                        "tool_name": tool.name,
                        "risk": review.risk,
                    },
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                if high_risk
                else None
            )
            record = ToolTestRecord(
                test_id=test_id,
                server_id=server_id,
                tool_name=payload.tool_name,
                actor_id=principal.principal_id,
                workspace_id=principal.workspace_id,
                request=payload,
                request_digest=digest,
                review_id=review.review_id,
                risk=review.risk,
                created_at=datetime.now(UTC),
                approval_task_id=task.task_id if task else None,
                workflow_id=f"sage-{test_id}" if high_risk else None,
                status="awaiting-approval" if high_risk else "prepared",
            )
            state.mcp_tests[test_id] = record
            return record, True

        record, created = self.store.update(reserve)
        if record.status == "awaiting-approval":
            await self.start_workflow(record)
        elif created:
            record = await self.execute(test_id)
        return self.public(record)

    async def start_workflow(self, record: ToolTestRecord) -> None:
        if self.backend is None:
            raise TestInfrastructureUnavailable("Temporal is unavailable")
        request = DurableRunRequest(
            workflow_id=record.workflow_id or "",
            workflow_type="sage-solution-workflow",
            task_queue=self.task_queue,
            execution_timeout_seconds=86400,
            payload={
                "mcp_test": {
                    "test_id": record.test_id,
                    "server_id": record.server_id,
                    "task_id": record.approval_task_id,
                }
            },
        )
        try:
            await self.backend.start(request)
        except Exception:
            await self.backend.describe(request.workflow_id)

    async def decide(self, test_id: str, principal: Principal, *, approve: bool, reason: str):
        record = self.get(test_id, principal)
        if self.backend is None:
            raise TestInfrastructureUnavailable("Temporal is unavailable")
        if not reason.strip():
            raise ValueError("A decision reason is required")
        task = self.tasks.get(record.approval_task_id or "")
        if task is None:
            raise PermissionError("Matching test approval is required")
        if task.status == "pending":
            task = self.tasks.decide(
                task.task_id,
                actor_id=principal.principal_id,
                actor_roles=principal.roles,
                decision={},
                approve=approve,
                reason=reason,
            )
        elif task.status != ("approved" if approve else "rejected"):
            raise HumanTaskError("Approval was already decided differently")
        await self.start_workflow(record)
        await self.backend.signal(
            record.workflow_id or "",
            "human_task_decided",
            task.model_dump(mode="json"),
        )
        self.audit_event(principal, "mcp.tool.test_decision", test_id, task.request_digest)
        return self.public(self.get(test_id, principal))

    async def execute(self, test_id: str) -> ToolTestRecord:
        record = self.store.snapshot().mcp_tests[test_id]
        if record.status not in {"prepared", "awaiting-approval"}:
            return record
        if record.approval_task_id:
            task = self.tasks.get(record.approval_task_id)
            if (
                task is None
                or task.subject.get("request_digest") != record.request_digest
                or (
                    task.subject.get("scope") != "admin-mcp-test"
                    or task.run_id != test_id
                    or task.workspace_id != record.workspace_id
                )
            ):
                raise PermissionError("Matching test approval is required")
            if task.status == "rejected":
                return self.finish(record, "rejected", {}, 0, expected_status=record.status)
            if task.expires_at and task.expires_at <= datetime.now(UTC):
                return self.finish(
                    record, "failed", {"error": "approval_expired"}, 0,
                    expected_status=record.status,
                )
            if task.status != "approved":
                raise PermissionError("Unexpired approval is required")
        if record.request is None:
            raise McpControlError("Test intent is unavailable")
        payload = record.request
        try:
            server, tool = await self.binding(record.server_id, payload)
        except (McpControlError, KeyError):
            return self.finish(
                record, "failed", {"error": "tool_binding_changed"}, 0,
                expected_status=record.status,
            )
        key = self.review_key(record.server_id, payload.tool_name, record.workspace_id)

        def claim(state: ControlPlaneState) -> bool:
            review = state.mcp_test_reviews.get(key)
            if review is None or review.review_id != record.review_id:
                raise McpControlError("Risk review changed; create a new test")
            if state.mcp_tests[test_id].status not in {"prepared", "awaiting-approval"}:
                return False
            state.mcp_tests[test_id] = record.model_copy(update={"status": "running"})
            return True

        try:
            claimed = self.store.update(claim)
        except McpControlError:
            return self.finish(
                record, "failed", {"error": "risk_review_changed"}, 0,
                expected_status=record.status,
            )
        if not claimed:
            return self.store.snapshot().mcp_tests[test_id]
        started = monotonic()
        status = "completed"
        try:
            async with asyncio.timeout(payload.timeout_seconds):
                raw = getattr(self.connector, "invoke_raw", None)
                result = (
                    await raw(server, tool.name, payload.arguments)
                    if raw
                    else {
                        "isError": False,
                        "content": [],
                        "structuredContent": await self.connector.invoke(
                            server, tool.name, payload.arguments
                        ),
                    }
                )
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > payload.max_output_bytes:
                result = {"error": "output_limit_exceeded", "bytes": len(encoded)}
                status = "invalid-output"
            elif result.get("isError"):
                status = "tool-error"
            elif isinstance(result.get("structuredContent"), dict):
                Draft202012Validator(tool.output_schema).validate(result["structuredContent"])
            result = self.redact(result, os.getenv(server.secret_ref or "") or server.dev_secret,
                                 {"properties": {"structuredContent": tool.output_schema}})
        except (ValidationError, SchemaError):
            result = {"error": "output_schema_mismatch"}
            status = "invalid-output"
        except asyncio.CancelledError:
            self.finish(
                record, "outcome-uncertain" if record.risk != "read-only" else "failed",
                {"error": "cancelled"}, int((monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            result = {"error": "timeout" if isinstance(exc, TimeoutError) else "upstream_failure"}
            status = "outcome-uncertain" if record.risk != "read-only" else "failed"
        return self.finish(record, status, result, int((monotonic() - started) * 1000))

    def finish(
        self, record: ToolTestRecord, status: str, result: dict[str, Any], elapsed: int,
        *, expected_status: str = "running",
    ):
        updated = record.model_copy(
            update={
                "status": status,
                "result": result,
                "elapsed_ms": elapsed,
                "finished_at": datetime.now(UTC),
                "request": None,
            }
        )
        def save(state: ControlPlaneState):
            current = state.mcp_tests[record.test_id]
            if current.status != expected_status:
                return current, False
            state.mcp_tests[record.test_id] = updated
            return updated, True

        saved, changed = self.store.update(save)
        if not changed:
            return saved
        self.audit.append(
            actor_id=record.actor_id,
            workspace_id=record.workspace_id,
            action="mcp.tool.test_call",
            resource=record.test_id,
            outcome="success" if status == "completed" else "failed",
            status_code=200,
            details_digest=record.request_digest,
        )
        return updated

    sensitive = staticmethod(contains_sensitive)
    redact = staticmethod(redact)
