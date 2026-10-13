"""Remote adapter for isolated Python Skill execution."""

import base64
import io
import stat
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from sage.digests import directory_digest, file_digest
from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.models import SkillDefinition
from sage.runner_protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerExecutionRequest,
    RunnerExecutionResponse,
    RunnerHealth,
    RunnerInputPayload,
    runner_request_digest,
)

_MAX_PACKAGE_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_INPUT_ARCHIVE_BYTES = 128 * 1024 * 1024


class RunnerClient(Protocol):
    async def health(self) -> RunnerHealth: ...

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse: ...


class HttpRunnerClient:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._transport = transport

    async def health(self) -> RunnerHealth:
        try:
            async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
                response = await client.get(f"{self._endpoint}/health/ready")
                response.raise_for_status()
                health = RunnerHealth.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError("Isolated Runner readiness check failed") from exc
        return health

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds + 10,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._endpoint}/v1/execute",
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "X-Sage-Runner-Protocol": RUNNER_PROTOCOL_VERSION,
                    },
                    json=request.model_dump(mode="json"),
                )
        except httpx.HTTPError as exc:
            raise ExecutionError("Isolated Runner is unavailable") from exc
        try:
            body = RunnerExecutionResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise ExecutionError("Isolated Runner returned an invalid response") from exc
        if (
            body.execution_id != request.execution_id
            or body.request_digest != request.request_digest
        ):
            raise ExecutionError("Isolated Runner response identity mismatch")
        if response.is_error or body.status != "completed" or body.result is None:
            raise ExecutionError(
                f"Isolated Runner {body.error_code or body.status}: "
                f"{body.message or 'execution failed'}"
            )
        return body


class RunnerAdapter:
    def __init__(
        self,
        client: RunnerClient,
        *,
        allowed_input_roots: Sequence[Path],
        profile: str = "python-restricted-v1",
    ) -> None:
        self._client = client
        self._allowed_input_roots = tuple(path.resolve() for path in allowed_input_roots)
        self._profile = profile

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        binding = definition.manifest.spec.execution
        if binding.type != "python-subprocess" or not binding.entrypoint:
            raise ExecutionError(f"Skill is not a Python Runner target: {definition.name}")
        inline_arguments = dict(arguments)
        inputs: list[RunnerInputPayload] = []
        for argument_name in tuple(inline_arguments):
            if not argument_name.casefold().endswith("_path"):
                continue
            raw_path = inline_arguments.pop(argument_name)
            if not isinstance(raw_path, str):
                raise ExecutionError(f"Runner path argument must be a string: {argument_name}")
            source = Path(raw_path).resolve(strict=True)
            if not any(source.is_relative_to(root) for root in self._allowed_input_roots):
                raise ExecutionError(
                    f"Runner input is outside managed Artifact storage: {argument_name}"
                )
            inputs.append(_input_payload(argument_name, source))

        execution_id = f"rex_{uuid4().hex}"
        package_archive = _archive_path(
            definition.package_root,
            max_bytes=_MAX_PACKAGE_ARCHIVE_BYTES,
        )
        values: dict[str, Any] = {
            "execution_id": execution_id,
            "run_id": context.run_id,
            "workspace_id": context.principal.workspace_id,
            "generation_id": context.generation_id,
            "skill_name": definition.name,
            "skill_version": definition.version,
            "package_digest": directory_digest(definition.package_root),
            "package_archive_base64": base64.b64encode(package_archive).decode("ascii"),
            "arguments": inline_arguments,
            "inputs": tuple(inputs),
            "timeout_seconds": definition.manifest.spec.policy.timeout_seconds,
            "max_output_bytes": definition.manifest.spec.policy.max_output_bytes,
            "profile": self._profile,
        }
        unsigned = RunnerExecutionRequest.model_construct(
            **values,
            protocol_version=RUNNER_PROTOCOL_VERSION,
            request_digest="sha256:" + "0" * 64,
        )
        request = RunnerExecutionRequest(
            **values,
            request_digest=runner_request_digest(unsigned),
        )
        response = await self._client.execute(request)
        if response.result is None:
            raise ExecutionError("Isolated Runner completed without a result")
        return response.result


def _input_payload(argument_name: str, source: Path) -> RunnerInputPayload:
    kind = "directory" if source.is_dir() else "file"
    relative_path = "content" if source.is_dir() else f"content/{source.name}"
    archive = _archive_path(source, max_bytes=_MAX_INPUT_ARCHIVE_BYTES)
    digest = directory_digest(source) if source.is_dir() else file_digest(source)
    return RunnerInputPayload(
        argument_name=argument_name,
        kind=kind,
        relative_path=relative_path,
        content_digest=digest,
        archive_base64=base64.b64encode(archive).decode("ascii"),
    )


def _archive_path(source: Path, *, max_bytes: int) -> bytes:
    resolved = source.resolve(strict=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        paths = [resolved] if resolved.is_file() else sorted(
            path for path in resolved.rglob("*") if path.is_file() or path.is_symlink()
        )
        if not paths:
            raise ExecutionError(f"Runner archive source is empty: {resolved.name}")
        for path in paths:
            if path.is_symlink() or stat.S_ISLNK(path.lstat().st_mode):
                raise ExecutionError("Runner archives cannot contain symbolic links")
            relative = Path(path.name) if resolved.is_file() else path.relative_to(resolved)
            archive_name = (Path("content") / relative).as_posix()
            archive.writestr(archive_name, path.read_bytes())
            if output.tell() > max_bytes:
                raise ExecutionError("Runner archive exceeds its transfer limit")
    payload = output.getvalue()
    if len(payload) > max_bytes:
        raise ExecutionError("Runner archive exceeds its transfer limit")
    return payload
