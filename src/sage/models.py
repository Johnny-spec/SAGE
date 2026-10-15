"""Domain models for discovered Skill packages."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class ArgumentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    position: int | None = Field(default=None, ge=0)
    option: str | None = None
    encoding: Literal["string", "json-file"] = "string"

    @model_validator(mode="after")
    def validate_binding(self) -> "ArgumentBinding":
        if (self.position is None) == (self.option is None):
            raise ValueError("exactly one of position or option is required")
        return self


class ExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["prompt", "builtin", "python-subprocess", "http", "mcp", "workflow", "model"]
    handler: str | None = None
    entrypoint: str | None = None
    endpoint_alias: str | None = Field(default=None, alias="endpointAlias")
    server_alias: str | None = Field(default=None, alias="serverAlias")
    tool_name: str | None = Field(default=None, alias="toolName")
    schema_digest: str | None = Field(default=None, alias="schemaDigest")
    server_config_digest: str | None = Field(default=None, alias="serverConfigDigest")
    arguments: list[ArgumentBinding] = Field(default_factory=list)
    workflow: str | None = None
    capability_ids: tuple[str, ...] = Field(default=(), alias="capabilityIds", max_length=100)
    max_steps: int = Field(default=40, ge=1, le=200, alias="maxSteps")
    max_model_calls: int = Field(default=20, ge=1, le=100, alias="maxModelCalls")
    max_tool_calls: int = Field(default=20, ge=1, le=100, alias="maxToolCalls")

    @model_validator(mode="after")
    def validate_execution_target(self) -> "ExecutionBinding":
        if self.type in {"workflow", "model"}:
            if not self.capability_ids or len(self.capability_ids) != len(set(self.capability_ids)):
                raise ValueError("Orchestrated Skills require distinct explicit capabilityIds")
            if any(not value.startswith(("skill:", "tool:mcp:")) for value in self.capability_ids):
                raise ValueError("Orchestrated Skills support Skill and native Tool dependencies")
        elif self.workflow is not None or self.capability_ids:
            raise ValueError("Orchestration fields require workflow or model execution")
        if self.type == "workflow" and not self.workflow:
            raise ValueError("workflow execution requires a package-relative workflow file")
        if self.type == "model" and self.workflow is not None:
            raise ValueError("model execution cannot declare a workflow file")
        if self.type == "http" and not self.endpoint_alias:
            raise ValueError("http execution requires endpointAlias")
        if self.type == "mcp" and (not self.server_alias or not self.tool_name):
            raise ValueError("mcp execution requires serverAlias and toolName")
        return self


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    attachment_types: list[str] = Field(default_factory=list, alias="attachmentTypes")


class SkillPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: Literal["read-only", "write-low", "write-high", "destructive"] = "read-only"
    timeout_seconds: int = Field(default=30, ge=1, le=300, alias="timeoutSeconds")
    max_output_bytes: int = Field(default=65_536, ge=1, alias="maxOutputBytes")
    result_validator: str | None = Field(default=None, alias="resultValidator")
    execution_enabled: bool = Field(default=True, alias="executionEnabled")


class SkillSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution: ExecutionBinding
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    routing: RoutingPolicy = Field(default_factory=RoutingPolicy)
    policy: SkillPolicy = Field(default_factory=SkillPolicy)


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["sage.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["SkillBinding"]
    metadata: ManifestMetadata
    spec: SkillSpec


class SkillDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    version: str
    package_root: Path
    manifest: SkillManifest

    @property
    def route_keywords(self) -> tuple[str, ...]:
        return tuple(keyword.casefold() for keyword in self.manifest.spec.routing.keywords)
