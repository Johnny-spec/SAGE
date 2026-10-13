import asyncio
import base64
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sage.adapters.runner import RunnerAdapter, RunnerClient, _archive_path
from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_settings import ControlPlaneSettings
from sage.domain import Principal, RuntimeContext
from sage.errors import ExecutionError
from sage.registry import SkillRegistry
from sage.runner_app import RunnerSettings, _execute, create_runner_app
from sage.runner_protocol import RunnerExecutionRequest, RunnerExecutionResponse, RunnerHealth


class CapturingRunnerClient(RunnerClient):
    def __init__(self) -> None:
        self.request: RunnerExecutionRequest | None = None

    async def health(self) -> RunnerHealth:
        return RunnerHealth(status="ready", profile="python-restricted-v1")

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        self.request = request
        return RunnerExecutionResponse(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status="completed",
            result={"text": "captured"},
        )


class InProcessRunnerClient(RunnerClient):
    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings
        self.requests: list[RunnerExecutionRequest] = []

    async def health(self) -> RunnerHealth:
        return RunnerHealth(status="ready", profile=self.settings.profile)

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        self.requests.append(request)
        self.settings.work_root.mkdir(parents=True, exist_ok=True)
        result = await _execute(request, self.settings)
        return RunnerExecutionResponse(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status="completed",
            result=result,
        )


class UnhealthyRunnerClient(RunnerClient):
    def __init__(self, *, profile: str | None = None) -> None:
        self.profile = profile

    async def health(self) -> RunnerHealth:
        if self.profile is None:
            raise ExecutionError("Runner is offline")
        return RunnerHealth(status="ready", profile=self.profile)

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        raise AssertionError(f"Unexpected execution: {request.execution_id}")


def _skill(root: Path, *, sleep: bool = False) -> Path:
    package = root / "runner-echo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: runner-echo\ndescription: Runner test Skill.\n---\n",
        encoding="utf-8",
    )
    (package / "skill.yaml").write_text(
        """apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata: {name: runner-echo, version: 1.0.0}
spec:
  execution:
    type: python-subprocess
    entrypoint: main.py
    arguments:
      - {name: document_path, position: 0}
      - {name: prefix, option: --prefix}
  inputSchema:
    type: object
    required: [document_path, prefix]
    additionalProperties: false
    properties:
      document_path: {type: string}
      prefix: {type: string}
  outputSchema:
    type: object
    required: [text]
    additionalProperties: false
    properties:
      text: {type: string}
  policy:
    risk: read-only
    timeoutSeconds: 2
    maxOutputBytes: 4096
""",
        encoding="utf-8",
    )
    script = (
        "import time\ntime.sleep(5)\nprint('{}')\n"
        if sleep
        else """import argparse
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("document_path")
parser.add_argument("--prefix", required=True)
args = parser.parse_args()
print(json.dumps({"text": args.prefix + Path(args.document_path).read_text(encoding="utf-8")}))
"""
    )
    (package / "main.py").write_text(script, encoding="utf-8")
    return package


def _runner_request(package: Path, artifact_root: Path) -> RunnerExecutionRequest:
    definition = SkillRegistry.load_definition(package)
    document = artifact_root / "document.txt"
    document.write_text("runner payload", encoding="utf-8")
    capture = CapturingRunnerClient()
    adapter = RunnerAdapter(capture, allowed_input_roots=(artifact_root,))
    asyncio.run(
        adapter.execute(
            definition,
            {"document_path": str(document), "prefix": "verified: "},
            context=RuntimeContext.create(
                mode="direct",
                principal=Principal.anonymous(),
                generation_id="gen_runner",
            ),
        )
    )
    assert capture.request is not None
    return capture.request


