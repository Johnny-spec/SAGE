"""Authenticated control-plane HTTP API."""

import hmac
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from sage.agent_drafts import AgentDraftManager
from sage.audit import AuditRecord
from sage.catalog import CatalogError, CatalogManager, admin_token_fingerprint
from sage.control_models import (
    AgentDraftRecord,
    ApprovalRecord,
    CatalogCandidateRecord,
    EvaluationReportRecord,
    GenerationRecord,
    McpServerRecord,
    SkillPackageRecord,
    SolutionComponentKind,
    SolutionComponentLock,
    SolutionDefinitionRecord,
    SolutionExecutionProfile,
    SolutionReleaseRecord,
    ToolRegistrationRecord,
)
from sage.control_settings import ControlPlaneSettings
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest, directory_digest
from sage.domain import Principal
from sage.evaluations import EvaluationError, EvaluationManager
from sage.feedback import (
    EvaluationSampleCandidateInput,
    EvaluationSampleCandidateRecord,
    FeedbackAuthorizationError,
    FeedbackCategory,
    FeedbackConflictError,
    FeedbackDispositionInput,
    FeedbackDispositionRecord,
    FeedbackQualitySummary,
    FeedbackRecord,
    FeedbackTargetError,
    new_evaluation_sample_candidate,
    new_feedback_disposition,
)
from sage.human_tasks import HumanTaskRecord
from sage.mcp_control import McpControlError, McpServerManager
from sage.mcp_test_models import ToolRiskReviewInput, ToolTestInput
from sage.mcp_testing import TestInfrastructureUnavailable
from sage.quality_evaluations import (
    EvaluationComparisonInput,
    EvaluationComparisonRecord,
    EvaluationSampleSetInput,
    EvaluationSampleSetRecord,
    QualityEvaluationConflictError,
    QualityEvaluationError,
)
from sage.skill_packages import SkillImportError, SkillPackageManager
from sage.solution_build_api import create_solution_build_router
from sage.solution_packages import (
    SolutionBundlePackageManager,
    SolutionPackageError,
    parse_solution_bundle,
)
from sage.solutions import SolutionError, SolutionManager
from sage.workflow_api import create_workflow_router


class SubagentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=40_000)
    capability_ids: list[str] = Field(default_factory=list, max_length=100)
    exclusive_capability_ids: list[str] = Field(default_factory=list, max_length=100)


class DelegationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=64)
    target_agent_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    max_calls: int = Field(default=1, ge=1, le=20)


class AgentDefinitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    instructions: str = Field(min_length=1, max_length=40_000)
    capability_ids: list[str] = Field(default_factory=list, max_length=200)
    subagents: list[SubagentInput] = Field(default_factory=list, max_length=20)
    delegations: list[DelegationInput] = Field(default_factory=list, max_length=20)
    memory_policy: Literal["none", "thread", "thread-and-user"] = "thread"
    max_steps: int = Field(default=40, ge=1, le=200)
    max_model_calls: int = Field(default=100, ge=1, le=1000)
    max_tool_calls: int = Field(default=100, ge=1, le=1000)
    max_subagent_calls: int = Field(default=20, ge=1, le=200)


class CloneAgentInput(BaseModel):
    version: str = Field(min_length=1, max_length=64)


class McpServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=64)
    transport: Literal["streamable-http", "stdio"]
    url: str | None = Field(default=None, max_length=2048)
    command_preset: str | None = Field(default=None, max_length=128)
    secret_ref: str | None = Field(default=None, max_length=256)
    dev_secret: str | None = Field(default=None, max_length=8192)


class McpImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=40_000)
    risk: Literal["read-only", "write-low", "write-high", "destructive"] = "read-only"


class McpToolRegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    risk: Literal["read-only", "write-low", "write-high", "destructive"] = "read-only"


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    agent_draft_ids: list[str] = Field(default_factory=list, max_length=100)
    skill_package_ids: list[str] = Field(default_factory=list, max_length=100)
    tool_registration_ids: list[str] = Field(default_factory=list, max_length=100)
    solution_release_ids: list[str] = Field(default_factory=list, max_length=100)
    default_agent_id: str | None = Field(default=None, max_length=64)


class ToolTestDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str = Field(min_length=1, max_length=2000)


class SolutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    owner: str = Field(min_length=1, max_length=200)
    entry_agent_id: str | None = Field(default=None, max_length=64)
    allowed_roles: list[str] = Field(default_factory=lambda: ["user"], min_length=1)
    supported_profiles: list[SolutionExecutionProfile] = Field(
        default_factory=lambda: ["direct", "interactive"],
        min_length=1,
        max_length=5,
    )


class SolutionComponentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SolutionComponentKind
    component_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SolutionReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    components: list[SolutionComponentInput] = Field(min_length=1, max_length=500)
    entry_agent_id: str | None = Field(default=None, max_length=64)
    allowed_roles: list[str] | None = Field(default=None, min_length=1, max_length=100)
    supported_profiles: list[SolutionExecutionProfile] | None = Field(
        default=None,
        min_length=1,
        max_length=5,
    )
    source_generation_id: str | None = Field(default=None, max_length=128)


