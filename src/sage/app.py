"""FastAPI application factory."""

import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import uuid4

import boto3
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from starlette.middleware.cors import CORSMiddleware

from sage.adapters import HttpRunnerClient, RunnerAdapter
from sage.adapters.runner import RunnerClient
from sage.admin_api import create_admin_router
from sage.agent_definitions import default_diagnostic_agent
from sage.agent_drafts import AgentDraftManager, agent_to_dict
from sage.agent_invocations import AgentTask
from sage.agent_runtime import DeepAgentRuntime
from sage.api_models import (
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentModelConfiguration,
    AgentStatus,
    AgentSummary,
    AttachmentResponse,
    DurableInvocationInput,
    DurableInvocationResponse,
    HumanTaskDecisionInput,
    InvocationRequest,
    InvocationResult,
    MessageRequest,
    MessageResponse,
    SkillSummary,
    SolutionSummary,
    UsageInfo,
    UserMemoryInput,
)
from sage.artifacts import S3ArtifactStore
from sage.attachments import AttachmentStore
from sage.audit import SqliteAuditStore
from sage.authentication import (
    AnonymousPrincipalResolver,
    AuthenticationError,
    EntraPrincipalResolver,
    PrincipalResolver,
)
from sage.capabilities import CapabilityView, CapabilityViewResolver
from sage.catalog import CatalogManager, initial_generation
from sage.control_settings import ControlPlaneSettings, load_control_plane_settings
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest, file_digest
from sage.discovery import (
    CapabilityDescriptorView,
    accessible_agent_capability_ids,
    agent_is_discoverable,
    build_capability_descriptors,
)
from sage.domain import Principal, RuntimeContext
from sage.durable import DurableExecutionPort, DurableRunRequest, TemporalExecutionAdapter
from sage.errors import (
    AttachmentNotFoundError,
    AttachmentRejectedError,
    ExecutionError,
    PolicyDeniedError,
    TargetNotFoundError,
)
from sage.evaluations import EvaluationManager
from sage.feedback import (
    FeedbackConflictError,
    FeedbackInput,
    FeedbackMemoryConfirmation,
    FeedbackRecord,
    FeedbackTargetError,
    SqliteFeedbackStore,
    new_feedback_record,
    new_memory_confirmation,
    validate_feedback_targets,
)
from sage.gateways import HttpEndpointRegistry, McpGatewayClient
from sage.human_tasks import HumanTaskError, HumanTaskRecord, SqliteHumanTaskStore
from sage.invocations import (
    InvocationConflictError,
    InvocationInProgressError,
    SqliteInvocationStore,
)
from sage.mcp_control import (
    ManagedMcpGatewayClient,
    McpConnector,
    McpServerManager,
    OfficialMcpConnector,
)
from sage.mcp_testing import McpToolTests
from sage.memory import MemoryRecord, SqliteMemoryStore
from sage.operations import (
    InteractiveOperationService,
    OperationError,
    OperationHandle,
    OperationRequest,
)
from sage.plans import PlanRecord, SqlitePlanStore
from sage.quality_evaluations import SqliteQualityEvaluationStore
from sage.reference_solutions.contract_review import review_contract
from sage.reference_solutions.enterprise_qa import answer_enterprise_question
from sage.reference_solutions.incident_diagnostics import (
    build_incident_route_choices,
    report_summary,
    require_nonempty_iis_metrics,
)
from sage.reference_solutions.news_intelligence import analyze_news
from sage.result_validation import ResultValidatorRegistry
from sage.results import (
    ResultUnavailableError,
    ResultView,
    build_result_view,
    build_result_view_from_output,
    result_available_data,
)
from sage.route_catalog import RouteChoice, build_route_catalog
from sage.runs import RunEvent, RunSnapshot, SqliteRunStore
from sage.runtime_manager import DynamicMessageService, RuntimeManager
from sage.scanning import build_content_scanner
from sage.settings import DEFAULT_ENV_FILE, AgentModelSettings
from sage.skill_orchestration import SkillOrchestrator
from sage.skill_packages import SkillPackageManager
from sage.solution_assist import SolutionAssistant
from sage.solution_packages import SolutionBundlePackageManager
from sage.solutions import SolutionManager
from sage.supply_chain import SolutionTrustStore
from sage.telemetry import TelemetryRuntime, configure_telemetry, record_http_result
from sage.ui_schema import SolutionUiSchema, load_solution_ui_schema
from sage.workflows import load_workflow_template

DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
STATIC_ROOT = Path(__file__).resolve().parent / "static"


def _request_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else Principal.anonymous()


def _run_event_category(name: str) -> str:
    prefix = name.partition(".")[0]
    if prefix in {"run", "workflow", "human_task"}:
        return "lifecycle"
    if prefix in {"agent", "model", "plan", "subagent"}:
        return "analysis"
    if prefix in {"skill", "tool"}:
        return "execution"
    if prefix in {"artifact", "citation", "assessment"}:
        return "evidence"
    if prefix == "result":
        return "result"
    return "progress"


def _run_event_payload(run_id: str, event: RunEvent) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": f"{run_id}:{event.sequence}",
        "sequence": event.sequence,
        "run_id": run_id,
        "type": event.name,
        "name": event.name,
        "category": _run_event_category(event.name),
        "occurred_at": event.created_at.isoformat(),
        "created_at": event.created_at.isoformat(),
        "visibility": "public",
        "data": event.data,
        "links": {},
    }


def _request_snapshot(
    request: Request,
    solution_id: str | None,
    *,
    required_profile: str | None = None,
):
    manager: RuntimeManager = request.app.state.runtime_manager
    try:
        return manager.resolve_active_solution(
            solution_id,
            _request_principal(request),
            required_profile=required_profile,
        )
    except TargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PolicyDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _request_child_snapshot(
    request: Request,
    parent_run_id: str,
    *,
    target_agent_id: str | None = None,
):
    manager: RuntimeManager = request.app.state.runtime_manager
    try:
        return await manager.resolve_child(
            parent_run_id,
            _request_principal(request),
            target_agent_id=target_agent_id,
        )
    except TargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PolicyDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _request_capabilities(
    request: Request,
    solution_id: str | None = None,
) -> CapabilityView:
    active = _request_snapshot(request, solution_id)
    resolver: CapabilityViewResolver = active.capabilities
    context = RuntimeContext.create(
        mode="direct",
        principal=_request_principal(request),
        generation_id=active.generation.generation_id,
        solution_id=active.solution_id,
        solution_release_id=(
            active.solution_release.release_id if active.solution_release is not None else None
        ),
    )
    return resolver.resolve(context)


