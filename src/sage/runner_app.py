"""Isolated service boundary for executing transferred Python Skill packages."""

import asyncio
import base64
import binascii
import hmac
import io
import json
import os
import signal
import stat
import sys
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field

from sage.adapters.subprocess import build_subprocess_argv, safe_subprocess_environment
from sage.digests import directory_digest, file_digest
from sage.registry import SkillPackageError, SkillRegistry
from sage.runner_protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerExecutionRequest,
    RunnerExecutionResponse,
)


class RunnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=32)
    work_root: Path = Path("/tmp/sage-runner")
    profile: str = Field(default="python-restricted-v1", min_length=1, max_length=100)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    max_archive_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_extracted_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    max_files: int = Field(default=2000, ge=1, le=10_000)


class RunnerExecutionFailure(ValueError):
    def __init__(self, code: str, message: str, *, status: str = "rejected") -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def load_runner_settings() -> RunnerSettings:
    token = os.getenv("SAGE_RUNNER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SAGE_RUNNER_TOKEN is required")
    return RunnerSettings(
        token=token,
        work_root=Path(os.getenv("SAGE_RUNNER_WORK_ROOT", "/tmp/sage-runner")),
        profile=os.getenv("SAGE_RUNNER_PROFILE", "python-restricted-v1"),
        max_concurrency=int(os.getenv("SAGE_RUNNER_MAX_CONCURRENCY", "1")),
        max_archive_bytes=int(
            os.getenv("SAGE_RUNNER_MAX_ARCHIVE_BYTES", str(128 * 1024 * 1024))
        ),
        max_extracted_bytes=int(
            os.getenv("SAGE_RUNNER_MAX_EXTRACTED_BYTES", str(256 * 1024 * 1024))
        ),
        max_files=int(os.getenv("SAGE_RUNNER_MAX_FILES", "2000")),
    )