class ApprovalDecisionInput(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class EvaluationReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1, max_length=200)
    status: Literal["passed", "failed"]
    metrics: dict[str, float] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=4000)


class HumanTaskCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    step_id: str | None = Field(default=None, max_length=200)
    kind: Literal["input", "approval"]
    title: str = Field(min_length=1, max_length=500)
    instructions: str = Field(default="", max_length=4000)
    assigned_roles: list[str] = Field(min_length=1, max_length=100)
    subject: dict[str, Any] = Field(default_factory=dict)
    payload_schema: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class AdminStatus(BaseModel):
    active_generation_id: str
    solutions: int
    solution_releases: int
    agents: int
    skills: int
    mcp_servers: int
    pending_approvals: int
    plaintext_dev_secrets: bool


class CapabilityAdminView(BaseModel):
    capability_id: str
    name: str
    version: str
    content_digest: str
    source: Literal["active", "draft"]
    executable: bool


def _admin_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    settings: ControlPlaneSettings = request.app.state.control_settings
    if not settings.admin_auth_enabled:
        request.state.admin_actor = "admin-auth-disabled"
        return "admin-auth-disabled"
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        if principal.roles.intersection({"platform_admin", "capability_admin"}):
            actor = f"entra:{principal.principal_id}"
            request.state.admin_actor = actor
            return actor
        raise HTTPException(status_code=403, detail="Administration role is required")
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Control plane is disabled until SAGE_ADMIN_TOKEN is configured",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid administration token")
    actor = admin_token_fingerprint(token)
    request.state.admin_actor = f"admin:{actor}"
    return actor


