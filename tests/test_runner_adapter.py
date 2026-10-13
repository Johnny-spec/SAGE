import asyncio
import base64
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from sage.adapters.runner import HttpRunnerClient, RunnerAdapter
from sage.app import DEFAULT_SKILLS_ROOT
from sage.digests import directory_digest, file_digest
from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError
from sage.registry import SkillRegistry
from sage.runner_protocol import RunnerExecutionRequest, RunnerExecutionResponse, RunnerHealth


class RecordingRunnerClient:
    def __init__(self) -> None:
        self.requests: list[RunnerExecutionRequest] = []

    async def health(self) -> RunnerHealth:
        return RunnerHealth(status="ready", profile="python-restricted-v1")

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        self.requests.append(request)
        return RunnerExecutionResponse(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status="completed",
            result={"skill": "iis_logs", "ok": True},
        )


def _context() -> RuntimeContext:
    return RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_runner",
            principal_id="user_runner",
            roles=frozenset({"user"}),
        ),
        generation_id="gen_runner",
    )


def test_runner_adapter_packages_skill_and_managed_path_input(tmp_path: Path) -> None:
    definition = SkillRegistry.load(DEFAULT_SKILLS_ROOT).get("iis-logs")
    assert definition is not None
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    log_path = artifact_root / "sample.log"
    log_path.write_text("#Fields: date time\n", encoding="utf-8")
    client = RecordingRunnerClient()
    adapter = RunnerAdapter(client, allowed_input_roots=(artifact_root,))

    result = asyncio.run(
        adapter.execute(
            definition,
            {"log_path": str(log_path), "filter": "status>=500"},
            context=_context(),
        )
    )

    assert result == {"skill": "iis_logs", "ok": True}
    request = client.requests[0]
    assert request.protocol_version == "sage-runner/v1"
    assert request.generation_id == "gen_runner"
    assert request.workspace_id == "ws_runner"
    assert request.skill_name == "iis-logs"
    assert request.package_digest == directory_digest(definition.package_root)
    assert request.arguments == {"filter": "status>=500"}
    assert len(request.inputs) == 1
    assert request.inputs[0].argument_name == "log_path"
    assert request.inputs[0].content_digest == file_digest(log_path)
    with zipfile.ZipFile(BytesIO(base64.b64decode(request.package_archive_base64))) as archive:
        assert "content/skill.yaml" in archive.namelist()
        assert "content/SKILL.md" in archive.namelist()
    with zipfile.ZipFile(BytesIO(base64.b64decode(request.inputs[0].archive_base64))) as archive:
        assert archive.read("content/sample.log") == log_path.read_bytes()


def test_runner_adapter_rejects_unmanaged_path_input(tmp_path: Path) -> None:
    definition = SkillRegistry.load(DEFAULT_SKILLS_ROOT).get("iis-logs")
    assert definition is not None
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "secret.log"
    outside.write_text("secret", encoding="utf-8")
    adapter = RunnerAdapter(RecordingRunnerClient(), allowed_input_roots=(managed,))

    with pytest.raises(ExecutionError, match="outside managed Artifact storage"):
        asyncio.run(
            adapter.execute(
                definition,
                {"log_path": str(outside)},
                context=_context(),
            )
        )


def test_http_runner_client_authenticates_and_checks_response_identity() -> None:
    observed_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed_headers.update(request.headers)
        body = RunnerExecutionRequest.model_validate_json(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": "sage-runner/v1",
                "execution_id": body.execution_id,
                "request_digest": body.request_digest,
                "status": "completed",
                "result": {"ok": True},
            },
        )

    unsigned = RunnerExecutionRequest.model_construct(
        execution_id="rex_1",
        run_id="run_1",
        workspace_id="ws_1",
        generation_id="gen_1",
        skill_name="sample",
        skill_version="1.0.0",
        package_digest="sha256:" + "1" * 64,
        package_archive_base64="eA==",
        arguments={},
        inputs=(),
        timeout_seconds=10,
        max_output_bytes=1024,
        profile="python-restricted-v1",
        protocol_version="sage-runner/v1",
        request_digest="sha256:" + "0" * 64,
    )
    from sage.runner_protocol import runner_request_digest

    request = RunnerExecutionRequest(
        **unsigned.model_dump(exclude={"request_digest"}),
        request_digest=runner_request_digest(unsigned),
    )
    client = HttpRunnerClient(
        "http://runner.internal",
        token="runner-secret",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(client.execute(request))

    assert response.result == {"ok": True}
    assert observed_headers["authorization"] == "Bearer runner-secret"
    assert observed_headers["x-sage-runner-protocol"] == "sage-runner/v1"


def test_http_runner_client_negotiates_health_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "protocol_version": "sage-runner/v1",
                "profile": "python-restricted-v1",
            },
        )

    client = HttpRunnerClient(
        "http://runner.internal",
        token="runner-secret",
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(client.health())

    assert health.profile == "python-restricted-v1"


def test_http_runner_client_rejects_incompatible_health_protocol() -> None:
    client = HttpRunnerClient(
        "http://runner.internal",
        token="runner-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "ready",
                    "protocol_version": "sage-runner/v2",
                    "profile": "python-restricted-v1",
                },
            )
        ),
    )

    with pytest.raises(ExecutionError, match="readiness check failed"):
        asyncio.run(client.health())


def test_runner_request_rejects_tampered_digest() -> None:
    with pytest.raises(ValidationError, match="digest mismatch"):
        RunnerExecutionRequest(
            execution_id="rex_1",
            run_id="run_1",
            workspace_id="ws_1",
            generation_id="gen_1",
            skill_name="sample",
            skill_version="1.0.0",
            package_digest="sha256:" + "1" * 64,
            package_archive_base64="eA==",
            arguments={},
            timeout_seconds=10,
            max_output_bytes=1024,
            request_digest="sha256:" + "0" * 64,
        )
