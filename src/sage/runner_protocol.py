"""Versioned contracts for isolated Python Skill execution."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage.digests import canonical_digest

RUNNER_PROTOCOL_VERSION = "sage-runner/v1"


class RunnerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]
    protocol_version: Literal["sage-runner/v1"] = RUNNER_PROTOCOL_VERSION
    profile: str = Field(min_length=1, max_length=100)


class RunnerInputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argument_name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$",
    )
    kind: Literal["file", "directory"]
    relative_path: str = Field(min_length=1, max_length=1000)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    archive_base64: str = Field(min_length=1)


class RunnerExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["sage-runner/v1"] = RUNNER_PROTOCOL_VERSION
    execution_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=128)
    skill_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    )
    skill_version: str = Field(min_length=1, max_length=64)
    package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package_archive_base64: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    inputs: tuple[RunnerInputPayload, ...] = Field(default=(), max_length=20)
    timeout_seconds: int = Field(ge=1, le=300)
    max_output_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    profile: str = Field(default="python-restricted-v1", min_length=1, max_length=100)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request_digest(self) -> "RunnerExecutionRequest":
        if self.request_digest != runner_request_digest(self):
            raise ValueError("Runner request digest mismatch")
        input_names = [item.argument_name for item in self.inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("Runner input argument names must be unique")
        if set(input_names).intersection(self.arguments):
            raise ValueError("Runner path arguments must not appear in inline arguments")
        return self


class RunnerExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["sage-runner/v1"] = RUNNER_PROTOCOL_VERSION
    execution_id: str = Field(min_length=1, max_length=128)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["completed", "rejected", "failed", "timeout", "cancelled"]
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_outcome(self) -> "RunnerExecutionResponse":
        if self.status == "completed" and self.result is None:
            raise ValueError("Completed Runner responses require a result")
        if self.status != "completed" and (not self.error_code or not self.message):
            raise ValueError("Failed Runner responses require an error code and message")
        return self


def runner_request_digest(request: RunnerExecutionRequest) -> str:
    return canonical_digest(
        {
            "protocol_version": request.protocol_version,
            "execution_id": request.execution_id,
            "run_id": request.run_id,
            "workspace_id": request.workspace_id,
            "generation_id": request.generation_id,
            "skill_name": request.skill_name,
            "skill_version": request.skill_version,
            "package_digest": request.package_digest,
            "arguments": request.arguments,
            "inputs": [
                RunnerInputPayload.model_validate(item).model_dump(
                    mode="json",
                    exclude={"archive_base64"},
                )
                for item in request.inputs
            ],
            "timeout_seconds": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
            "profile": request.profile,
        }
    )
