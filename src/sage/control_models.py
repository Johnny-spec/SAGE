"""Serializable control-plane records used before database persistence is introduced."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage.mcp_test_models import ToolRiskReview, ToolTestRecord
from sage.tools import McpToolDefinition
from sage.workflow_draft_models import WorkflowDraftRecord


def utc_now() -> datetime:
    return datetime.now(UTC)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Literal["error", "warning", "info"]
    code: str
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not-run", "valid", "invalid"] = "not-run"
    checked_at: datetime | None = None
    content_digest: str | None = None
    issues: tuple[ValidationIssue, ...] = ()


class AgentDraftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    definition: dict[str, Any]
    status: Literal["draft", "valid", "invalid", "published"] = "draft"
    validation: ValidationReport = Field(default_factory=ValidationReport)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillPackageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str
    source: Literal["zip", "mcp"]
    original_filename: str
    source_ref: str | None = None
    original_relpath: str | None = None
    package_relpath: str
    name: str | None = None
    version: str | None = None
    content_digest: str
    execution_type: str | None = None
    executable: bool = False
    status: Literal["uploaded", "valid", "invalid", "published"] = "uploaded"
    validation: ValidationReport = Field(default_factory=ValidationReport)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class McpToolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    schema_digest: str


class McpServerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    alias: str
    transport: Literal["streamable-http", "stdio"]
    url: str | None = None
    command_preset: str | None = None
    secret_ref: str | None = None
    dev_secret: str | None = None
    config_digest: str
    status: Literal["draft", "reachable", "discovered", "failed", "disabled"] = "draft"
    tools: tuple[McpToolRecord, ...] = ()
    tested_at: datetime | None = None
    tested_config_digest: str | None = None
    discovered_at: datetime | None = None
    discovered_config_digest: str | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ToolRegistrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registration_id: str
    tool_id: str
    definition: McpToolDefinition
    content_digest: str
    status: Literal["draft", "published"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_identity(self) -> "ToolRegistrationRecord":
        if self.tool_id != self.definition.tool_id:
            raise ValueError("Tool registration identity does not match its binding")
        if self.content_digest != self.definition.content_digest:
            raise ValueError("Tool registration digest does not match its definition")
        return self


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    kind: Literal["candidate", "rollback"]
    target_id: str
    status: Literal["pending", "approved", "rejected", "consumed"] = "pending"
    diff_digest: str
    requested_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None
    actor_fingerprint: str | None = None
    reason: str | None = None


class CatalogCandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    base_generation_id: str
    title: str
    description: str = ""
    agent_draft_ids: tuple[str, ...] = ()
    skill_package_ids: tuple[str, ...] = ()
    tool_registration_ids: tuple[str, ...] = ()
    solution_release_ids: tuple[str, ...] = ()
    default_agent_id: str | None = None
    status: Literal[
        "draft",
        "valid",
        "invalid",
        "pending-approval",
        "approved",
        "rejected",
        "active",
        "activation-failed",
    ] = "draft"
    validation: ValidationReport = Field(default_factory=ValidationReport)
    diff_digest: str | None = None
    approval_id: str | None = None
    generation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GenerationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: str
    parent_generation_id: str | None = None
    content_digest: str
    skills_root: str
    skill_versions: dict[str, str]
    skill_digests: dict[str, str] = Field(default_factory=dict)
    tool_definitions: dict[str, McpToolDefinition] = Field(default_factory=dict)
    agent_definitions: dict[str, dict[str, Any]]
    solution_releases: dict[str, str] = Field(default_factory=dict)
    default_agent_id: str = "diagnostic-agent"
    candidate_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


SolutionComponentKind = Literal[
    "skill",
    "mcp-tool",
    "agent",
    "workflow",
    "trigger",
    "prompt",
    "model-profile",
    "mcp-server",
    "policy",
    "content",
    "ui-schema",
    "evaluation-suite",
]
SolutionExecutionProfile = Literal[
    "direct",
    "interactive",
    "durable",
    "scheduled",
    "event-driven",
]


class SolutionComponentLock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SolutionComponentKind
    component_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SolutionDefinitionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    owner: str = Field(min_length=1)
    entry_agent_id: str | None = None
    allowed_roles: tuple[str, ...] = ("user",)
    supported_profiles: tuple[SolutionExecutionProfile, ...] = ("direct", "interactive")
    status: Literal["draft", "active", "disabled"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SolutionReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str = Field(min_length=1)
    solution_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    components: tuple[SolutionComponentLock, ...] = Field(min_length=1)
    component_paths: dict[str, str] = Field(default_factory=dict)
    package_relpath: str | None = None
    package_metadata: dict[str, str] = Field(default_factory=dict)
    entry_agent_id: str | None = None
    allowed_roles: tuple[str, ...] = ("user",)
    supported_profiles: tuple[SolutionExecutionProfile, ...] = ("direct", "interactive")
    source_generation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EvaluationReportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    release_id: str
    release_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    suite_id: str
    suite_version: str
    suite_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["passed", "failed"]
    metrics: dict[str, float] = Field(default_factory=dict)
    summary: str = ""
    actor_fingerprint: str
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class ControlPlaneState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[13] = 13
    workflow_drafts: dict[str, WorkflowDraftRecord] = Field(default_factory=dict)
    active_generation_id: str
    agents: dict[str, AgentDraftRecord] = Field(default_factory=dict)
    skills: dict[str, SkillPackageRecord] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServerRecord] = Field(default_factory=dict)
    tool_registrations: dict[str, ToolRegistrationRecord] = Field(default_factory=dict)
    mcp_test_reviews: dict[str, ToolRiskReview] = Field(default_factory=dict)
    mcp_tests: dict[str, ToolTestRecord] = Field(default_factory=dict)
    candidates: dict[str, CatalogCandidateRecord] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    generations: dict[str, GenerationRecord] = Field(default_factory=dict)
    thread_bindings: dict[str, str] = Field(default_factory=dict)
    solutions: dict[str, SolutionDefinitionRecord] = Field(default_factory=dict)
    solution_releases: dict[str, SolutionReleaseRecord] = Field(default_factory=dict)
    evaluations: dict[str, EvaluationReportRecord] = Field(default_factory=dict)
