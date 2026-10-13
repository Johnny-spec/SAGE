"""Versioned deterministic Workflow templates with bounded Agent nodes."""

from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class WorkflowRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_attempts: int = Field(default=1, ge=1, le=20)
    initial_interval_seconds: int = Field(default=1, ge=1, le=3600)
    backoff_coefficient: float = Field(default=2, ge=1, le=10)
    maximum_interval_seconds: int = Field(default=60, ge=1, le=86_400)


class WorkflowBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: Literal["literal", "workflow-input", "step-output", "artifact"]
    value: Any = None
    step_id: str | None = Field(default=None, alias="stepId", min_length=1, max_length=200)
    path: str = Field(default="", max_length=1000)
    artifact_id: str | None = Field(
        default=None,
        alias="artifactId",
        min_length=1,
        max_length=255,
    )

    @model_validator(mode="after")
    def validate_source(self) -> "WorkflowBinding":
        if self.path and not self.path.startswith("/"):
            raise ValueError("Workflow binding path must be a JSON Pointer")
        if self.source == "step-output" and self.step_id is None:
            raise ValueError("step-output binding requires stepId")
        if self.source != "step-output" and self.step_id is not None:
            raise ValueError(f"{self.source} binding cannot declare stepId")
        if self.source == "artifact" and self.artifact_id is None:
            raise ValueError("artifact binding requires artifactId")
        if self.source != "artifact" and self.artifact_id is not None:
            raise ValueError(f"{self.source} binding cannot declare artifactId")
        return self


class WorkflowCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: Literal["workflow-input", "step-output"]
    step_id: str | None = Field(default=None, alias="stepId", min_length=1, max_length=200)
    path: str = Field(default="", max_length=1000)
    operator: Literal["equals", "not-equals", "in", "not-in", "exists"] = "equals"
    value: Any = None

    @model_validator(mode="after")
    def validate_source(self) -> "WorkflowCondition":
        if self.path and not self.path.startswith("/"):
            raise ValueError("Workflow condition path must be a JSON Pointer")
        if self.source == "step-output" and self.step_id is None:
            raise ValueError("step-output condition requires stepId")
        if self.source == "workflow-input" and self.step_id is not None:
            raise ValueError("workflow-input condition cannot declare stepId")
        if self.operator in {"in", "not-in"} and not isinstance(
            self.value,
            (list, tuple),
        ):
            raise ValueError(f"{self.operator} condition requires an array value")
        return self


class WorkflowStepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    step_id: str = Field(alias="id", min_length=1, max_length=200)
    kind: Literal["skill", "tool", "agent", "workflow", "human-task"]
    target: str = Field(min_length=1, max_length=2000)
    depends_on: tuple[str, ...] = Field(default=(), alias="dependsOn")
    timeout_seconds: int = Field(default=300, alias="timeoutSeconds", ge=1, le=86_400)
    retry: WorkflowRetryPolicy = Field(default_factory=WorkflowRetryPolicy)
    input: dict[str, Any] = Field(default_factory=dict)
    inherit_input: bool = Field(default=True, alias="inheritInput")
    bindings: dict[str, WorkflowBinding] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict, alias="outputSchema")
    when: WorkflowCondition | None = None
    join_policy: Literal["all", "all-settled"] = Field(default="all", alias="joinPolicy")
    failure_policy: Literal["fail-fast", "continue-with-errors", "escalate"] = Field(
        default="fail-fast",
        alias="failurePolicy",
    )
    concurrency_key: str | None = Field(
        default=None,
        alias="concurrencyKey",
        min_length=1,
        max_length=200,
    )

    @model_validator(mode="after")
    def validate_output_schema(self) -> "WorkflowStepDefinition":
        if self.kind == "tool" and not self.target.startswith("tool:mcp:"):
            raise ValueError("Tool steps require a native MCP capability id")
        if self.output_schema:
            try:
                Draft202012Validator.check_schema(self.output_schema)
            except SchemaError as exc:
                raise ValueError(f"Invalid Workflow output schema: {exc.message}") from exc
        return self


class WorkflowTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["sage.dev/v1alpha1", "sage.dev/v1beta1"] = Field(
        alias="apiVersion"
    )
    kind: Literal["WorkflowTemplate"]
    workflow_id: str = Field(alias="workflowId", min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=64)
    steps: tuple[WorkflowStepDefinition, ...] = Field(min_length=1, max_length=500)
    max_concurrency: int = Field(default=1, alias="maxConcurrency", ge=1, le=100)
    outputs: dict[str, WorkflowBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> "WorkflowTemplate":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Workflow step ids must be unique")
        known = set(step_ids)
        if any(binding.source == "step-output" and binding.step_id not in known
               for binding in self.outputs.values()):
            raise ValueError("Workflow output references an unknown step")
        unknown = {
            dependency
            for step in self.steps
            for dependency in step.depends_on
            if dependency not in known
        }
        if unknown:
            raise ValueError(f"Unknown Workflow dependencies: {', '.join(sorted(unknown))}")
        dependencies = {step.step_id: set(step.depends_on) for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("Workflow dependency graph contains a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        ancestors: dict[str, set[str]] = {}

        def resolve_ancestors(step_id: str) -> set[str]:
            cached = ancestors.get(step_id)
            if cached is not None:
                return cached
            resolved = set(dependencies[step_id])
            for dependency in dependencies[step_id]:
                resolved.update(resolve_ancestors(dependency))
            ancestors[step_id] = resolved
            return resolved

        for step in self.steps:
            referenced_steps = {
                binding.step_id
                for binding in step.bindings.values()
                if binding.source == "step-output"
            }
            if step.when is not None and step.when.source == "step-output":
                referenced_steps.add(step.when.step_id)
            unavailable = {
                referenced
                for referenced in referenced_steps
                if referenced not in resolve_ancestors(step.step_id)
            }
            if unavailable:
                raise ValueError(
                    f"Workflow step {step.step_id} references unavailable outputs: "
                    f"{', '.join(sorted(str(value) for value in unavailable))}"
                )
        return self


def load_workflow_template(path: Path) -> WorkflowTemplate:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Workflow template must be an object")
        return WorkflowTemplate.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise ValueError(f"Invalid Workflow template: {exc}") from exc