def test_runner_executes_transferred_package_and_input(tmp_path: Path) -> None:
    package = _skill(tmp_path / "skills")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request = _runner_request(package, artifact_root)
    app = create_runner_app(
        RunnerSettings(token="runner-secret-" + "x" * 32, work_root=tmp_path / "work")
    )

    with TestClient(app) as client:
        unauthorized = client.post(
            "/v1/execute",
            headers={"X-Sage-Runner-Protocol": "sage-runner/v1"},
            json=request.model_dump(mode="json"),
        )
        response = client.post(
            "/v1/execute",
            headers={
                "Authorization": "Bearer runner-secret-" + "x" * 32,
                "X-Sage-Runner-Protocol": "sage-runner/v1",
            },
            json=request.model_dump(mode="json"),
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["status"] == "completed", response.text
    assert response.json()["result"] == {"text": "verified: runner payload"}
    assert list((tmp_path / "work").iterdir()) == []


def test_runner_rejects_tampered_package_without_execution(tmp_path: Path) -> None:
    package = _skill(tmp_path / "skills")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request = _runner_request(package, artifact_root)
    (package / "main.py").write_text("print('tampered')\n", encoding="utf-8")
    tampered = request.model_copy(
        update={
            "package_archive_base64": base64.b64encode(
                _archive_path(package, max_bytes=32 * 1024 * 1024)
            ).decode("ascii")
        }
    )
    app = create_runner_app(
        RunnerSettings(token="runner-secret-" + "x" * 32, work_root=tmp_path / "work")
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/execute",
            headers={
                "Authorization": "Bearer runner-secret-" + "x" * 32,
                "X-Sage-Runner-Protocol": "sage-runner/v1",
            },
            json=tampered.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["error_code"] == "package_digest_mismatch"


def test_runner_rejects_archive_path_traversal(tmp_path: Path) -> None:
    package = _skill(tmp_path / "skills")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request = _runner_request(package, artifact_root)
    unsafe = BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.py", "print('escaped')")
    tampered = request.model_copy(
        update={"package_archive_base64": base64.b64encode(unsafe.getvalue()).decode("ascii")}
    )
    app = create_runner_app(
        RunnerSettings(token="runner-secret-" + "x" * 32, work_root=tmp_path / "work")
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/execute",
            headers={
                "Authorization": "Bearer runner-secret-" + "x" * 32,
                "X-Sage-Runner-Protocol": "sage-runner/v1",
            },
            json=tampered.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["error_code"] == "invalid_archive"
    assert not (tmp_path / "escape.py").exists()


def test_runner_terminates_timed_out_process(tmp_path: Path) -> None:
    package = _skill(tmp_path / "skills", sleep=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request = _runner_request(package, artifact_root).model_copy(
        update={"timeout_seconds": 1}
    )
    values = request.model_dump(exclude={"request_digest"})
    from sage.runner_protocol import runner_request_digest

    unsigned = RunnerExecutionRequest.model_construct(
        **values,
        request_digest="sha256:" + "0" * 64,
    )
    request = RunnerExecutionRequest(
        **values,
        request_digest=runner_request_digest(unsigned),
    )
    app = create_runner_app(
        RunnerSettings(token="runner-secret-" + "x" * 32, work_root=tmp_path / "work")
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/execute",
            headers={
                "Authorization": "Bearer runner-secret-" + "x" * 32,
                "X-Sage-Runner-Protocol": "sage-runner/v1",
            },
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "timeout"
    assert response.json()["error_code"] == "timeout"


def test_sage_routes_uploaded_iis_artifact_through_runner(tmp_path: Path) -> None:
    runner = InProcessRunnerClient(
        RunnerSettings(
            token="runner-secret-" + "x" * 32,
            work_root=tmp_path / "runner-work",
        )
    )
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
            require_content_scanning=False,
            require_signed_solutions=False,
        ),
        runner_client=runner,
    )
    fixture = Path(__file__).parent / "fixtures" / "iis" / "sample.log"

    with TestClient(app) as client:
        health = client.get("/health/ready")
        uploaded = client.post(
            "/v1/artifacts",
            files={"file": ("sample.log", fixture.read_bytes(), "text/plain")},
        )
        response = client.post(
            "/v1/messages",
            json={
                "message": "Analyze this IIS log",
                "skill": "iis-logs",
                "artifact_ids": [uploaded.json()["artifact_id"]],
            },
        )

    assert health.status_code == 200
    assert health.json()["runner"] == "ready"
    assert response.status_code == 200, response.text
    assert response.json()["result"]["raw"]["metrics"]["total_requests"] == 5
    assert len(runner.requests) == 1
    assert runner.requests[0].skill_name == "iis-logs"
    assert runner.requests[0].inputs[0].argument_name == "log_path"
    assert list((tmp_path / "runner-work").iterdir()) == []


def test_production_without_runner_hides_python_skills(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path / "state",
            environment="production",
            admin_auth_enabled=True,
            require_content_scanning=False,
            require_signed_solutions=False,
        ),
    )

    with TestClient(app) as client:
        health = client.get("/health/ready")
        capabilities = client.get("/v1/capabilities")
        invocation = client.post(
            "/v1/messages",
            json={"message": "analyze", "skill": "iis-logs"},
        )

    assert health.status_code == 200
    assert health.json()["runner"] == "disabled"
    assert capabilities.status_code == 200
    ids = {item["capability_id"] for item in capabilities.json()}
    assert "skill:echo" in ids
    assert "skill:iis-logs" not in ids
    assert "skill:netlog" not in ids
    assert invocation.status_code == 404


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (UnhealthyRunnerClient(), "Runner is offline"),
        (UnhealthyRunnerClient(profile="other-profile"), "Runner profile mismatch"),
    ],
)
def test_api_readiness_fails_for_unavailable_or_incompatible_runner(
    tmp_path: Path,
    runner: RunnerClient,
    message: str,
) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
        runner_client=runner,
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert message in response.json()["detail"]