def create_runner_app(settings: RunnerSettings | None = None) -> FastAPI:
    configured = settings or load_runner_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured.work_root.mkdir(parents=True, exist_ok=True)
        app.state.runner_semaphore = asyncio.Semaphore(configured.max_concurrency)
        yield

    app = FastAPI(title="SAGE Isolated Runner", version="1.0", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live", "protocol_version": RUNNER_PROTOCOL_VERSION}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not configured.work_root.is_dir() or not os.access(configured.work_root, os.W_OK):
            raise HTTPException(status_code=503, detail="Runner work directory is unavailable")
        return {
            "status": "ready",
            "protocol_version": RUNNER_PROTOCOL_VERSION,
            "profile": configured.profile,
        }

    @app.post("/v1/execute", response_model=RunnerExecutionResponse)
    async def execute(
        payload: RunnerExecutionRequest,
        authorization: Annotated[str | None, Header()] = None,
        protocol: Annotated[str | None, Header(alias="X-Sage-Runner-Protocol")] = None,
    ) -> RunnerExecutionResponse:
        _require_auth(authorization, configured.token)
        if protocol != RUNNER_PROTOCOL_VERSION:
            raise HTTPException(status_code=409, detail="Runner protocol version mismatch")
        if payload.profile != configured.profile:
            return _failure(payload, "profile_mismatch", "Runner profile is unavailable")
        async with app.state.runner_semaphore:
            try:
                result = await _execute(payload, configured)
            except RunnerExecutionFailure as exc:
                return _failure(payload, exc.code, str(exc), status=exc.status)
            return RunnerExecutionResponse(
                execution_id=payload.execution_id,
                request_digest=payload.request_digest,
                status="completed",
                result=result,
            )

    return app


def _require_auth(authorization: str | None, expected_token: str) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not token or not hmac.compare_digest(token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid Runner token")


async def _execute(
    payload: RunnerExecutionRequest,
    settings: RunnerSettings,
) -> dict[str, object]:
    with TemporaryDirectory(prefix="execution-", dir=settings.work_root) as temporary:
        execution_root = Path(temporary).resolve()
        package_archive = _decode_archive(payload.package_archive_base64, settings)
        decoded_inputs = [
            (item, _decode_archive(item.archive_base64, settings))
            for item in payload.inputs
        ]
        if len(package_archive) + sum(len(archive) for _, archive in decoded_inputs) > (
            settings.max_archive_bytes
        ):
            raise RunnerExecutionFailure(
                "archive_too_large",
                "Runner request archives exceed their combined transfer limit",
            )
        package_container = execution_root / "package"
        extracted_bytes = _extract_archive(package_archive, package_container, settings)
        transferred_root = package_container / "content"
        if (
            not transferred_root.is_dir()
            or directory_digest(transferred_root) != payload.package_digest
        ):
            raise RunnerExecutionFailure(
                "package_digest_mismatch",
                "Transferred Skill package digest does not match the request",
            )
        package_root = package_container / payload.skill_name
        transferred_root.replace(package_root)
        try:
            definition = SkillRegistry.load_definition(package_root)
        except (OSError, SkillPackageError, ValueError) as exc:
            raise RunnerExecutionFailure(
                "invalid_package",
                "Transferred Skill package is invalid",
            ) from exc
        binding = definition.manifest.spec.execution
        if (
            definition.name != payload.skill_name
            or definition.version != payload.skill_version
            or binding.type != "python-subprocess"
            or not binding.entrypoint
        ):
            raise RunnerExecutionFailure(
                "package_identity_mismatch",
                "Transferred Skill identity does not match the request",
            )

        arguments = dict(payload.arguments)
        inputs_root = execution_root / "inputs"
        for item, archive in decoded_inputs:
            destination = (inputs_root / item.argument_name).resolve()
            if not destination.is_relative_to(inputs_root.resolve()):
                raise RunnerExecutionFailure("invalid_input_path", "Runner input path is invalid")
            extracted_bytes += _extract_archive(archive, destination, settings)
            if extracted_bytes > settings.max_extracted_bytes:
                raise RunnerExecutionFailure(
                    "archive_too_large",
                    "Runner request exceeds its combined extracted size limit",
                )
            input_path = (destination / item.relative_path).resolve()
            if not input_path.is_relative_to(destination.resolve()) or not input_path.exists():
                raise RunnerExecutionFailure("invalid_input_path", "Runner input path is invalid")
            actual_digest = (
                directory_digest(input_path)
                if item.kind == "directory"
                else file_digest(input_path)
            )
            if actual_digest != item.content_digest:
                raise RunnerExecutionFailure(
                    "input_digest_mismatch",
                    "Transferred input digest does not match the request",
                )
            arguments[item.argument_name] = str(input_path)

        try:
            Draft202012Validator(definition.manifest.spec.input_schema).validate(arguments)
        except ValidationError as exc:
            raise RunnerExecutionFailure(
                "input_schema_mismatch",
                "Runner input does not match the Skill schema",
            ) from exc
        entrypoint = (package_root / binding.entrypoint).resolve()
        if not entrypoint.is_relative_to(package_root) or not entrypoint.is_file():
            raise RunnerExecutionFailure("invalid_entrypoint", "Runner entrypoint is invalid")
        if entrypoint.suffix.casefold() != ".py":
            raise RunnerExecutionFailure("invalid_entrypoint", "Runner only executes Python")

        argv = build_subprocess_argv(binding.arguments, arguments, input_directory=execution_root)
        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(entrypoint),
                    *argv,
                    cwd=str(package_root),
                    env=safe_subprocess_environment(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(entrypoint),
                    *argv,
                    cwd=str(package_root),
                    env=safe_subprocess_environment(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except OSError as exc:
            raise RunnerExecutionFailure(
                "process_start_failed",
                "Runner could not start the Skill process",
                status="failed",
            ) from exc

        try:
            async with asyncio.timeout(payload.timeout_seconds):
                stdout, stderr = await asyncio.gather(
                    _read_limited(process.stdout, payload.max_output_bytes),
                    _read_limited(process.stderr, 16_384),
                )
                return_code = await process.wait()
        except TimeoutError as exc:
            await _terminate(process)
            raise RunnerExecutionFailure(
                "timeout",
                "Skill execution exceeded its timeout",
                status="timeout",
            ) from exc
        except asyncio.CancelledError:
            await _terminate(process)
            raise
        except RunnerExecutionFailure:
            await _terminate(process)
            raise

        if return_code != 0:
            raise RunnerExecutionFailure(
                "process_failed",
                f"Skill process exited with code {return_code}",
                status="failed",
            )
        try:
            result = json.loads(stdout.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerExecutionFailure(
                "invalid_json",
                "Skill process returned invalid JSON",
                status="failed",
            ) from exc
        if not isinstance(result, dict):
            raise RunnerExecutionFailure(
                "invalid_result",
                "Skill process result must be an object",
                status="failed",
            )
        try:
            Draft202012Validator(definition.manifest.spec.output_schema).validate(result)
        except ValidationError as exc:
            raise RunnerExecutionFailure(
                "output_schema_mismatch",
                "Skill process result does not match the output schema",
                status="failed",
            ) from exc
        return result


async def _read_limited(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise RunnerExecutionFailure(
                "output_too_large",
                "Skill process output exceeded its limit",
                status="failed",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name != "nt":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    await process.wait()


def _decode_archive(value: str, settings: RunnerSettings) -> bytes:
    if len(value) > ((settings.max_archive_bytes + 2) // 3) * 4 + 4:
        raise RunnerExecutionFailure("archive_too_large", "Runner archive exceeds its limit")
    try:
        payload = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RunnerExecutionFailure(
            "invalid_archive",
            "Runner archive is not valid base64",
        ) from exc
    if len(payload) > settings.max_archive_bytes:
        raise RunnerExecutionFailure("archive_too_large", "Runner archive exceeds its limit")
    return payload


def _extract_archive(payload: bytes, destination: Path, settings: RunnerSettings) -> int:
    destination.mkdir(parents=True, exist_ok=False)
    names: set[str] = set()
    total = 0
    written = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if not members or len(members) > settings.max_files:
                raise RunnerExecutionFailure(
                    "invalid_archive",
                    "Runner archive has an invalid file count",
                )
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                mode = member.external_attr >> 16
                normalized = path.as_posix().casefold()
                if (
                    path.is_absolute()
                    or not path.parts
                    or ".." in path.parts
                    or any(":" in part for part in path.parts)
                    or stat.S_ISLNK(mode)
                    or member.flag_bits & 0x1
                    or normalized in names
                ):
                    raise RunnerExecutionFailure(
                        "invalid_archive",
                        "Runner archive contains an unsafe path",
                    )
                names.add(normalized)
                total += member.file_size
                if total > settings.max_extracted_bytes:
                    raise RunnerExecutionFailure(
                        "archive_too_large",
                        "Runner archive exceeds its extracted size limit",
                    )
                target = destination.joinpath(*path.parts).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise RunnerExecutionFailure(
                        "invalid_archive",
                        "Runner archive path escapes its extraction root",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(64 * 1024):
                        written += len(chunk)
                        if written > settings.max_extracted_bytes:
                            raise RunnerExecutionFailure(
                                "archive_too_large",
                                "Runner archive exceeds its extracted size limit",
                            )
                        output.write(chunk)
    except RunnerExecutionFailure:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise RunnerExecutionFailure("invalid_archive", "Runner archive is invalid") from exc
    return written


def _failure(
    request: RunnerExecutionRequest,
    code: str,
    message: str,
    *,
    status: str = "rejected",
) -> RunnerExecutionResponse:
    return RunnerExecutionResponse(
        execution_id=request.execution_id,
        request_digest=request.request_digest,
        status=status,  # type: ignore[arg-type]
        error_code=code,
        message=message,
    )