def _not_found(kind: str, identifier: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{kind} not found: {identifier}")


def _mcp_view(record: McpServerRecord) -> dict[str, Any]:
    value = record.model_dump(mode="json", exclude={"dev_secret"})
    value["secret_configured"] = bool(record.secret_ref or record.dev_secret)
    return value


def _available_capabilities(request: Request) -> tuple[set[str], set[tuple[str, str]]]:
    store: LocalControlStore = request.app.state.control_store
    state = store.snapshot()
    active = request.app.state.runtime_manager.active
    capability_ids = {
        f"skill:{item.name}"
        for item in active.registry
        if item.manifest.spec.policy.execution_enabled
    }
    capability_ids.update(
        f"skill:{item.name}"
        for item in state.skills.values()
        if item.status in {"valid", "published"} and item.executable and item.name
    )
    published = {
        (definition["agent_id"], definition["version"])
        for generation in state.generations.values()
        for definition in generation.agent_definitions.values()
    }
    capability_ids.update(
        tool.tool_id for tool in active.generation.tool_definitions.values() if tool.enabled
    )
    capability_ids.update(
        record.tool_id for record in state.tool_registrations.values() if record.definition.enabled
    )
    return capability_ids, published


def create_admin_router() -> APIRouter:
    router = APIRouter(prefix="/v1/admin", dependencies=[Depends(_admin_actor)])
    router.include_router(create_workflow_router())
    router.include_router(create_solution_build_router())

    def test_principal(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal):
            return principal
        return Principal(
            workspace_id="ws_default",
            principal_id=request.state.admin_actor,
            roles=frozenset({"platform_admin"}),
            authentication_method="local",
        )

    def require_idempotency_key(request: Request) -> str:
        value = (request.headers.get("idempotency-key") or "").strip()
        if not value or len(value) > 255:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key is required and must not exceed 255 characters",
            )
        return value

    async def test_operation(operation):
        try:
            return await operation
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP tool/test not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TestInfrastructureUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except McpControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="MCP test service unavailable") from exc

    @router.post("/mcp/servers/{server_id}/tool-risk-reviews")
    async def review_tool_risk(server_id: str, payload: ToolRiskReviewInput, request: Request):
        return await test_operation(
            request.app.state.mcp_tool_tests.review(
                server_id,
                payload,
                test_principal(request),
            )
        )

    @router.get("/mcp/servers/{server_id}/tool-risk-reviews")
    async def tool_risk_reviews(server_id: str, request: Request):
        principal = test_principal(request)
        return [
            record
            for record in request.app.state.control_store.snapshot().mcp_test_reviews.values()
            if record.server_id == server_id and record.workspace_id == principal.workspace_id
        ]

    @router.post("/mcp/servers/{server_id}/tool-tests")
    async def create_tool_test(
        server_id: str,
        payload: ToolTestInput,
        request: Request,
        response: Response,
    ):
        result = await test_operation(
            request.app.state.mcp_tool_tests.create(
                server_id,
                payload,
                test_principal(request),
            )
        )
        if result["status"] in {"prepared", "awaiting-approval", "running"}:
            response.status_code = 202
        return result

    @router.get("/mcp/servers/{server_id}/tool-tests")
    async def list_tool_tests(server_id: str, request: Request):
        principal = test_principal(request)
        return [
            request.app.state.mcp_tool_tests.public(record)
            for record in request.app.state.control_store.snapshot().mcp_tests.values()
            if record.server_id == server_id
            and record.workspace_id == principal.workspace_id
            and record.actor_id == principal.principal_id
        ][-100:]

    def owned_test(server_id: str, test_id: str, request: Request):
        try:
            record = request.app.state.mcp_tool_tests.get(test_id, test_principal(request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP test not found") from exc
        if record.server_id != server_id:
            raise HTTPException(status_code=404, detail="MCP test not found")
        return record

    @router.get("/mcp/servers/{server_id}/tool-tests/{test_id}")
    async def get_tool_test(server_id: str, test_id: str, request: Request):
        return request.app.state.mcp_tool_tests.public(owned_test(server_id, test_id, request))

    @router.post("/mcp/servers/{server_id}/tool-tests/{test_id}/decision", status_code=202)
    async def decide_tool_test(
        server_id: str,
        test_id: str,
        payload: ToolTestDecisionInput,
        request: Request,
    ):
        owned_test(server_id, test_id, request)
        return await test_operation(
            request.app.state.mcp_tool_tests.decide(
                test_id,
                test_principal(request),
                approve=payload.approve,
                reason=payload.reason,
            )
        )

    @router.post("/mcp/servers/{server_id}/tool-tests/{test_id}/dispatch")
    async def dispatch_tool_test(server_id: str, test_id: str, request: Request):
        service = request.app.state.mcp_tool_tests
        record = request.app.state.control_store.snapshot().mcp_tests.get(test_id)
        principal = test_principal(request)
        if (
            record is None
            or record.server_id != server_id
            or record.workspace_id != principal.workspace_id
        ):
            raise HTTPException(status_code=404, detail="MCP test not found")
        if not record.approval_task_id:
            raise HTTPException(status_code=409, detail="Not an approved durable test")
        return service.public(await test_operation(service.execute(test_id)))

    @router.get("/status", response_model=AdminStatus)
    async def status(request: Request) -> AdminStatus:
        settings: ControlPlaneSettings = request.app.state.control_settings
        state = request.app.state.control_store.snapshot()
        return AdminStatus(
            active_generation_id=state.active_generation_id,
            solutions=len(state.solutions),
            solution_releases=len(state.solution_releases),
            agents=len(state.agents),
            skills=len(state.skills),
            mcp_servers=len(state.mcp_servers),
            pending_approvals=sum(item.status == "pending" for item in state.approvals.values()),
            plaintext_dev_secrets=settings.allow_plaintext_dev_secrets,
        )

    async def skill_run_owned_workspace(run_id: str, request: Request):
        run, _, _ = await request.app.state.skill_orchestrator.resolve(run_id)
        if run.context.principal.workspace_id != test_principal(request).workspace_id:
            raise HTTPException(status_code=404, detail="Skill Run not found")

    @router.post("/skill-runs/{run_id}/model")
    async def model_skill_run(run_id: str, request: Request):
        await skill_run_owned_workspace(run_id, request)
        return await request.app.state.skill_orchestrator.model(run_id)

    @router.post("/skill-runs/{run_id}/tool")
    async def durable_tool_run(run_id: str, request: Request):
        await skill_run_owned_workspace(run_id, request)
        return await request.app.state.skill_orchestrator.tool(run_id)

    @router.post("/skill-runs/{run_id}/result")
    async def finish_skill_run(run_id: str, payload: dict[str, Any], request: Request):
        await skill_run_owned_workspace(run_id, request)
        return await request.app.state.skill_orchestrator.finish(run_id, payload.get("output"))

    @router.get("/audits", response_model=list[AuditRecord])
    async def list_audits(request: Request, limit: int = 100) -> list[AuditRecord]:
        try:
            return list(request.app.state.audit_store.list(limit=limit))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def quality_feedback(feedback_id: str, request: Request) -> FeedbackRecord:
        principal = test_principal(request)
        record = request.app.state.feedback_store.get(feedback_id)
        if (
            record is None
            or record.workspace_id != principal.workspace_id
            or not record.quality_use_allowed
        ):
            raise _not_found("Feedback", feedback_id)
        return record

    @router.get("/feedback", response_model=list[FeedbackRecord])
    async def list_feedback(
        request: Request,
        solution_id: str | None = None,
        category: FeedbackCategory | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        principal = test_principal(request)
        try:
            return list(
                request.app.state.feedback_store.list_for_workspace(
                    principal.workspace_id,
                    solution_id=solution_id,
                    category=category,
                    limit=limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/feedback/quality-summary", response_model=FeedbackQualitySummary)
    async def feedback_quality_summary(
        request: Request,
        solution_id: str | None = None,
    ) -> FeedbackQualitySummary:
        return request.app.state.feedback_store.quality_summary(
            test_principal(request).workspace_id,
            solution_id=solution_id,
        )

    @router.get(
        "/feedback/{feedback_id}/dispositions",
        response_model=list[FeedbackDispositionRecord],
    )
    async def list_feedback_dispositions(
        feedback_id: str,
        request: Request,
    ) -> list[FeedbackDispositionRecord]:
        feedback = quality_feedback(feedback_id, request)
        return list(
            request.app.state.feedback_store.list_dispositions(
                feedback.feedback_id,
                workspace_id=feedback.workspace_id,
            )
        )

    @router.post(
        "/feedback/{feedback_id}/dispositions",
        response_model=FeedbackDispositionRecord,
        status_code=201,
    )
    async def append_feedback_disposition(
        feedback_id: str,
        payload: FeedbackDispositionInput,
        request: Request,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> FeedbackDispositionRecord:
        feedback = quality_feedback(feedback_id, request)
        try:
            disposition = new_feedback_disposition(feedback, payload, actor_id=actor)
            return request.app.state.feedback_store.append_disposition(disposition)
        except FeedbackAuthorizationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/feedback/{feedback_id}/evaluation-candidates",
        response_model=EvaluationSampleCandidateRecord,
        status_code=201,
    )
    async def create_evaluation_sample_candidate(
        feedback_id: str,
        payload: EvaluationSampleCandidateInput,
        request: Request,
        response: Response,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> EvaluationSampleCandidateRecord:
        feedback = quality_feedback(feedback_id, request)
        source_run = await request.app.state.run_store.get(feedback.run_id)
        if source_run is None or source_run.context.principal.workspace_id != feedback.workspace_id:
            raise HTTPException(status_code=409, detail="Feedback source Run is unavailable")
        try:
            candidate = new_evaluation_sample_candidate(
                feedback,
                payload,
                actor_id=actor,
                input_data=source_run.input_data,
            )
            disposition = new_feedback_disposition(
                feedback,
                FeedbackDispositionInput(
                    action="accepted-as-evaluation-candidate",
                    reason="Accepted as an evaluation sample candidate",
                ),
                actor_id=actor,
            )
            stored, created = request.app.state.feedback_store.create_evaluation_candidate(
                candidate,
                disposition,
                request_digest=canonical_digest(
                    {
                        "feedback_digest": feedback.record_digest,
                        "candidate": payload.model_dump(mode="json"),
                    }
                ),
            )
        except FeedbackAuthorizationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FeedbackTargetError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return stored

    @router.get(
        "/evaluation-sample-candidates",
        response_model=list[EvaluationSampleCandidateRecord],
    )
    async def list_evaluation_sample_candidates(
        request: Request,
        solution_id: str | None = None,
        limit: int = 100,
    ) -> list[EvaluationSampleCandidateRecord]:
        principal = test_principal(request)
        try:
            return list(
                request.app.state.feedback_store.list_evaluation_candidates(
                    principal.workspace_id,
                    solution_id=solution_id,
                    limit=limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/evaluation-sample-sets",
        response_model=EvaluationSampleSetRecord,
        status_code=201,
    )
    async def create_evaluation_sample_set(
        payload: EvaluationSampleSetInput,
        request: Request,
        response: Response,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> EvaluationSampleSetRecord:
        principal = test_principal(request)
        candidates = tuple(
            request.app.state.feedback_store.get_evaluation_candidate(candidate_id)
            for candidate_id in payload.candidate_ids
        )
        if any(
            candidate is None or candidate.workspace_id != principal.workspace_id
            for candidate in candidates
        ):
            raise HTTPException(status_code=404, detail="Evaluation sample candidate not found")
        try:
            stored, created = request.app.state.quality_evaluation_store.create_sample_set(
                payload,
                tuple(candidate for candidate in candidates if candidate is not None),
                workspace_id=principal.workspace_id,
                actor_id=actor,
                idempotency_key=require_idempotency_key(request),
            )
        except QualityEvaluationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QualityEvaluationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return stored

    @router.get(
        "/evaluation-sample-sets",
        response_model=list[EvaluationSampleSetRecord],
    )
    async def list_evaluation_sample_sets(
        request: Request,
        solution_id: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[EvaluationSampleSetRecord]:
        try:
            return list(
                request.app.state.quality_evaluation_store.list_sample_sets(
                    test_principal(request).workspace_id,
                    solution_id=solution_id,
                    target_id=target_id,
                    limit=limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def scoped_sample_set(
        sample_set_id: str,
        request: Request,
    ) -> EvaluationSampleSetRecord:
        record = request.app.state.quality_evaluation_store.get_sample_set(sample_set_id)
        if (
            record is None
            or record.workspace_id != test_principal(request).workspace_id
        ):
            raise _not_found("Evaluation sample set", sample_set_id)
        return record

    @router.get(
        "/evaluation-sample-sets/{sample_set_id}",
        response_model=EvaluationSampleSetRecord,
    )
    async def get_evaluation_sample_set(
        sample_set_id: str,
        request: Request,
    ) -> EvaluationSampleSetRecord:
        return scoped_sample_set(sample_set_id, request)

    @router.post(
        "/evaluation-sample-sets/{sample_set_id}/comparisons",
        response_model=EvaluationComparisonRecord,
        status_code=201,
    )
    async def create_evaluation_comparison(
        sample_set_id: str,
        payload: EvaluationComparisonInput,
        request: Request,
        response: Response,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> EvaluationComparisonRecord:
        sample_set = scoped_sample_set(sample_set_id, request)
        try:
            stored, created = request.app.state.quality_evaluation_store.create_comparison(
                sample_set,
                payload,
                actor_id=actor,
                idempotency_key=require_idempotency_key(request),
            )
        except QualityEvaluationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QualityEvaluationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return stored

    @router.get(
        "/evaluation-sample-sets/{sample_set_id}/comparisons",
        response_model=list[EvaluationComparisonRecord],
    )
    async def list_evaluation_comparisons(
        sample_set_id: str,
        request: Request,
        limit: int = 100,
    ) -> list[EvaluationComparisonRecord]:
        sample_set = scoped_sample_set(sample_set_id, request)
        try:
            return list(
                request.app.state.quality_evaluation_store.list_comparisons(
                    sample_set.sample_set_id,
                    workspace_id=sample_set.workspace_id,
                    limit=limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/human-tasks", response_model=HumanTaskRecord, status_code=201)
    async def create_human_task(
        payload: HumanTaskCreateInput,
        request: Request,
    ) -> HumanTaskRecord:
        run = await request.app.state.run_store.get(payload.run_id)
        if run is None:
            raise _not_found("Run", payload.run_id)
        if run.context.lifecycle != "durable" or run.context.solution_id is None:
            raise HTTPException(
                status_code=409,
                detail="HumanTasks require a durable Solution Run",
            )
        return request.app.state.human_task_store.create(
            run_id=run.context.run_id,
            workspace_id=run.context.principal.workspace_id,
            solution_id=run.context.solution_id,
            kind=payload.kind,
            title=payload.title,
            instructions=payload.instructions,
            assigned_roles=tuple(payload.assigned_roles),
            subject=payload.subject,
            payload_schema=payload.payload_schema,
            step_id=payload.step_id,
            expires_at=payload.expires_at,
        )

    @router.get("/solutions", response_model=list[SolutionDefinitionRecord])
    async def list_solutions(request: Request) -> list[SolutionDefinitionRecord]:
        manager: SolutionManager = request.app.state.solution_manager
        return manager.list_solutions()

    @router.post("/solutions", response_model=SolutionDefinitionRecord, status_code=201)
    async def create_solution(
        payload: SolutionInput,
        request: Request,
    ) -> SolutionDefinitionRecord:
        manager: SolutionManager = request.app.state.solution_manager
        try:
            if (
                payload.entry_agent_id
                and payload.entry_agent_id
                not in request.app.state.runtime_manager.active.generation.agent_definitions
            ):
                raise SolutionError(f"Entry Agent is not active: {payload.entry_agent_id}")
            return manager.create(
                solution_id=payload.solution_id,
                title=payload.title,
                description=payload.description,
                owner=payload.owner,
                entry_agent_id=payload.entry_agent_id,
                allowed_roles=tuple(payload.allowed_roles),
                supported_profiles=tuple(payload.supported_profiles),
            )
        except SolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/solutions/{solution_id}/releases",
        response_model=list[SolutionReleaseRecord],
    )
    async def list_solution_releases(
        solution_id: str,
        request: Request,
    ) -> list[SolutionReleaseRecord]:
        manager: SolutionManager = request.app.state.solution_manager
        try:
            return manager.list_releases(solution_id)
        except KeyError as exc:
            raise _not_found("Solution", solution_id) from exc

    @router.get("/solution-releases", response_model=list[SolutionReleaseRecord])
    async def list_all_solution_releases(request: Request) -> list[SolutionReleaseRecord]:
        manager: SolutionManager = request.app.state.solution_manager
        return manager.list_all_releases()

    @router.post(
        "/solutions/{solution_id}/releases",
        response_model=SolutionReleaseRecord,
        status_code=201,
    )
    async def create_solution_release(
        solution_id: str,
        payload: SolutionReleaseInput,
        request: Request,
    ) -> SolutionReleaseRecord:
        manager: SolutionManager = request.app.state.solution_manager
        try:
            return manager.create_release(
                solution_id,
                version=payload.version,
                components=tuple(
                    SolutionComponentLock.model_validate(component.model_dump())
                    for component in payload.components
                ),
                entry_agent_id=payload.entry_agent_id,
                allowed_roles=(
                    tuple(payload.allowed_roles) if payload.allowed_roles is not None else None
                ),
                supported_profiles=(
                    tuple(payload.supported_profiles)
                    if payload.supported_profiles is not None
                    else None
                ),
                source_generation_id=payload.source_generation_id,
            )
        except KeyError as exc:
            raise _not_found("Solution", solution_id) from exc
        except SolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/solutions/imports", status_code=201)
    async def import_solution_bundle(
        request: Request,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        package_relpath: str | None = None
        if Path(file.filename or "").suffix.casefold() == ".zip":
            packages: SolutionBundlePackageManager = request.app.state.solution_bundle_packages
            try:
                manifest, package_relpath, package_metadata = await packages.import_zip(file)
            except SolutionPackageError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            package_metadata = {}
            if request.app.state.control_settings.signed_solutions_required:
                await file.close()
                raise HTTPException(
                    status_code=400,
                    detail="Production Solution imports require a signed ZIP Bundle",
                )
            content = await file.read(1024 * 1024 + 1)
            await file.close()
            if len(content) > 1024 * 1024:
                raise HTTPException(status_code=400, detail="Solution manifest exceeds 1 MiB")
            try:
                manifest = parse_solution_bundle(content.decode("utf-8"))
            except (UnicodeDecodeError, SolutionPackageError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if manifest.component_paths():
                raise HTTPException(
                    status_code=400,
                    detail="Solution manifests with component paths must be uploaded as ZIP",
                )
        manager: SolutionManager = request.app.state.solution_manager
        try:
            solution = manager.get(manifest.metadata.name)
        except KeyError:
            solution = manager.create(
                solution_id=manifest.metadata.name,
                title=manifest.metadata.title,
                description=manifest.spec.description,
                owner=manifest.spec.owner,
                entry_agent_id=manifest.spec.entry_agent_id,
                allowed_roles=manifest.spec.allowed_roles,
                supported_profiles=manifest.spec.supported_profiles,
            )
        try:
            release = manager.create_release(
                solution.solution_id,
                version=manifest.metadata.version,
                components=manifest.component_locks(),
                entry_agent_id=manifest.spec.entry_agent_id,
                allowed_roles=manifest.spec.allowed_roles,
                supported_profiles=manifest.spec.supported_profiles,
                source_generation_id=request.app.state.control_store.snapshot().active_generation_id,
                component_paths=manifest.component_paths(),
                package_relpath=package_relpath,
                package_metadata=package_metadata,
            )
        except SolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "solution": solution.model_dump(mode="json"),
            "release": release.model_dump(mode="json"),
        }

    @router.get(
        "/solution-releases/{release_id}/evaluations",
        response_model=list[EvaluationReportRecord],
    )
    async def list_release_evaluations(
        release_id: str,
        request: Request,
    ) -> list[EvaluationReportRecord]:
        manager: EvaluationManager = request.app.state.evaluation_manager
        try:
            return manager.list_for_release(release_id)
        except KeyError as exc:
            raise _not_found("Solution Release", release_id) from exc

    @router.post(
        "/solution-releases/{release_id}/evaluations",
        response_model=EvaluationReportRecord,
        status_code=201,
    )
    async def record_release_evaluation(
        release_id: str,
        payload: EvaluationReportInput,
        request: Request,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> EvaluationReportRecord:
        manager: EvaluationManager = request.app.state.evaluation_manager
        try:
            return manager.record(
                release_id,
                suite_id=payload.suite_id,
                status=payload.status,
                metrics=payload.metrics,
                summary=payload.summary,
                actor_fingerprint=actor,
            )
        except KeyError as exc:
            raise _not_found("Solution Release", release_id) from exc
        except EvaluationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/capabilities", response_model=list[CapabilityAdminView])
    async def capabilities(request: Request) -> list[CapabilityAdminView]:
        state = request.app.state.control_store.snapshot()
        active = request.app.state.runtime_manager.active
        result = [
            CapabilityAdminView(
                capability_id=f"skill:{definition.name}",
                name=definition.name,
                version=definition.version,
                content_digest=directory_digest(definition.package_root),
                source="active",
                executable=definition.manifest.spec.policy.execution_enabled,
            )
            for definition in active.registry
        ]
        result.extend(
            CapabilityAdminView(
                capability_id=tool.tool_id,
                name=f"{tool.binding.server_alias}/{tool.binding.tool_name}",
                version=tool.version,
                content_digest=tool.content_digest,
                source="active",
                executable=tool.enabled,
            )
            for tool in active.generation.tool_definitions.values()
        )
        active_ids = {item.capability_id for item in result}
        draft_tools = {}
        for record in state.tool_registrations.values():
            if record.tool_id not in active_ids:
                draft_tools[record.tool_id] = record.definition
        result.extend(
            CapabilityAdminView(
                capability_id=tool.tool_id,
                name=f"{tool.binding.server_alias}/{tool.binding.tool_name}",
                version=tool.version,
                content_digest=tool.content_digest,
                source="draft",
                executable=tool.enabled,
            )
            for tool in draft_tools.values()
        )
        result.extend(
            CapabilityAdminView(
                capability_id=f"skill:{record.name}",
                name=record.name or record.package_id,
                version=record.version or "unknown",
                content_digest=record.content_digest,
                source="draft",
                executable=record.executable,
            )
            for record in state.skills.values()
            if record.name and f"skill:{record.name}" not in active_ids
        )
        return result

    @router.get("/agents", response_model=list[AgentDraftRecord])
    async def list_agent_drafts(request: Request) -> list[AgentDraftRecord]:
        manager: AgentDraftManager = request.app.state.agent_drafts
        return manager.list_drafts()

    @router.post("/agents", response_model=AgentDraftRecord, status_code=201)
    async def create_agent(payload: AgentDefinitionInput, request: Request) -> AgentDraftRecord:
        manager: AgentDraftManager = request.app.state.agent_drafts
        return manager.create(payload.model_dump(mode="python"))

    @router.put("/agents/{draft_id}", response_model=AgentDraftRecord)
    async def update_agent(
        draft_id: str,
        payload: AgentDefinitionInput,
        request: Request,
    ) -> AgentDraftRecord:
        manager: AgentDraftManager = request.app.state.agent_drafts
        try:
            return manager.update(draft_id, payload.model_dump(mode="python"))
        except KeyError as exc:
            raise _not_found("Agent draft", draft_id) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/agents/{draft_id}/clone", response_model=AgentDraftRecord, status_code=201)
    async def clone_agent(
        draft_id: str,
        payload: CloneAgentInput,
        request: Request,
    ) -> AgentDraftRecord:
        manager: AgentDraftManager = request.app.state.agent_drafts
        try:
            return manager.clone(draft_id, payload.version)
        except KeyError as exc:
            raise _not_found("Agent draft", draft_id) from exc

    @router.post("/agents/{draft_id}/validate", response_model=AgentDraftRecord)
    async def validate_agent(draft_id: str, request: Request) -> AgentDraftRecord:
        manager: AgentDraftManager = request.app.state.agent_drafts
        capabilities, published = _available_capabilities(request)
        try:
            return manager.validate(draft_id, capabilities, published)
        except KeyError as exc:
            raise _not_found("Agent draft", draft_id) from exc

    @router.get("/skills", response_model=list[SkillPackageRecord])
    async def list_skill_packages(request: Request) -> list[SkillPackageRecord]:
        manager: SkillPackageManager = request.app.state.skill_packages
        return manager.list_packages()

    @router.post("/skills/uploads", response_model=SkillPackageRecord, status_code=201)
    async def upload_skill(
        request: Request,
        file: Annotated[UploadFile, File()],
    ) -> SkillPackageRecord:
        manager: SkillPackageManager = request.app.state.skill_packages
        try:
            return await manager.upload(file)
        except (SkillImportError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/skills/{package_id}/validate", response_model=SkillPackageRecord)
    async def validate_skill(package_id: str, request: Request) -> SkillPackageRecord:
        manager: SkillPackageManager = request.app.state.skill_packages
        try:
            return manager.validate(package_id)
        except KeyError as exc:
            raise _not_found("Skill package", package_id) from exc

    @router.get("/skills/{package_id}/download/{kind}")
    async def download_skill(package_id: str, kind: str, request: Request) -> FileResponse:
        if kind not in {"original", "normalized"}:
            raise HTTPException(
                status_code=400, detail="Download kind must be original or normalized"
            )
        manager: SkillPackageManager = request.app.state.skill_packages
        try:
            path = manager.download_path(package_id, kind)
            record = manager.get(package_id)
        except KeyError as exc:
            raise _not_found("Skill artifact", f"{package_id}:{kind}") from exc
        return FileResponse(
            path,
            media_type="application/zip",
            filename=(
                record.original_filename
                if kind == "original"
                else f"{record.name or package_id}-{record.version or 'draft'}-normalized.zip"
            ),
        )

    @router.get("/mcp/servers")
    async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
        manager: McpServerManager = request.app.state.mcp_servers
        return [_mcp_view(item) for item in manager.list_servers()]

    @router.post("/mcp/servers", status_code=201)
    async def create_mcp_server(
        payload: McpServerInput,
        request: Request,
    ) -> dict[str, Any]:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            return _mcp_view(manager.create(payload.model_dump(mode="python")))
        except McpControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/mcp/servers/{server_id}")
    async def update_mcp_server(
        server_id: str,
        payload: McpServerInput,
        request: Request,
    ) -> dict[str, Any]:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            return _mcp_view(manager.update(server_id, payload.model_dump(mode="python")))
        except KeyError as exc:
            raise _not_found("MCP server", server_id) from exc
        except McpControlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/mcp/servers/{server_id}/test")
    async def test_mcp_server(server_id: str, request: Request) -> dict[str, Any]:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            server, details = await manager.test(server_id)
            return {"server": _mcp_view(server), "details": details}
        except KeyError as exc:
            raise _not_found("MCP server", server_id) from exc
        except McpControlError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/mcp/servers/{server_id}/discover")
    async def discover_mcp_server(server_id: str, request: Request) -> dict[str, Any]:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            return _mcp_view(await manager.discover(server_id))
        except KeyError as exc:
            raise _not_found("MCP server", server_id) from exc
        except McpControlError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post(
        "/mcp/servers/{server_id}/tools/{tool_name}/import",
        response_model=SkillPackageRecord,
        status_code=201,
    )
    async def import_mcp_tool(
        server_id: str,
        tool_name: str,
        payload: McpImportInput,
        request: Request,
    ) -> SkillPackageRecord:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            return manager.import_tool(
                server_id,
                tool_name,
                **payload.model_dump(mode="python"),
            )
        except KeyError as exc:
            raise _not_found("MCP server", server_id) from exc
        except (McpControlError, SkillImportError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/tools", response_model=list[ToolRegistrationRecord])
    async def list_native_tools(request: Request) -> list[ToolRegistrationRecord]:
        manager: McpServerManager = request.app.state.mcp_servers
        return manager.list_tool_registrations()

    @router.post(
        "/mcp/servers/{server_id}/tool-registrations",
        response_model=ToolRegistrationRecord,
        status_code=201,
    )
    async def register_native_mcp_tool(
        server_id: str,
        payload: McpToolRegistrationInput,
        request: Request,
    ) -> ToolRegistrationRecord:
        manager: McpServerManager = request.app.state.mcp_servers
        try:
            return manager.register_tool(server_id, **payload.model_dump(mode="python"))
        except KeyError as exc:
            raise _not_found("MCP server", server_id) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/candidates", response_model=list[CatalogCandidateRecord])
    async def list_candidates(request: Request) -> list[CatalogCandidateRecord]:
        manager: CatalogManager = request.app.state.catalog_manager
        return manager.list_candidates()

    @router.post("/candidates", response_model=CatalogCandidateRecord, status_code=201)
    async def create_candidate(payload: CandidateInput, request: Request) -> CatalogCandidateRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.create_candidate(
                title=payload.title,
                description=payload.description,
                agent_draft_ids=tuple(payload.agent_draft_ids),
                skill_package_ids=tuple(payload.skill_package_ids),
                tool_registration_ids=tuple(payload.tool_registration_ids),
                solution_release_ids=tuple(payload.solution_release_ids),
                default_agent_id=payload.default_agent_id,
            )
        except CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/validate", response_model=CatalogCandidateRecord)
    async def validate_candidate(candidate_id: str, request: Request) -> CatalogCandidateRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            await manager.refresh_candidate_mcp(candidate_id)
            return manager.validate(candidate_id)
        except KeyError as exc:
            raise _not_found("Candidate", candidate_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/publish", response_model=GenerationRecord)
    async def publish_candidate(
        candidate_id: str,
        request: Request,
    ) -> GenerationRecord:
        settings = request.app.state.control_settings
        if settings.environment != "development":
            raise HTTPException(status_code=403, detail="Quick publish requires development")
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            await manager.refresh_candidate_mcp(candidate_id)
            return manager.publish_development(
                candidate_id, environment=settings.environment
            )
        except KeyError as exc:
            raise _not_found("Candidate", candidate_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/submit", response_model=ApprovalRecord)
    async def submit_candidate(candidate_id: str, request: Request) -> ApprovalRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.submit_approval(candidate_id)
        except KeyError as exc:
            raise _not_found("Candidate", candidate_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/activate", response_model=GenerationRecord)
    async def activate_candidate(candidate_id: str, request: Request) -> GenerationRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            await manager.refresh_candidate_mcp(candidate_id)
            return manager.activate(candidate_id)
        except KeyError as exc:
            raise _not_found("Candidate", candidate_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/approvals", response_model=list[ApprovalRecord])
    async def list_approvals(request: Request) -> list[ApprovalRecord]:
        manager: CatalogManager = request.app.state.catalog_manager
        return manager.list_approvals()

    @router.post("/approvals/{approval_id}/approve", response_model=ApprovalRecord)
    async def approve(
        approval_id: str,
        payload: ApprovalDecisionInput,
        request: Request,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> ApprovalRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.decide_approval(
                approval_id,
                approve=True,
                actor_fingerprint=actor,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise _not_found("Approval", approval_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/approvals/{approval_id}/reject", response_model=ApprovalRecord)
    async def reject(
        approval_id: str,
        payload: ApprovalDecisionInput,
        request: Request,
        actor: Annotated[str, Depends(_admin_actor)],
    ) -> ApprovalRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.decide_approval(
                approval_id,
                approve=False,
                actor_fingerprint=actor,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise _not_found("Approval", approval_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/generations", response_model=list[GenerationRecord])
    async def list_generations(request: Request) -> list[GenerationRecord]:
        manager: CatalogManager = request.app.state.catalog_manager
        return manager.list_generations()

    @router.get("/generations/{generation_id}/diff")
    async def generation_diff(generation_id: str, request: Request) -> dict[str, Any]:
        state = request.app.state.control_store.snapshot()
        generation = state.generations.get(generation_id)
        if generation is None:
            raise _not_found("Generation", generation_id)
        parent = state.generations.get(generation.parent_generation_id or "")
        old_skills = parent.skill_versions if parent else {}
        old_agents = parent.agent_definitions if parent else {}
        return {
            "generation_id": generation_id,
            "parent_generation_id": generation.parent_generation_id,
            "skills": _mapping_diff(old_skills, generation.skill_versions),
            "agents": _mapping_diff(old_agents, generation.agent_definitions),
            "tools": _mapping_diff(
                {key: tool.content_digest for key, tool in parent.tool_definitions.items()}
                if parent
                else {},
                {key: tool.content_digest for key, tool in generation.tool_definitions.items()},
            ),
            "solution_releases": _mapping_diff(
                parent.solution_releases if parent else {}, generation.solution_releases
            ),
        }

    @router.post("/generations/{generation_id}/rollback-request", response_model=ApprovalRecord)
    async def request_rollback(generation_id: str, request: Request) -> ApprovalRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.request_rollback(generation_id)
        except KeyError as exc:
            raise _not_found("Generation", generation_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/approvals/{approval_id}/rollback", response_model=GenerationRecord)
    async def execute_rollback(approval_id: str, request: Request) -> GenerationRecord:
        manager: CatalogManager = request.app.state.catalog_manager
        try:
            return manager.rollback(approval_id)
        except KeyError as exc:
            raise _not_found("Approval", approval_id) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


def _mapping_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_keys = set(before)
    after_keys = set(after)
    return {
        "added": sorted(after_keys - before_keys),
        "removed": sorted(before_keys - after_keys),
        "changed": sorted(key for key in before_keys & after_keys if before[key] != after[key]),
    }