def create_app(
    skills_root: Path | None = None,
    *,
    agent_model: BaseChatModel | None = None,
    agent_model_name: str | None = None,
    agent_settings: AgentModelSettings | None = None,
    mcp_gateway: McpGatewayClient | None = None,
    mcp_connector: McpConnector | None = None,
    http_endpoints: HttpEndpointRegistry | None = None,
    control_settings: ControlPlaneSettings | None = None,
    principal_resolver: PrincipalResolver | None = None,
    durable_execution: DurableExecutionPort | None = None,
    telemetry_runtime: TelemetryRuntime | None = None,
    runner_client: RunnerClient | None = None,
) -> FastAPI:
    configured_settings = control_settings or load_control_plane_settings(DEFAULT_ENV_FILE)
    telemetry = telemetry_runtime or configure_telemetry(
        service_name=configured_settings.otel_service_name,
        endpoint=configured_settings.otel_exporter_otlp_endpoint,
        insecure=configured_settings.otel_exporter_otlp_insecure,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with TemporaryDirectory(prefix="sage-attachments-") as temporary_root:
            resolved_control_settings = configured_settings
            root = skills_root or resolved_control_settings.skills_root
            resolved_principal_resolver = principal_resolver
            if resolved_principal_resolver is None:
                if resolved_control_settings.authentication_mode == "entra-id":
                    resolved_principal_resolver = EntraPrincipalResolver(
                        tenant_id=resolved_control_settings.entra_tenant_id or "",
                        audience=resolved_control_settings.entra_audience or "",
                    )
                else:
                    resolved_principal_resolver = AnonymousPrincipalResolver()
            resolved_durable_execution = durable_execution
            if (
                resolved_durable_execution is None
                and resolved_control_settings.temporal_target is not None
            ):
                resolved_durable_execution = await TemporalExecutionAdapter.connect(
                    resolved_control_settings.temporal_target,
                    namespace=resolved_control_settings.temporal_namespace,
                    api_key=resolved_control_settings.temporal_api_key,
                    tls=resolved_control_settings.temporal_tls,
                )
            persistent_control = skills_root is None or control_settings is not None
            if not persistent_control:
                resolved_control_settings = resolved_control_settings.model_copy(
                    update={"data_root": Path(temporary_root) / "control"}
                )
            initial_root = root.resolve()
            state_path = resolved_control_settings.data_root / "control-plane.json"
            state_exists = state_path.is_file()
            if persistent_control and not state_exists:
                initial_root = (
                    resolved_control_settings.data_root / "generations" / "gen_startup" / "skills"
                )
                if not initial_root.is_dir():
                    initial_root.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(root, initial_root)
            initial = None
            if not state_exists:
                default_agent = default_diagnostic_agent()
                initial = initial_generation(
                    initial_root,
                    {default_agent.agent_id: agent_to_dict(default_agent)},
                    default_agent.agent_id,
                )
            control_store = LocalControlStore(resolved_control_settings.data_root, initial)
            content_scanner = build_content_scanner(
                host=resolved_control_settings.clamav_host,
                port=resolved_control_settings.clamav_port,
                timeout_seconds=resolved_control_settings.clamav_timeout_seconds,
                required=resolved_control_settings.content_scanning_required,
            )
            if resolved_control_settings.artifact_backend == "s3":
                artifact_cache_root = Path(temporary_root) / "artifact-cache"
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=resolved_control_settings.s3_endpoint_url,
                    region_name=resolved_control_settings.s3_region,
                )
                s3_client.head_bucket(Bucket=resolved_control_settings.s3_bucket or "")
                artifact_backend = S3ArtifactStore(
                    s3_client,
                    bucket=resolved_control_settings.s3_bucket or "",
                    prefix=resolved_control_settings.s3_prefix,
                    cache_root=artifact_cache_root,
                )
                attachments = AttachmentStore(
                    None,
                    artifact_backend=artifact_backend,
                    content_scanner=content_scanner,
                )
                runner_input_roots = (artifact_cache_root,)
            else:
                runtime_artifact_root = control_store.artifacts_root / "runtime"
                attachments = AttachmentStore(
                    runtime_artifact_root,
                    content_scanner=content_scanner,
                )
                runner_input_roots = (runtime_artifact_root,)
            runs = SqliteRunStore(str(control_store.root / "runs.sqlite3"))
            memories = SqliteMemoryStore(str(control_store.root / "memory.sqlite3"))
            plans = SqlitePlanStore(str(control_store.root / "plans.sqlite3"))
            invocations = SqliteInvocationStore(str(control_store.root / "invocations.sqlite3"))
            feedback = SqliteFeedbackStore(str(control_store.root / "feedback.sqlite3"))
            quality_evaluations = SqliteQualityEvaluationStore(
                str(control_store.root / "quality-evaluations.sqlite3")
            )
            human_tasks = SqliteHumanTaskStore(str(control_store.root / "human-tasks.sqlite3"))
            audit_store = SqliteAuditStore(str(control_store.root / "audit.sqlite3"))
            runner_enabled = (
                runner_client is not None or resolved_control_settings.runner_url is not None
            )
            skill_packages = SkillPackageManager(
                control_store,
                content_scanner,
                python_runner_enabled=runner_enabled,
            )
            agent_drafts = AgentDraftManager(control_store)
            solution_manager = SolutionManager(control_store)
            solution_bundle_packages = SolutionBundlePackageManager(
                control_store,
                content_scanner,
                trust_store=SolutionTrustStore(resolved_control_settings.solution_trust_store),
                signature_required=resolved_control_settings.signed_solutions_required,
            )
            evaluation_manager = EvaluationManager(control_store)
            active_record = control_store.snapshot().generations[
                control_store.snapshot().active_generation_id
            ]
            agent_drafts.ensure_published(active_record.agent_definitions)
            connector = mcp_connector or OfficialMcpConnector(resolved_control_settings)
            mcp_servers = McpServerManager(
                control_store,
                resolved_control_settings,
                connector,
                skill_packages,
            )
            managed_mcp_gateway = mcp_gateway or ManagedMcpGatewayClient(
                mcp_servers,
                connector,
            )
            resolved_runner_client = runner_client or (
                HttpRunnerClient(
                    resolved_control_settings.runner_url,
                    token=resolved_control_settings.runner_token or "",
                )
                if resolved_control_settings.runner_url is not None
                else None
            )
            python_adapter = (
                RunnerAdapter(
                    resolved_runner_client,
                    allowed_input_roots=runner_input_roots,
                    profile=resolved_control_settings.runner_profile,
                )
                if resolved_runner_client is not None
                else None
            )
            checkpoint_context = None
            checkpointer = None
            if resolved_control_settings.checkpoint_database_url:
                checkpoint_context = AsyncPostgresSaver.from_conn_string(
                    resolved_control_settings.checkpoint_database_url
                )
                checkpointer = await checkpoint_context.__aenter__()
                try:
                    await checkpointer.setup()
                except BaseException:
                    await checkpoint_context.__aexit__(None, None, None)
                    raise
            try:
                runtime_manager = RuntimeManager(
                    control_store,
                    attachments,
                    runs,
                    agent_model=agent_model,
                    agent_model_name=agent_model_name,
                    agent_settings=agent_settings,
                    mcp_gateway=managed_mcp_gateway,
                    http_endpoints=http_endpoints,
                    memory_store=memories,
                    plan_store=plans,
                    builtin_handlers={
                        "report-summary": report_summary,
                        "enterprise-qa": answer_enterprise_question,
                        "news-intelligence": analyze_news,
                        "contract-review": review_contract,
                    },
                    result_validators=ResultValidatorRegistry(
                        {"iis-w3c-nonempty": require_nonempty_iis_metrics}
                    ),
                    checkpointer=checkpointer,
                    invocation_store=invocations,
                    python_adapter=python_adapter,
                    allow_local_subprocess=resolved_control_settings.local_subprocess_enabled,
                )
                catalog_manager = CatalogManager(
                    control_store,
                    agent_drafts,
                    skill_packages,
                    mcp_servers,
                    runtime_manager,
                    evaluation_gate_mode=resolved_control_settings.evaluation_gate_mode,
                )
                operation_service = InteractiveOperationService(
                    runtime_manager,
                    runs,
                    invocations,
                )
            except BaseException:
                if checkpoint_context is not None:
                    await checkpoint_context.__aexit__(None, None, None)
                raise
            active = runtime_manager.active
            app.state.control_settings = resolved_control_settings
            app.state.principal_resolver = resolved_principal_resolver
            app.state.durable_execution = resolved_durable_execution
            app.state.control_store = control_store
            app.state.attachments = attachments
            app.state.run_store = runs
            app.state.memory_store = memories
            app.state.plan_store = plans
            app.state.invocation_store = invocations
            app.state.feedback_store = feedback
            app.state.quality_evaluation_store = quality_evaluations
            app.state.human_task_store = human_tasks
            app.state.audit_store = audit_store
            app.state.skill_packages = skill_packages
            app.state.agent_drafts = agent_drafts
            app.state.solution_manager = solution_manager
            app.state.solution_assistant = SolutionAssistant(agent_model, agent_settings)
            app.state.solution_bundle_packages = solution_bundle_packages
            app.state.evaluation_manager = evaluation_manager
            app.state.mcp_servers = mcp_servers
            app.state.mcp_tool_tests = McpToolTests(
                control_store,
                mcp_servers,
                connector,
                human_tasks,
                audit_store,
                resolved_durable_execution,
                resolved_control_settings.temporal_task_queue,
            )
            app.state.skill_orchestrator = SkillOrchestrator(
                control_store,
                runtime_manager,
                runs,
                invocations,
                resolved_durable_execution,
                resolved_control_settings.temporal_task_queue,
                human_tasks,
            )
            app.state.runtime_manager = runtime_manager
            app.state.catalog_manager = catalog_manager
            app.state.registry = active.registry
            app.state.agent_catalog = active.agent_catalog
            app.state.agent_runtimes = active.agent_runtimes
            app.state.agent_runtime = active.agent_runtimes.get()
            app.state.capabilities = active.capabilities
            app.state.message_service = DynamicMessageService(runtime_manager)
            app.state.operation_service = operation_service
            app.state.runner_client = resolved_runner_client
            try:
                yield
            finally:
                await operation_service.close()
                if checkpoint_context is not None:
                    await checkpoint_context.__aexit__(None, None, None)
                telemetry.shutdown()

    app = FastAPI(title="SAGE Skills Gateway", version="0.1.0", lifespan=lifespan)
    if configured_settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured_settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
            ],
        )
    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    @app.middleware("http")
    async def resolve_principal(request: Request, call_next):
        with telemetry.tracer.start_as_current_span("HTTP request") as span:
            span.set_attribute("http.request.method", request.method)
            path = request.url.path
            public_control_path = path.startswith(("/v1/control/bootstrap", "/v1/auth/config"))
            admin_auth_disabled = (
                path.startswith("/v1/admin") and not configured_settings.admin_auth_enabled
            )
            admin_with_local_token = (
                path.startswith("/v1/admin")
                and configured_settings.admin_auth_enabled
                and configured_settings.authentication_mode != "entra-id"
            )
            requires_principal = (
                path.startswith("/v1/")
                and not public_control_path
                and not admin_auth_disabled
                and not admin_with_local_token
            )
            if requires_principal:
                resolver: PrincipalResolver = request.app.state.principal_resolver
                try:
                    request.state.principal = resolver.resolve(request.headers.get("Authorization"))
                except AuthenticationError as exc:
                    response = JSONResponse(status_code=401, content={"detail": str(exc)})
                    _record_audit(request, response.status_code)
                    record_http_result(
                        span,
                        route=f"{request.method} {path}",
                        status_code=response.status_code,
                    )
                    _apply_security_headers(response, configured_settings)
                    return response
            try:
                response = await call_next(request)
            except BaseException:
                _record_audit(request, 500)
                record_http_result(
                    span,
                    route=f"{request.method} {path}",
                    status_code=500,
                )
                raise
            _record_audit(request, response.status_code)
            route_object = request.scope.get("route")
            route_path = getattr(route_object, "path", path)
            record_http_result(
                span,
                route=f"{request.method} {route_path}",
                status_code=response.status_code,
            )
            _apply_security_headers(response, configured_settings)
            return response

    def _apply_security_headers(
        response: Response,
        settings: ControlPlaneSettings,
    ) -> None:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' https://login.microsoftonline.com; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self' https://login.microsoftonline.com",
        )
        if settings.environment == "production":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

    def _record_audit(request: Request, status_code: int) -> None:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if not request.url.path.startswith("/v1/"):
            return
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal):
            actor_id = principal.principal_id
            workspace_id = principal.workspace_id
        else:
            admin_actor = getattr(request.state, "admin_actor", None)
            _, _, token = (request.headers.get("Authorization") or "").partition(" ")
            actor_id = (
                str(admin_actor)
                if admin_actor
                else (
                    f"admin:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
                    if token
                    else "anonymous"
                )
            )
            workspace_id = "control-plane"
        outcome = "success" if status_code < 400 else "denied" if status_code < 500 else "failed"
        request.app.state.audit_store.append(
            actor_id=actor_id,
            workspace_id=workspace_id,
            action=request.method,
            resource=request.url.path,
            outcome=outcome,
            status_code=status_code,
            details_digest=canonical_digest(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                }
            ),
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> dict[str, int | str | bool]:
        active = request.app.state.runtime_manager.active
        runner = request.app.state.runner_client
        if runner is not None:
            try:
                runner_health = await runner.health()
            except ExecutionError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if runner_health.profile != request.app.state.control_settings.runner_profile:
                raise HTTPException(status_code=503, detail="Runner profile mismatch")
            runner_status = "ready"
        else:
            runner_status = (
                "local-subprocess"
                if request.app.state.control_settings.local_subprocess_enabled
                else "disabled"
            )
        return {
            "status": "ready",
            "skills": len(active.registry),
            "solutions": len(active.generation.solution_releases),
            "generation_id": active.generation.generation_id,
            "artifact_backend": request.app.state.control_settings.artifact_backend,
            "durable": request.app.state.durable_execution is not None,
            "persistent_checkpoints": bool(
                request.app.state.control_settings.checkpoint_database_url
            ),
            "runner": runner_status,
        }

    @app.get("/v1/control/bootstrap")
    async def control_bootstrap(
        request: Request,
        response: Response,
    ) -> dict[str, str | bool | None]:
        settings: ControlPlaneSettings = request.app.state.control_settings
        response.headers["Cache-Control"] = "no-store"
        return {
            "environment": settings.environment,
            "admin_auth_enabled": settings.admin_auth_enabled,
            "auto_admin": settings.auto_admin_enabled,
            "admin_token": settings.admin_token if settings.auto_admin_enabled else None,
        }

    @app.get("/v1/auth/config")
    async def authentication_config(request: Request) -> dict[str, str | None]:
        settings: ControlPlaneSettings = request.app.state.control_settings
        client_id = settings.entra_client_id or settings.entra_audience
        scope = settings.entra_scope
        if scope is None and settings.entra_audience:
            scope = f"api://{settings.entra_audience}/access_as_user"
        return {
            "mode": settings.authentication_mode,
            "tenant_id": (
                settings.entra_tenant_id if settings.authentication_mode == "entra-id" else None
            ),
            "client_id": client_id if settings.authentication_mode == "entra-id" else None,
            "scope": scope if settings.authentication_mode == "entra-id" else None,
        }

    @app.get("/v1/skills", response_model=list[SkillSummary])
    async def list_skills(
        request: Request,
        solution_id: str | None = None,
    ) -> list[SkillSummary]:
        capabilities = _request_capabilities(request, solution_id)
        return [
            SkillSummary(
                name=definition.name,
                description=definition.description,
                version=definition.version,
                risk=definition.manifest.spec.policy.risk,
            )
            for definition in capabilities.skills
        ]

    def client_capabilities(
        request: Request,
        solution_id: str | None,
    ) -> list[CapabilityDescriptorView]:
        snapshot = _request_snapshot(request, solution_id)
        capabilities = _request_capabilities(request, solution_id)
        return list(
            build_capability_descriptors(
                generation=snapshot.generation,
                release=snapshot.solution_release,
                capabilities=capabilities,
                agents=snapshot.agent_catalog,
                durable_available=request.app.state.durable_execution is not None,
            )
        )

    @app.get("/v1/capabilities", response_model=list[CapabilityDescriptorView])
    async def list_client_capabilities(
        request: Request,
        solution_id: str | None = None,
    ) -> list[CapabilityDescriptorView]:
        return client_capabilities(request, solution_id)

    @app.get(
        "/v1/solutions/{solution_id}/capabilities",
        response_model=list[CapabilityDescriptorView],
    )
    async def list_solution_capabilities(
        solution_id: str,
        request: Request,
    ) -> list[CapabilityDescriptorView]:
        return client_capabilities(request, solution_id)

    @app.get("/v1/routes", response_model=list[RouteChoice])
    async def list_routes(
        request: Request,
        solution_id: str | None = None,
    ) -> list[RouteChoice]:
        active = _request_snapshot(request, solution_id)
        capabilities = _request_capabilities(request, solution_id)
        routes = build_route_catalog(capabilities.skills)
        if capabilities.get_skill("skills-collection") is not None:
            routes.extend(build_incident_route_choices(Path(active.generation.skills_root)))
        return routes

    @app.get("/v1/solutions", response_model=list[SolutionSummary])
    async def list_solutions(request: Request) -> list[SolutionSummary]:
        state = request.app.state.control_store.snapshot()
        generation = state.generations[state.active_generation_id]
        principal = _request_principal(request)
        result: list[SolutionSummary] = []
        for solution_id, release_id in sorted(generation.solution_releases.items()):
            solution = state.solutions.get(solution_id)
            release = state.solution_releases.get(release_id)
            if solution is None or release is None:
                continue
            if not principal.roles.intersection(release.allowed_roles):
                continue
            result.append(
                SolutionSummary(
                    solution_id=solution.solution_id,
                    title=solution.title,
                    description=solution.description,
                    release_id=release.release_id,
                    version=release.version,
                    entry_agent_id=release.entry_agent_id,
                    supported_profiles=list(release.supported_profiles),
                )
            )
        return result

    @app.get("/v1/solutions/{solution_id}/ui-schema", response_model=SolutionUiSchema)
    async def get_solution_ui_schema(
        solution_id: str,
        request: Request,
    ) -> SolutionUiSchema:
        snapshot = _request_snapshot(request, solution_id)
        release = snapshot.solution_release
        if release is None or release.package_relpath is None:
            raise HTTPException(status_code=404, detail="Solution UI Schema not found")
        components = [
            component for component in release.components if component.kind == "ui-schema"
        ]
        if len(components) != 1:
            raise HTTPException(status_code=404, detail="Solution UI Schema not found")
        component = components[0]
        relative_path = release.component_paths.get(f"ui-schema:{component.component_id}")
        if relative_path is None:
            raise HTTPException(status_code=404, detail="Solution UI Schema not found")
        package_root = request.app.state.control_store.resolve_path(release.package_relpath)
        schema_path = (package_root / relative_path).resolve()
        if not schema_path.is_relative_to(package_root.resolve()) or not schema_path.is_file():
            raise HTTPException(status_code=409, detail="Solution UI Schema is unavailable")
        if file_digest(schema_path) != component.content_digest:
            raise HTTPException(status_code=409, detail="Solution UI Schema digest mismatch")
        try:
            schema = load_solution_ui_schema(schema_path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="Solution UI Schema is invalid") from exc
        if schema.solution_id != solution_id:
            raise HTTPException(status_code=409, detail="Solution UI Schema id mismatch")
        return schema

    @app.get("/v1/agent", response_model=AgentStatus)
    async def agent_status(
        request: Request,
        solution_id: str | None = None,
    ) -> AgentStatus:
        snapshot = _request_snapshot(request, solution_id)
        capabilities = _request_capabilities(request, solution_id)
        try:
            runtime: DeepAgentRuntime = snapshot.agent_runtimes.get()
        except TargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        settings = runtime.settings
        definition = runtime.definition
        if not agent_is_discoverable(definition, capabilities):
            raise HTTPException(status_code=404, detail="Agent not found")
        return AgentStatus(
            agent_id=definition.agent_id,
            version=definition.version,
            runtime="deepagents",
            model=runtime.model_name,
            subagents=list(definition.worker_names),
            capabilities=list(accessible_agent_capability_ids(definition, capabilities)),
            memory=definition.memory_policy,
            configuration=AgentModelConfiguration(
                env_file_present=settings.env_file_present,
                authentication=settings.authentication_label(),
                token_scope=(
                    settings.azure_openai_token_scope
                    if settings.authentication_label() == "entra-id"
                    else None
                ),
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                timeout_seconds=settings.timeout_seconds,
                max_retries=settings.max_retries,
                recursion_limit=settings.recursion_limit,
            ),
        )

    @app.get("/v1/agents", response_model=list[AgentSummary])
    async def list_agents(
        request: Request,
        solution_id: str | None = None,
    ) -> list[AgentSummary]:
        catalog = _request_snapshot(request, solution_id).agent_catalog
        capabilities = _request_capabilities(request, solution_id)
        return [
            AgentSummary(
                agent_id=definition.agent_id,
                name=definition.name,
                version=definition.version,
                subagents=list(definition.worker_names),
                capabilities=list(accessible_agent_capability_ids(definition, capabilities)),
                memory=definition.memory_policy,
                max_steps=definition.max_steps,
                max_model_calls=definition.max_model_calls,
                max_tool_calls=definition.max_tool_calls,
                max_subagent_calls=definition.max_subagent_calls,
            )
            for definition in catalog
            if agent_is_discoverable(definition, capabilities)
        ]

    @app.post("/v1/messages", response_model=MessageResponse)
    async def post_message(payload: MessageRequest, request: Request) -> MessageResponse:
        service: DynamicMessageService = request.app.state.message_service
        try:
            return await service.run(payload, principal=_request_principal(request))
        except TargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/v1/operations",
        response_model=OperationHandle,
        status_code=202,
    )
    async def start_operation(
        payload: OperationRequest,
        request: Request,
        response: Response,
    ) -> OperationHandle:
        idempotency_key = (request.headers.get("idempotency-key") or "").strip()
        if not idempotency_key or len(idempotency_key) > 255:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key is required and must not exceed 255 characters",
            )
        try:
            handle, created = await request.app.state.operation_service.start(
                payload,
                _request_principal(request),
                idempotency_key=idempotency_key,
            )
        except TargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except InvocationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OperationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return handle

    @app.post("/v1/invocations", response_model=InvocationResult)
    async def invoke_capability(
        payload: InvocationRequest,
        request: Request,
    ) -> InvocationResult | JSONResponse:
        principal = _request_principal(request)
        native_tool = payload.capability_id.startswith("tool:mcp:")
        if not native_tool and not payload.capability_id.startswith("skill:"):
            raise HTTPException(
                status_code=400, detail="Only skill and native MCP tool capabilities are supported"
            )
        if native_tool and payload.artifact_ids:
            raise HTTPException(
                status_code=422,
                detail="Native tools require explicit schema arguments, not implicit artifacts",
            )
        parent_context: RuntimeContext | None = None
        if payload.parent_run_id is None:
            snapshot = _request_snapshot(request, payload.solution_id, required_profile="direct")
            execution_principal = principal
        else:
            snapshot, parent_context = await _request_child_snapshot(
                request,
                payload.parent_run_id,
            )
            if payload.solution_id not in {None, parent_context.solution_id}:
                raise HTTPException(
                    status_code=400,
                    detail="Invocation Solution does not match the Parent Run",
                )
            execution_principal = parent_context.principal
        capability_name = payload.capability_id.removeprefix("skill:")
        if native_tool and parent_context is None:
            tool = snapshot.generation.tool_definitions.get(payload.capability_id)
            if tool is not None and tool.risk in {"write-high", "destructive"}:
                result = await request.app.state.skill_orchestrator.start_tool(
                    payload,
                    snapshot,
                    execution_principal,
                )
                return JSONResponse(status_code=202, content=result.model_dump(mode="json"))
        if parent_context and parent_context.skill_id:
            parent_skill = snapshot.registry.get(parent_context.skill_id)
            if parent_skill is None or payload.capability_id not in (
                parent_skill.manifest.spec.execution.capability_ids
            ):
                raise HTTPException(
                    status_code=403, detail="Capability is not declared by the Skill"
                )
        if parent_context and parent_context.lifecycle == "durable" and (
            native_tool or (not payload.artifact_ids and payload.approval_task_id is None)
        ):
            view = snapshot.capabilities.resolve(parent_context)
            tool = (next(
                (item for item in view.tools if item.tool_id == payload.capability_id), None
            ) if native_tool else view.get_skill(capability_name))
            executor = snapshot.tool_executor if native_tool else snapshot.executor
            if tool is None or executor is None:
                raise HTTPException(status_code=404, detail="Capability unavailable")
            if parent_context.agent_id and payload.capability_id not in (
                snapshot.agent_catalog.get(parent_context.agent_id).capability_ids
            ):
                raise HTTPException(status_code=403, detail="Tool not allowed for parent Agent")
            if not payload.idempotency_key:
                raise HTTPException(status_code=422, detail="Durable Tool requires idempotency_key")
            approvals = request.app.state.skill_orchestrator.approvals
            try:
                task = approvals.prepare(
                    parent_context, tool, payload.input, payload.idempotency_key
                )
                if task and task.status == "pending":
                    result = InvocationResult(
                        status="awaiting-approval",
                        invocation_id=payload.idempotency_key,
                        run_id=parent_context.run_id,
                        solution_id=parent_context.solution_id,
                        solution_release_id=parent_context.solution_release_id,
                        capability_id=payload.capability_id,
                        output={},
                        usage=UsageInfo(),
                        approval_task_id=task.task_id,
                    )
                    return JSONResponse(status_code=202, content=result.model_dump(mode="json"))
                output = await approvals.dispatch(
                    executor,
                    parent_context,
                    tool,
                    payload.input,
                    payload.idempotency_key,
                    task,
                )
                return InvocationResult(
                    status="completed",
                    invocation_id=payload.idempotency_key,
                    run_id=parent_context.run_id,
                    solution_id=parent_context.solution_id,
                    solution_release_id=parent_context.solution_release_id,
                    capability_id=payload.capability_id,
                    output=output,
                    usage=UsageInfo(tool_calls=1),
                )
            except PolicyDeniedError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except (InvocationConflictError, InvocationInProgressError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ExecutionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        approved_capability_ids: frozenset[str] = frozenset()
        if payload.approval_task_id is not None:
            if payload.parent_run_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="approval_task_id requires parent_run_id",
                )
            parent_run = await request.app.state.run_store.get(payload.parent_run_id)
            if parent_run is None:
                raise HTTPException(status_code=404, detail="Parent Run not found")
            same_owner = parent_run.context.principal.principal_id == principal.principal_id
            delegated_admin = principal.roles.intersection(
                {"workflow_worker", "platform_admin", "capability_admin"}
            )
            if parent_run.context.principal.workspace_id != principal.workspace_id or (
                not same_owner and not delegated_admin
            ):
                raise HTTPException(status_code=404, detail="Parent Run not found")
            approval = request.app.state.human_task_store.get(payload.approval_task_id)
            invocation_input_digest = canonical_digest(payload.input)
            if (
                approval is None
                or approval.kind != "approval"
                or approval.status != "approved"
                or approval.run_id != parent_run.context.run_id
                or approval.workspace_id != principal.workspace_id
                or approval.solution_id != snapshot.solution_id
                or approval.subject.get("capability_id") != payload.capability_id
                or approval.subject.get("input_digest") != invocation_input_digest
                or (
                    native_tool
                    and (
                        approval.subject.get("generation_id") != snapshot.generation.generation_id
                        or approval.subject.get("tool_digest")
                        != (
                            snapshot.generation.tool_definitions[
                                payload.capability_id
                            ].content_digest
                            if payload.capability_id in snapshot.generation.tool_definitions
                            else None
                        )
                    )
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="A matching approved HumanTask is required",
                )
            approved_capability_ids = frozenset({payload.capability_id})
        request_digest = canonical_digest(
            {
                "workspace_id": execution_principal.workspace_id,
                "principal_id": execution_principal.principal_id,
                "solution_id": snapshot.solution_id,
                "solution_release_id": (
                    snapshot.solution_release.release_id
                    if snapshot.solution_release is not None
                    else None
                ),
                "capability_id": payload.capability_id,
                "input": payload.input,
                **({"generation_id": snapshot.generation.generation_id} if native_tool else {}),
                "artifact_ids": payload.artifact_ids,
                "parent_run_id": payload.parent_run_id,
                "approval_task_id": payload.approval_task_id,
            }
        )
        invocation_id = (
            "inv_"
            + canonical_digest(
                {
                    "workspace_id": execution_principal.workspace_id,
                    "principal_id": execution_principal.principal_id,
                    "solution_id": snapshot.solution_id,
                    "idempotency_key": payload.idempotency_key,
                }
            ).removeprefix("sha256:")[:32]
            if payload.idempotency_key
            else f"inv_{uuid4().hex}"
        )
        try:
            reservation = request.app.state.invocation_store.reserve(
                invocation_id,
                request_digest,
            )
        except (InvocationConflictError, InvocationInProgressError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if reservation.status == "completed" and reservation.result_json is not None:
            return InvocationResult.model_validate_json(reservation.result_json)
        native_dispatched = False
        native_started = False
        native_context = None
        try:
            if native_tool:
                native_input_data = {
                    "kind": "capability",
                    "capability_id": payload.capability_id,
                    "input": payload.input,
                    "artifact_ids": payload.artifact_ids,
                }
                native_context = RuntimeContext.create(
                    mode="direct",
                    principal=execution_principal,
                    generation_id=snapshot.generation.generation_id,
                    solution_id=snapshot.solution_id,
                    solution_release_id=snapshot.solution_release.release_id
                    if snapshot.solution_release
                    else None,
                    parent_run_id=payload.parent_run_id,
                    root_run_id=parent_context.root_run_id if parent_context else None,
                    approved_capability_ids=approved_capability_ids,
                )
                view = snapshot.capabilities.resolve(native_context)
                if snapshot.tool_executor is None or not view.contains(payload.capability_id):
                    raise TargetNotFoundError("Native Tool is not available in this run")
                tool = next(item for item in view.tools if item.tool_id == payload.capability_id)
                if parent_context and parent_context.agent_id:
                    agent = snapshot.agent_catalog.get(parent_context.agent_id)
                    if payload.capability_id not in agent.capability_ids:
                        raise PolicyDeniedError("Native Tool is not allowed for the parent Agent")
                await request.app.state.run_store.begin(
                    native_context,
                    input_data=native_input_data,
                )
                native_started = True
                await request.app.state.run_store.append(
                    native_context.run_id, "tool.started", {"tool": payload.capability_id}
                )
                native_dispatched = tool.risk != "read-only"
                output = await snapshot.tool_executor.execute(
                    payload.capability_id, payload.input, context=native_context
                )
                result = InvocationResult(
                    status="completed",
                    invocation_id=invocation_id,
                    run_id=native_context.run_id,
                    solution_id=native_context.solution_id,
                    solution_release_id=native_context.solution_release_id,
                    capability_id=payload.capability_id,
                    output=output,
                    usage=UsageInfo(tool_calls=1),
                )
                try:
                    view = build_result_view_from_output(
                        native_context,
                        native_input_data,
                        output,
                        capability_id=payload.capability_id,
                        usage={"tool_calls": 1},
                    )
                    await request.app.state.run_store.append(
                        native_context.run_id,
                        "result.available",
                        result_available_data(view),
                    )
                except ResultUnavailableError as exc:
                    await request.app.state.run_store.append(
                        native_context.run_id,
                        "result.projection.failed",
                        {
                            "run_id": native_context.run_id,
                            "code": "result_projection_failed",
                            "message": str(exc),
                        },
                    )
                await request.app.state.run_store.append(
                    native_context.run_id,
                    "run.completed",
                    {
                        "capability_id": payload.capability_id,
                        "output": output,
                        "usage": {"tool_calls": 1},
                    },
                )
                request.app.state.invocation_store.complete(invocation_id, result.model_dump_json())
                return result
            response = await request.app.state.message_service.run(
                MessageRequest(
                    solution_id=snapshot.solution_id,
                    message=f"Invoke {payload.capability_id}",
                    mode="direct",
                    skill=capability_name,
                    arguments=payload.input,
                    artifact_ids=payload.artifact_ids,
                ),
                principal=principal,
                parent_run_id=payload.parent_run_id,
                approved_capability_ids=approved_capability_ids,
            )
            if response.run_id is None or response.result is None:
                raise ExecutionError("Invocation did not produce a result")
            run = await request.app.state.run_store.get(response.run_id)
            if run is None:
                raise ExecutionError("Invocation Run was not recorded")
            result = InvocationResult(
                status="completed",
                invocation_id=invocation_id,
                run_id=response.run_id,
                solution_id=run.context.solution_id,
                solution_release_id=run.context.solution_release_id,
                capability_id=(
                    f"skill:{response.route.skill}" if response.route else payload.capability_id
                ),
                output=response.result,
                usage=response.usage,
            )
            request.app.state.invocation_store.complete(
                invocation_id,
                result.model_dump_json(),
            )
            return result
        except TargetNotFoundError as exc:
            request.app.state.invocation_store.fail(invocation_id, "target_not_found")
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AttachmentNotFoundError as exc:
            request.app.state.invocation_store.fail(invocation_id, "artifact_not_found")
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PolicyDeniedError as exc:
            request.app.state.invocation_store.fail(invocation_id, "policy_denied")
            if native_started and native_context:
                await request.app.state.run_store.append(
                    native_context.run_id, "run.failed", {"error": "policy_denied"}
                )
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExecutionError as exc:
            request.app.state.invocation_store.fail(
                invocation_id, "outcome_uncertain" if native_dispatched else "execution_failed"
            )
            if native_started and native_context:
                await request.app.state.run_store.append(
                    native_context.run_id,
                    "run.failed",
                    {"error": "outcome_uncertain" if native_dispatched else "execution_failed"},
                )
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/agent-invocations", response_model=AgentInvocationResult)
    async def invoke_agent(
        payload: AgentInvocationRequest,
        request: Request,
        response: Response,
    ) -> AgentInvocationResult:
        try:
            manager = request.app.state.runtime_manager
            _, parent = await manager.resolve_child(
                payload.parent_run_id, _request_principal(request),
                target_agent_id=payload.agent_id,
            )
            invoke = (request.app.state.skill_orchestrator.delegate
                      if parent.lifecycle == "durable" else manager.invoke_agent)
            result = await invoke(
                AgentTask(
                    parent_run_id=payload.parent_run_id,
                    target_agent_id=payload.agent_id,
                    goal=payload.goal,
                    input=payload.input,
                    artifact_ids=tuple(payload.artifact_ids),
                    idempotency_key=payload.idempotency_key,
                    turn_index=payload.turn_index,
                ),
                _request_principal(request),
            )
        except (InvocationConflictError, InvocationInProgressError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result.status == "awaiting-approval":
            response.status_code = 202
        return AgentInvocationResult(
            status=result.status,
            invocation_id=result.invocation_id,
            run_id=result.run_id,
            parent_run_id=result.parent_run_id,
            solution_id=result.solution_id,
            solution_release_id=result.solution_release_id,
            agent_id=result.agent_id,
            output=result.output,
            usage=result.usage,
            tasks=result.tasks,
        )

    async def save_artifact(
        request: Request,
        file: UploadFile,
        solution_id: str | None,
    ) -> AttachmentResponse:
        store: AttachmentStore = request.app.state.attachments
        snapshot = _request_snapshot(request, solution_id)
        try:
            record = await store.save(
                file,
                workspace_id=_request_principal(request).workspace_id,
                solution_id=snapshot.solution_id,
            )
        except AttachmentRejectedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return AttachmentResponse(
            attachment_id=record.attachment_id,
            artifact_id=record.artifact_id,
            filename=record.filename,
            size=record.size,
            media_type=record.media_type,
            content_digest=record.content_digest,
            solution_id=record.solution_id,
        )

    @app.post("/v1/artifacts", response_model=AttachmentResponse, status_code=201)
    async def upload_artifact(
        request: Request,
        file: Annotated[UploadFile, File()],
        solution_id: str | None = None,
    ) -> AttachmentResponse:
        return await save_artifact(request, file, solution_id)

    @app.post("/v1/attachments", response_model=AttachmentResponse, status_code=201)
    async def upload_attachment(
        request: Request,
        file: Annotated[UploadFile, File()],
        solution_id: str | None = None,
    ) -> AttachmentResponse:
        return await save_artifact(request, file, solution_id)

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: str,
        request: Request,
        solution_id: str | None = None,
    ) -> dict[str, object]:
        snapshot = _request_snapshot(request, solution_id)
        try:
            artifact = request.app.state.attachments.get_artifact(
                artifact_id,
                workspace_id=_request_principal(request).workspace_id,
                solution_id=snapshot.solution_id,
                enforce_scope=True,
            )
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return artifact.model_dump(mode="json")

    @app.get("/v1/artifacts/{artifact_id}/content", response_class=FileResponse)
    async def download_artifact_content(
        artifact_id: str,
        request: Request,
        solution_id: str | None = None,
    ) -> FileResponse:
        snapshot = _request_snapshot(request, solution_id)
        try:
            artifact = request.app.state.attachments.get(
                artifact_id,
                workspace_id=_request_principal(request).workspace_id,
                solution_id=snapshot.solution_id,
                enforce_scope=True,
            )
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not artifact.path.is_file():
            raise HTTPException(
                status_code=409,
                detail="Directory Artifacts do not have a single downloadable content file",
            )
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            filename=artifact.filename,
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"{artifact.content_digest}"',
                "X-Artifact-Content-Digest": artifact.content_digest,
            },
        )

    @app.get("/v1/memories", response_model=list[MemoryRecord])
    async def list_user_memories(
        solution_id: str,
        request: Request,
    ) -> list[MemoryRecord]:
        _request_snapshot(request, solution_id)
        principal = _request_principal(request)
        return list(
            request.app.state.memory_store.list(
                workspace_id=principal.workspace_id,
                solution_id=solution_id,
                scope="user",
                scope_id=principal.principal_id,
            )
        )

    @app.put("/v1/memories", response_model=MemoryRecord)
    async def put_user_memory(
        payload: UserMemoryInput,
        request: Request,
    ) -> MemoryRecord:
        _request_snapshot(request, payload.solution_id)
        principal = _request_principal(request)
        return request.app.state.memory_store.put(
            workspace_id=principal.workspace_id,
            solution_id=payload.solution_id,
            scope="user",
            scope_id=principal.principal_id,
            key=payload.key,
            value=payload.value,
            created_by=principal.principal_id,
            source_run_id=payload.source_run_id,
            sensitivity=payload.sensitivity,
            expires_at=payload.expires_at,
        )

    @app.delete("/v1/memories/{memory_id}", status_code=204)
    async def delete_user_memory(
        memory_id: str,
        solution_id: str,
        request: Request,
    ) -> Response:
        _request_snapshot(request, solution_id)
        principal = _request_principal(request)
        deleted = request.app.state.memory_store.delete(
            memory_id,
            workspace_id=principal.workspace_id,
            solution_id=solution_id,
            scope_id=principal.principal_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found")
        return Response(status_code=204)

    @app.get("/v1/human-tasks", response_model=list[HumanTaskRecord])
    async def list_human_tasks(
        request: Request,
        solution_id: str | None = None,
        status: str | None = "pending",
    ) -> list[HumanTaskRecord]:
        _request_snapshot(request, solution_id)
        principal = _request_principal(request)
        return list(
            request.app.state.human_task_store.list_for_principal(
                workspace_id=principal.workspace_id,
                solution_id=solution_id or "_standalone",
                roles=principal.roles,
                status=status,
            )
        )

    @app.post("/v1/human-tasks/{task_id}/decision", response_model=HumanTaskRecord)
    async def decide_human_task(
        task_id: str,
        payload: HumanTaskDecisionInput,
        request: Request,
    ) -> HumanTaskRecord:
        task = request.app.state.human_task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="HumanTask not found")
        if task.subject.get("scope") in {"published-tool", "published-skill"}:
            run = await request.app.state.run_store.get(task.run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            _request_snapshot(request, run.context.solution_id)
        else:
            _request_snapshot(request, task.solution_id)
        principal = _request_principal(request)
        if task.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="HumanTask not found")
        if not principal.roles.intersection(task.assigned_roles):
            raise HTTPException(status_code=403, detail="Principal is not assigned to this task")
        try:
            if task.status == "pending":
                decided = request.app.state.human_task_store.decide(
                    task_id,
                    actor_id=principal.principal_id,
                    actor_roles=principal.roles,
                    decision=payload.decision,
                    approve=payload.approve,
                    reason=payload.reason,
                )
            elif task.decision == payload.decision and (
                task.kind != "approval"
                or task.status == ("approved" if payload.approve else "rejected")
            ):
                decided = task
            else:
                raise HumanTaskError("HumanTask has already been decided differently")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="HumanTask not found") from exc
        except HumanTaskError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        run = await request.app.state.run_store.get(decided.run_id)
        backend = request.app.state.durable_execution
        if (
            run is not None
            and run.context.lifecycle == "durable"
            and run.context.request_id is not None
            and backend is not None
        ):
            try:
                await backend.signal(
                    run.context.request_id,
                    "human_task_decided",
                    decided.model_dump(mode="json"),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "HumanTask was saved but workflow resume signal failed; retry the decision"
                    ),
                ) from exc
            if not any(
                event.name == "human_task.decided" and event.data.get("task_id") == decided.task_id
                for event in run.events
            ):
                await request.app.state.run_store.append(
                    run.context.run_id,
                    "human_task.decided",
                    {"task_id": decided.task_id, "status": decided.status},
                )
        return decided

    @app.post("/v1/messages/stream", response_class=EventSourceResponse)
    async def stream_message(
        payload: MessageRequest,
        request: Request,
    ) -> AsyncIterator[ServerSentEvent]:
        service: DynamicMessageService = request.app.state.message_service
        async for event in service.stream(
            payload,
            principal=_request_principal(request),
        ):
            yield ServerSentEvent(
                event=event.name,
                data=event.data,
                id=str(event.sequence),
            )

    def authorized_run(request: Request, snapshot: RunSnapshot | None) -> RunSnapshot:
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        principal = _request_principal(request)
        owner = snapshot.context.principal
        if (
            owner.workspace_id != principal.workspace_id
            or owner.principal_id != principal.principal_id
        ) and not principal.has_role("auditor"):
            raise HTTPException(status_code=404, detail="Run not found")
        return snapshot

    @app.get("/v1/operations/{run_id}", response_model=OperationHandle)
    async def get_operation(run_id: str, request: Request) -> OperationHandle:
        authorized_run(request, await request.app.state.run_store.get(run_id))
        try:
            return await request.app.state.operation_service.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Operation not found") from exc

    @app.post(
        "/v1/operations/{run_id}/cancel",
        response_model=OperationHandle,
        status_code=202,
    )
    async def cancel_operation(run_id: str, request: Request) -> OperationHandle:
        authorized_run(request, await request.app.state.run_store.get(run_id))
        try:
            return await request.app.state.operation_service.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Operation not found") from exc
        except OperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, object]:
        snapshot = authorized_run(request, await request.app.state.run_store.get(run_id))
        return {
            "run_id": snapshot.context.run_id,
            "thread_id": snapshot.context.thread_id,
            "solution_id": snapshot.context.solution_id,
            "solution_release_id": snapshot.context.solution_release_id,
            "generation_id": snapshot.context.generation_id,
            "mode": snapshot.context.mode,
            "lifecycle": snapshot.context.lifecycle,
            "status": snapshot.status,
            "created_at": snapshot.context.created_at.isoformat(),
            "event_count": len(snapshot.events),
        }

    @app.get("/v1/runs/{run_id}/result", response_model=ResultView)
    async def get_run_result(run_id: str, request: Request) -> ResultView:
        snapshot = authorized_run(request, await request.app.state.run_store.get(run_id))
        try:
            return build_result_view(snapshot)
        except ResultUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/runs/{run_id}/feedback",
        response_model=FeedbackRecord,
        status_code=201,
    )
    async def create_run_feedback(
        run_id: str,
        payload: FeedbackInput,
        request: Request,
        response: Response,
    ) -> FeedbackRecord:
        snapshot = authorized_run(request, await request.app.state.run_store.get(run_id))
        principal = _request_principal(request)
        if snapshot.context.principal.principal_id != principal.principal_id:
            raise HTTPException(status_code=404, detail="Run not found")
        idempotency_key = (request.headers.get("idempotency-key") or "").strip()
        if not idempotency_key or len(idempotency_key) > 255:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key is required and must not exceed 255 characters",
            )
        try:
            result = build_result_view(snapshot)
            validate_feedback_targets(payload, result)
        except ResultUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FeedbackTargetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        request_digest = canonical_digest(
            {
                "run_id": run_id,
                "result_digest": result.result_digest,
                "feedback": payload.model_dump(mode="json"),
            }
        )
        record = new_feedback_record(
            workspace_id=principal.workspace_id,
            principal_id=principal.principal_id,
            result=result,
            payload=payload,
        )
        try:
            stored, created = request.app.state.feedback_store.create(
                record,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return stored

    @app.get(
        "/v1/runs/{run_id}/feedback",
        response_model=list[FeedbackRecord],
    )
    async def list_run_feedback(run_id: str, request: Request) -> list[FeedbackRecord]:
        snapshot = authorized_run(request, await request.app.state.run_store.get(run_id))
        return list(
            request.app.state.feedback_store.list_for_run(
                run_id,
                workspace_id=snapshot.context.principal.workspace_id,
            )
        )

    def owned_feedback(feedback_id: str, request: Request) -> FeedbackRecord:
        principal = _request_principal(request)
        record = request.app.state.feedback_store.get(feedback_id)
        if (
            record is None
            or record.workspace_id != principal.workspace_id
            or record.principal_id != principal.principal_id
        ):
            raise HTTPException(status_code=404, detail="Feedback not found")
        return record

    @app.get("/v1/feedback/{feedback_id}", response_model=FeedbackRecord)
    async def get_feedback(feedback_id: str, request: Request) -> FeedbackRecord:
        return owned_feedback(feedback_id, request)

    @app.post(
        "/v1/feedback/{feedback_id}/memory/confirm",
        response_model=FeedbackMemoryConfirmation,
        status_code=201,
    )
    async def confirm_feedback_memory(
        feedback_id: str,
        request: Request,
        response: Response,
    ) -> FeedbackMemoryConfirmation:
        feedback = owned_feedback(feedback_id, request)
        proposal = feedback.memory_proposal
        if feedback.expected_action != "propose-memory" or proposal is None:
            raise HTTPException(status_code=409, detail="Feedback has no memory proposal")
        if feedback.solution_id is None:
            raise HTTPException(status_code=409, detail="Memory proposals require a Solution Run")
        existing_confirmation = request.app.state.feedback_store.get_memory_confirmation(
            feedback.feedback_id
        )
        if existing_confirmation is not None:
            response.status_code = 200
            return existing_confirmation

        _request_snapshot(request, feedback.solution_id)
        principal = _request_principal(request)
        existing_memories = request.app.state.memory_store.list(
            workspace_id=principal.workspace_id,
            solution_id=feedback.solution_id,
            scope="user",
            scope_id=principal.principal_id,
        )
        existing = next(
            (memory for memory in existing_memories if memory.key == proposal.key),
            None,
        )
        if existing is not None and (
            existing.value != proposal.value
            or existing.sensitivity != proposal.sensitivity
            or existing.expires_at != proposal.expires_at
        ):
            raise HTTPException(
                status_code=409,
                detail="A different user memory already exists for this key",
            )
        memory = existing or request.app.state.memory_store.put(
            workspace_id=principal.workspace_id,
            solution_id=feedback.solution_id,
            scope="user",
            scope_id=principal.principal_id,
            key=proposal.key,
            value=proposal.value,
            created_by=principal.principal_id,
            source_run_id=feedback.run_id,
            sensitivity=proposal.sensitivity,
            expires_at=proposal.expires_at,
        )
        confirmation = new_memory_confirmation(
            feedback,
            memory_id=memory.memory_id,
            memory_digest=canonical_digest(memory.model_dump(mode="json")),
        )
        try:
            stored, created = request.app.state.feedback_store.confirm_memory(confirmation)
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not created:
            response.status_code = 200
        return stored

    @app.get("/v1/runs/{run_id}/events")
    async def get_run_events(
        run_id: str,
        request: Request,
        after: int = 0,
    ) -> list[dict[str, object]]:
        if after < 0:
            raise HTTPException(status_code=400, detail="after must be non-negative")
        snapshot = authorized_run(request, await request.app.state.run_store.get(run_id))
        return [
            _run_event_payload(run_id, event)
            for event in snapshot.events
            if event.sequence > after
        ]

    @app.get("/v1/runs/{run_id}/events/stream")
    async def stream_run_events(
        run_id: str,
        request: Request,
        after_sequence: int = 0,
    ) -> EventSourceResponse:
        if after_sequence < 0:
            raise HTTPException(
                status_code=400,
                detail="after_sequence must be non-negative",
            )
        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            try:
                resumed_sequence = int(last_event_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative event sequence",
                ) from exc
            if resumed_sequence < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative event sequence",
                )
            after_sequence = max(after_sequence, resumed_sequence)

        initial = authorized_run(
            request,
            await request.app.state.run_store.get(run_id),
        )

        async def persisted_events() -> AsyncIterator[str]:
            cursor = after_sequence
            snapshot = initial
            while True:
                for event in snapshot.events:
                    if event.sequence <= cursor:
                        continue
                    cursor = event.sequence
                    data = json.dumps(
                        _run_event_payload(run_id, event),
                        ensure_ascii=False,
                    )
                    yield f"id: {event.sequence}\nevent: {event.name}\ndata: {data}\n\n"
                if snapshot.status != "running" or await request.is_disconnected():
                    return
                snapshot = authorized_run(
                    request,
                    await request.app.state.run_store.wait_for_events(
                        run_id,
                        after_sequence=cursor,
                        timeout_seconds=15,
                    ),
                )
                if snapshot.status == "running" and all(
                    event.sequence <= cursor for event in snapshot.events
                ):
                    yield ": keep-alive\n\n"

        return EventSourceResponse(persisted_events())

    @app.get("/v1/runs/{run_id}/plan", response_model=PlanRecord)
    async def get_run_plan(run_id: str, request: Request) -> PlanRecord:
        authorized_run(request, await request.app.state.run_store.get(run_id))
        plan = request.app.state.plan_store.get_for_run(run_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        return plan

    def durable_backend(request: Request) -> DurableExecutionPort:
        backend = request.app.state.durable_execution
        if backend is None:
            raise HTTPException(
                status_code=503,
                detail="Durable execution is not configured",
            )
        return backend

    @app.post("/v1/durable-runs", response_model=DurableInvocationResponse, status_code=202)
    async def start_durable_run(
        payload: DurableInvocationInput,
        request: Request,
    ) -> DurableInvocationResponse:
        if payload.skill is not None or payload.agent_id is not None:
            if (
                sum(
                    target is not None
                    for target in (
                        payload.skill,
                        payload.agent_id,
                        payload.workflow_type,
                    )
                )
                != 1
            ):
                raise HTTPException(status_code=422, detail="Select exactly one durable target")
            snapshot = _request_snapshot(request, payload.solution_id, required_profile="durable")
            return await request.app.state.skill_orchestrator.start(
                payload,
                snapshot,
                _request_principal(request),
            )
        if payload.workflow_type is None or payload.solution_id is None:
            raise HTTPException(
                status_code=422, detail="Legacy workflow requires Solution and workflow_type"
            )
        principal = _request_principal(request)
        snapshot = _request_snapshot(request, payload.solution_id, required_profile="durable")
        release = snapshot.solution_release
        workflow_component = (
            next(
                (
                    item
                    for item in release.components
                    if item.kind == "workflow" and item.component_id == payload.workflow_type
                ),
                None,
            )
            if release is not None
            else None
        )
        if release is None or workflow_component is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow not found in Solution Release: {payload.workflow_type}",
            )
        workflow_template = None
        workflow_path = release.component_paths.get(f"workflow:{workflow_component.component_id}")
        if workflow_path is not None and release.package_relpath is not None:
            package_root = request.app.state.control_store.resolve_path(release.package_relpath)
            try:
                workflow_template = load_workflow_template(package_root / workflow_path)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if (
                workflow_template.workflow_id != workflow_component.component_id
                or workflow_template.version != workflow_component.version
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Workflow Template identity does not match the Release lock",
                )
        identity_digest = canonical_digest(
            {
                "workspace_id": principal.workspace_id,
                "principal_id": principal.principal_id,
                "solution_id": payload.solution_id,
                "idempotency_key": payload.idempotency_key,
            }
        ).removeprefix("sha256:")
        run_id = f"run_{identity_digest}"
        workflow_id = f"sage-{identity_digest}"
        request_digest = canonical_digest(
            {
                "solution_release_id": release.release_id,
                "workflow_type": payload.workflow_type,
                "mode": payload.mode,
                "payload": payload.payload,
                "execution_timeout_seconds": payload.execution_timeout_seconds,
            }
        )
        existing = await request.app.state.run_store.get(run_id)
        backend = durable_backend(request)
        if existing is not None:
            authorized_run(request, existing)
            started_event = next(
                (event for event in existing.events if event.name == "workflow.started"),
                None,
            )
            if started_event is None or started_event.data.get("request_digest") != request_digest:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used for a different durable run",
                )
            handle = await backend.describe(workflow_id)
            return DurableInvocationResponse(
                run_id=run_id,
                workflow_id=workflow_id,
                temporal_run_id=handle.run_id,
                status=handle.status,
                solution_id=payload.solution_id,
                solution_release_id=release.release_id,
            )
        context = RuntimeContext.create(
            mode=payload.mode,
            lifecycle="durable",
            principal=principal,
            run_id=run_id,
            generation_id=snapshot.generation.generation_id,
            solution_id=payload.solution_id,
            solution_release_id=release.release_id,
            request_id=workflow_id,
        )
        await request.app.state.run_store.begin(
            context,
            input_data={
                "kind": "durable",
                "request": payload.model_dump(mode="json", exclude_none=True),
            },
        )
        try:
            handle = await backend.start(
                DurableRunRequest(
                    workflow_id=workflow_id,
                    workflow_type=payload.workflow_type,
                    task_queue=request.app.state.control_settings.temporal_task_queue,
                    payload={
                        "context": {
                            "run_id": run_id,
                            "workspace_id": principal.workspace_id,
                            "principal_id": principal.principal_id,
                            "solution_id": payload.solution_id,
                            "solution_release_id": release.release_id,
                            "generation_id": snapshot.generation.generation_id,
                        },
                        "input": payload.payload,
                        "workflow_type": payload.workflow_type,
                        "workflow": (
                            workflow_template.model_dump(mode="json")
                            if workflow_template is not None
                            else None
                        ),
                    },
                    execution_timeout_seconds=payload.execution_timeout_seconds,
                )
            )
            await request.app.state.run_store.append(
                run_id,
                "workflow.started",
                {
                    "workflow_id": workflow_id,
                    "temporal_run_id": handle.run_id,
                    "request_digest": request_digest,
                },
            )
        except Exception as exc:
            await request.app.state.run_store.append(
                run_id,
                "run.failed",
                {"code": "durable_start_failed", "message": type(exc).__name__},
            )
            raise HTTPException(status_code=502, detail="Durable workflow start failed") from exc
        return DurableInvocationResponse(
            run_id=run_id,
            workflow_id=workflow_id,
            temporal_run_id=handle.run_id,
            status=handle.status,
            solution_id=payload.solution_id,
            solution_release_id=release.release_id,
        )

    @app.get("/v1/durable-runs/{run_id}", response_model=DurableInvocationResponse)
    async def get_durable_run(run_id: str, request: Request) -> DurableInvocationResponse:
        run = authorized_run(request, await request.app.state.run_store.get(run_id))
        if run.context.lifecycle != "durable" or run.context.request_id is None:
            raise HTTPException(status_code=404, detail="Durable run not found")
        handle = await durable_backend(request).describe(run.context.request_id)
        if run.status == "running":
            terminal_event = {
                "completed": "run.completed",
                "failed": "run.failed",
                "timed-out": "run.failed",
                "terminated": "run.failed",
                "canceled": "run.cancelled",
                "cancelled": "run.cancelled",
            }.get(handle.status)
            if terminal_event is not None:
                await request.app.state.run_store.append(
                    run_id,
                    terminal_event,
                    {"workflow_id": run.context.request_id, "status": handle.status},
                )
        return DurableInvocationResponse(
            run_id=run_id,
            workflow_id=handle.workflow_id,
            temporal_run_id=handle.run_id,
            status=handle.status,
            solution_id=run.context.solution_id or "",
            solution_release_id=run.context.solution_release_id or "",
        )

    @app.get("/v1/durable-runs/{run_id}/result")
    async def durable_result(run_id: str, request: Request):
        run = authorized_run(request, await request.app.state.run_store.get(run_id))
        if run.context.lifecycle != "durable" or run.context.request_id is None:
            raise HTTPException(status_code=404, detail="Durable run not found")
        result = next(
            (event.data for event in run.events if event.name == "skill.orchestration.result"), None
        )
        if result is None:
            handle = await get_durable_run(run_id, request)
            if handle.status != "completed":
                raise HTTPException(
                    status_code=409, detail="No completed Workflow result is available",
                )
            result = await durable_backend(request).result(run.context.request_id)
        return result

    @app.post("/v1/durable-runs/{run_id}/cancel", status_code=202)
    async def cancel_durable_run(run_id: str, request: Request) -> dict[str, str]:
        run = authorized_run(request, await request.app.state.run_store.get(run_id))
        if run.context.lifecycle != "durable" or run.context.request_id is None:
            raise HTTPException(status_code=404, detail="Durable run not found")
        if run.status == "cancelled":
            return {"run_id": run_id, "status": "cancelled"}
        if run.status != "running":
            raise HTTPException(
                status_code=409,
                detail=f"Durable run is already terminal: {run.status}",
            )
        await durable_backend(request).cancel(
            run.context.request_id,
            reason="Cancelled by the requesting principal",
        )
        await request.app.state.run_store.append(
            run_id,
            "run.cancelled",
            {"workflow_id": run.context.request_id},
        )
        return {"run_id": run_id, "status": "cancelled"}

    @app.get("/admin", include_in_schema=False)
    async def admin_index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "admin.html")

    @app.get("/solution-api", include_in_schema=False)
    async def solution_api_index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "solution-api.html")

    app.include_router(create_admin_router())

    return app


app = create_app()
