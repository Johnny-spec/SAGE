import asyncio
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_settings import ControlPlaneSettings
from sage.domain import Principal, RuntimeContext
from sage.settings import AgentModelSettings

IIS_FIXTURE = Path(__file__).parent / "fixtures" / "iis" / "sample.log"
NETLOG_FIXTURE = Path(__file__).parent / "fixtures" / "netlog" / "auth_failure.json"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(
        create_app(
            DEFAULT_SKILLS_ROOT,
            agent_model=DemoAgentModel(),
            agent_model_name="sage-demo-model",
            agent_settings=AgentModelSettings(env_file_present=True),
        )
    ) as test_client:
        yield test_client


def test_rule_routes_message_to_echo_skill(client: TestClient) -> None:
    response = client.post("/v1/messages", json={"message": "请回显 hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["run_id"].startswith("run_")
    assert body["thread_id"].startswith("thr_")
    assert body["route"]["skill"] == "echo"
    assert body["route"]["method"] == "rule"
    assert body["result"] == {"text": "请回显 hello"}
    assert body["usage"]["model_calls"] == 0


def test_readiness_reports_generation_and_runtime_providers(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "skills": 8,
        "solutions": 0,
        "generation_id": "gen_startup",
        "artifact_backend": "local",
        "durable": False,
        "persistent_checkpoints": False,
        "runner": "local-subprocess",
    }


def test_sync_run_lifecycle_is_recorded(client: TestClient) -> None:
    response = client.post("/v1/messages", json={"message": "echo recorded"})
    run_id = response.json()["run_id"]

    snapshot = asyncio.run(client.app.state.run_store.get(run_id))

    assert snapshot is not None
    assert snapshot.status == "completed"
    assert [event.name for event in snapshot.events] == [
        "run.started",
        "result.available",
        "run.completed",
    ]
    assert snapshot.context.principal.principal_id == "anonymous"

    run_response = client.get(f"/v1/runs/{run_id}")
    replay = client.get(f"/v1/runs/{run_id}/events?after=1")

    assert run_response.status_code == 200
    assert run_response.json()["status"] == "completed"
    assert run_response.json()["event_count"] == 3
    assert [event["sequence"] for event in replay.json()] == [2, 3]
    assert [event["name"] for event in replay.json()] == [
        "result.available",
        "run.completed",
    ]
    result = client.get(f"/v1/runs/{run_id}/result").json()
    assert replay.json()[0]["data"]["result_digest"] == result["result_digest"]
    assert replay.json()[0]["data"]["links"]["result"] == f"/v1/runs/{run_id}/result"


def test_run_event_stream_replays_persisted_events_with_stable_ids(
    client: TestClient,
) -> None:
    response = client.post("/v1/messages", json={"message": "echo stream replay"})
    run_id = response.json()["run_id"]

    with client.stream("GET", f"/v1/runs/{run_id}/events/stream") as stream:
        body = "".join(stream.iter_text())

    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "id: 1" in body
    assert "id: 2" in body
    assert "event: run.started" in body
    assert "event: run.completed" in body
    assert f'"event_id": "{run_id}:2"' in body
    assert '"schema_version": "1.0"' in body
    assert '"category": "lifecycle"' in body


def test_run_event_stream_resumes_after_last_event_id(client: TestClient) -> None:
    response = client.post("/v1/messages", json={"message": "echo stream resume"})
    run_id = response.json()["run_id"]

    with client.stream(
        "GET",
        f"/v1/runs/{run_id}/events/stream",
        headers={"Last-Event-ID": "1"},
    ) as stream:
        body = "".join(stream.iter_text())

    assert stream.status_code == 200
    assert "id: 1" not in body
    assert "event: run.started" not in body
    assert "id: 2" in body
    assert body.count("event: run.completed") == 1


def test_run_event_stream_follows_active_run_until_terminal(client: TestClient) -> None:
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        generation_id="gen_startup",
    )
    assert client.portal is not None
    client.portal.call(client.app.state.run_store.begin, context)

    async def complete_run() -> None:
        await asyncio.sleep(0.2)
        await client.app.state.run_store.append(
            context.run_id,
            "skill.completed",
            {"skill": "echo"},
        )
        await client.app.state.run_store.append(
            context.run_id,
            "run.completed",
            {
                "response": {
                    "status": "completed",
                    "message": "done",
                    "result": {"text": "done"},
                    "usage": {},
                }
            },
        )

    completion = client.portal.start_task_soon(complete_run)
    with client.stream(
        "GET",
        f"/v1/runs/{context.run_id}/events/stream",
        headers={"Last-Event-ID": "1"},
    ) as stream:
        body = "".join(stream.iter_text())
    completion.result()

    assert stream.status_code == 200
    assert "event: run.started" not in body
    assert "event: skill.completed" in body
    assert "event: run.completed" in body
    assert "id: 2" in body
    assert "id: 3" in body


@pytest.mark.parametrize(
    ("url_suffix", "headers", "message"),
    [
        ("?after_sequence=-1", {}, "after_sequence must be non-negative"),
        ("", {"Last-Event-ID": "invalid"}, "Last-Event-ID must be a non-negative"),
    ],
)
def test_run_event_stream_rejects_invalid_resume_cursor(
    client: TestClient,
    url_suffix: str,
    headers: dict[str, str],
    message: str,
) -> None:
    response = client.post("/v1/messages", json={"message": "echo invalid cursor"})
    run_id = response.json()["run_id"]

    stream = client.get(
        f"/v1/runs/{run_id}/events/stream{url_suffix}",
        headers=headers,
    )

    assert stream.status_code == 400
    assert message in stream.json()["detail"]


def test_completed_run_exposes_stable_result_view(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message": "ignored",
            "skill": "echo",
            "arguments": {"text": "stable result"},
        },
    )
    run_id = response.json()["run_id"]

    first = client.get(f"/v1/runs/{run_id}/result")
    repeated = client.get(f"/v1/runs/{run_id}/result")

    assert first.status_code == 200
    assert repeated.json() == first.json()
    result = first.json()
    assert result["schema_version"] == "1.0"
    assert result["run_id"] == run_id
    assert result["generation_id"] == "gen_startup"
    assert result["capability_id"] == "skill:echo"
    assert result["input_digest"].startswith("sha256:")
    assert result["data"] == {"text": "stable result"}
    assert [block["block_id"] for block in result["blocks"]] == ["answer", "result"]
    assert all(block["content_digest"].startswith("sha256:") for block in result["blocks"])
    assert result["result_digest"].startswith("sha256:")


def test_completed_run_accepts_idempotent_block_feedback(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={"message": "ignored", "skill": "echo", "arguments": {"text": "review me"}},
    )
    run_id = response.json()["run_id"]
    result = client.get(f"/v1/runs/{run_id}/result").json()
    block = next(item for item in result["blocks"] if item["block_id"] == "result")
    payload = {
        "rating": 3,
        "categories": ["incomplete"],
        "comment": "The response needs more context.",
        "annotations": [
            {
                "block_id": block["block_id"],
                "block_revision": block["revision"],
                "target_digest": block["content_digest"],
                "comment": "Explain this value.",
            }
        ],
        "expected_action": "evaluation-candidate",
        "evaluation_use_allowed": True,
    }

    first = client.post(
        f"/v1/runs/{run_id}/feedback",
        headers={"Idempotency-Key": "feedback-1"},
        json=payload,
    )
    repeated = client.post(
        f"/v1/runs/{run_id}/feedback",
        headers={"Idempotency-Key": "feedback-1"},
        json=payload,
    )
    changed = client.post(
        f"/v1/runs/{run_id}/feedback",
        headers={"Idempotency-Key": "feedback-1"},
        json={**payload, "rating": 1},
    )
    listed = client.get(f"/v1/runs/{run_id}/feedback")

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert changed.status_code == 409
    assert first.json()["result_digest"] == result["result_digest"]
    assert first.json()["generation_id"] == result["generation_id"]
    assert first.json()["annotations"][0]["target_digest"] == block["content_digest"]
    assert listed.status_code == 200
    assert [item["feedback_id"] for item in listed.json()] == [first.json()["feedback_id"]]


def test_run_feedback_rejects_changed_block_digest(client: TestClient) -> None:
    response = client.post("/v1/messages", json={"message": "echo feedback target"})
    run_id = response.json()["run_id"]

    feedback = client.post(
        f"/v1/runs/{run_id}/feedback",
        headers={"Idempotency-Key": "feedback-invalid-target"},
        json={
            "annotations": [
                {
                    "block_id": "result",
                    "block_revision": 1,
                    "target_digest": "sha256:" + "0" * 64,
                    "comment": "This target is stale.",
                }
            ]
        },
    )

    assert feedback.status_code == 422
    assert feedback.json()["detail"] == "Result block content changed: result"


def test_running_run_has_no_result_and_rejects_feedback_without_idempotency_key(
    client: TestClient,
) -> None:
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        generation_id="gen_startup",
    )
    asyncio.run(client.app.state.run_store.begin(context))

    result = client.get(f"/v1/runs/{context.run_id}/result")
    feedback = client.post(
        f"/v1/runs/{context.run_id}/feedback",
        json={"rating": 3},
    )

    assert result.status_code == 409
    assert result.json()["detail"] == "Run result is not complete: running"
    assert feedback.status_code == 422
    assert "Idempotency-Key is required" in feedback.json()["detail"]


def test_agent_invocation_creates_idempotent_child_run(client: TestClient) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    active = client.app.state.runtime_manager.active
    parent_context = RuntimeContext.create(
        mode="agent",
        lifecycle="durable",
        principal=Principal.anonymous(),
        generation_id=active.generation.generation_id,
    )
    asyncio.run(client.app.state.run_store.begin(parent_context))
    payload = {
        "parent_run_id": parent_context.run_id,
        "agent_id": active.agent_catalog.default_agent_id,
        "goal": "Return a concise status without using tools.",
        "input": {"request_id": "req_1"},
        "idempotency_key": "status-step",
    }

    assert client.post("/v1/agent-invocations", json=payload).status_code == 503
    active.agent_runtimes._checkpointer = InMemorySaver()
    first = client.post("/v1/agent-invocations", json=payload)
    replay = client.post("/v1/agent-invocations", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert first.json() == replay.json()
    child = asyncio.run(client.app.state.run_store.get(first.json()["run_id"]))
    parent = asyncio.run(client.app.state.run_store.get(parent_context.run_id))
    assert child is not None
    assert child.context.parent_run_id == parent_context.run_id
    assert child.context.root_run_id == parent_context.run_id
    assert child.context.lifecycle == "durable"
    assert child.context.generation_id == parent_context.generation_id
    assert parent is not None
    assert [event.name for event in parent.events] == [
        "run.started",
        "agent.handoff.started",
        "agent.handoff.completed",
    ]


def test_explicit_skill_uses_supplied_arguments(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={"message": "ignored", "skill": "echo", "arguments": {"text": "exact"}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"text": "exact"}
    assert response.json()["route"]["method"] == "explicit"


def test_direct_invocation_is_idempotent_and_rejects_changed_input(
    client: TestClient,
) -> None:
    payload = {
        "capability_id": "skill:echo",
        "input": {"text": "exact"},
        "idempotency_key": "request-42",
    }

    first = client.post("/v1/invocations", json=payload)
    repeated = client.post("/v1/invocations", json=payload)
    changed = client.post(
        "/v1/invocations",
        json={**payload, "input": {"text": "changed"}},
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert first.json()["output"] == {"text": "exact"}
    assert first.json()["usage"]["model_calls"] == 0
    assert changed.status_code == 409
    assert "different invocation" in changed.json()["detail"]


def test_report_summary_skill_consumes_structured_analysis(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message": "build report",
            "skill": "report-summary",
            "arguments": {
                "analysis": {
                    "findings": [{"title": "Slow request"}],
                    "recommendations": ["Inspect the slow endpoint"],
                    "raw": {"metrics": {"total_requests": 5}},
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["result"]["request_count"] == 5
    assert response.json()["result"]["finding_count"] == 1
    assert response.json()["result"]["status"] == "attention"


def test_unknown_intent_requests_clarification(client: TestClient) -> None:
    response = client.post("/v1/messages", json={"message": "What is the weather?"})

    assert response.status_code == 200
    assert response.json()["status"] == "clarification_required"
    assert response.json()["route"] is None


def test_unknown_explicit_skill_is_not_executed(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={"message": "run it", "skill": "missing"},
    )

    assert response.status_code == 404


def test_explicit_skills_collection_route_runs_safe_manager_wrapper(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message": "Use SkillsCollection diagnostic manager for an ambiguous incident",
            "skill": "skills-collection",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"]["skill"] == "skills-collection"
    assert body["route"]["method"] == "explicit"
    assert body["result"]["skill"] == "orchestrator"
    assert body["result"]["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert body["result"]["raw"]["routing"]["ambiguous"] is True


def test_explicit_skills_collection_child_route_runs_edge_qa(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message": "How can I troubleshoot Microsoft Edge startup behavior?",
            "skill": "skills-collection",
            "arguments": {"product": "Edge", "child_skill": "edge_qa"},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["raw"]["products_run"] == ["Edge"]
    nodes = result["raw"]["manager_report"]["invocation_chain"]["nodes"]
    edge_qa = next(node for node in nodes if node["skill"] == "edge_qa")
    assert edge_qa["status"] == "completed"


def test_upload_routes_and_executes_vendored_iis_skill(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={"file": ("sample.log", IIS_FIXTURE.read_bytes(), "text/plain")},
    )
    assert upload.status_code == 201
    attachment = upload.json()
    assert attachment["media_type"] == "text/iis-w3c"

    response = client.post(
        "/v1/messages",
        json={
            "message": "请分析这个 IIS 日志",
            "attachment_ids": [attachment["attachment_id"]],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"]["skill"] == "iis-logs"
    assert body["result"]["skill"] == "iis_logs"
    assert body["result"]["raw"]["metrics"]["total_requests"] == 5
    assert body["usage"]["model_calls"] == 0


def test_upload_routes_and_executes_vendored_netlog_skill(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={
            "file": (
                "chrome-net-export-log.json",
                NETLOG_FIXTURE.read_bytes(),
                "application/json",
            )
        },
    )
    assert upload.status_code == 201
    attachment = upload.json()
    assert attachment["media_type"] == "application/x-chromium-netlog"

    response = client.post(
        "/v1/messages",
        json={
            "message": "分析这个 netlog 的认证失败原因",
            "attachment_ids": [attachment["attachment_id"]],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"]["skill"] == "netlog"
    assert body["result"]["skill"] == "netlog"
    assert body["result"]["raw"]["per_file"][0]["requests_total"] == 1
    assert "auth_error" in body["result"]["raw"]["observed_problem_types"]


def test_iis_skill_rejects_netlog_attachment(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={"file": ("netlog.json", NETLOG_FIXTURE.read_bytes(), "application/json")},
    )

    response = client.post(
        "/v1/messages",
        json={
            "message": "incorrect explicit route",
            "skill": "iis-logs",
            "attachment_ids": [upload.json()["attachment_id"]],
        },
    )

    assert response.status_code == 422
    assert "not compatible" in response.json()["detail"]


def test_iis_skill_rejects_unstructured_text(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={"file": ("not-iis.log", b"this is not a W3C log", "text/plain")},
    )

    response = client.post(
        "/v1/messages",
        json={
            "message": "analyze IIS log",
            "skill": "iis-logs",
            "attachment_ids": [upload.json()["attachment_id"]],
        },
    )

    assert response.status_code == 422
    assert "not compatible" in response.json()["detail"]


def test_upload_accepts_log_larger_than_legacy_two_megabyte_limit(
    client: TestClient,
) -> None:
    content = IIS_FIXTURE.read_bytes() + b"x" * (2 * 1024 * 1024)

    response = client.post(
        "/v1/attachments",
        files={"file": ("large.log", content, "text/plain")},
    )

    assert response.status_code == 201
    assert response.json()["size"] == len(content)
    assert response.json()["media_type"] == "text/iis-w3c"


def test_upload_accepts_w3c_text_with_rotated_suffix(client: TestClient) -> None:
    response = client.post(
        "/v1/attachments",
        files={"file": ("u_ex.log.1", IIS_FIXTURE.read_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 201
    assert response.json()["media_type"] == "text/iis-w3c"


def test_upload_and_execute_iis_zip_bundle(client: TestClient) -> None:
    bundle = BytesIO()
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.writestr("logs/sample.log", IIS_FIXTURE.read_bytes())

    upload = client.post(
        "/v1/attachments",
        files={"file": ("iis-logs.zip", bundle.getvalue(), "application/zip")},
    )

    assert upload.status_code == 201
    attachment = upload.json()
    assert attachment["media_type"] == "application/vnd.sage.iis-w3c-bundle"

    response = client.post(
        "/v1/messages",
        json={"message": "analyze these logs", "attachment_ids": [attachment["attachment_id"]]},
    )
    assert response.status_code == 200
    assert response.json()["route"]["skill"] == "iis-logs"
    assert response.json()["result"]["raw"]["metrics"]["total_requests"] == 5


def test_upload_rejects_unsafe_zip_member(client: TestClient) -> None:
    bundle = BytesIO()
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../sample.log", IIS_FIXTURE.read_bytes())

    response = client.post(
        "/v1/attachments",
        files={"file": ("unsafe.zip", bundle.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "unsafe path" in response.json()["detail"]


def test_upload_rejects_unknown_binary_content(client: TestClient) -> None:
    response = client.post(
        "/v1/attachments",
        files={"file": ("trace.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "Unsupported attachment content" in response.json()["detail"]


def test_upload_accepts_pdf_as_opaque_artifact(client: TestClient) -> None:
    response = client.post(
        "/v1/artifacts",
        files={"file": ("contract.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
    )

    assert response.status_code == 201
    assert response.json()["media_type"] == "application/pdf"
    assert response.json()["artifact_id"].startswith("art_")


def test_upload_accepts_safe_generic_zip_without_extracting_it(client: TestClient) -> None:
    bundle = BytesIO()
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.writestr("document.txt", "not an IIS log")

    response = client.post(
        "/v1/artifacts",
        files={"file": ("documents.zip", bundle.getvalue(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["media_type"] == "application/zip"


def test_upload_recognizes_docx_container(client: TestClient) -> None:
    document = BytesIO()
    with ZipFile(document, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")

    response = client.post(
        "/v1/artifacts",
        files={
            "file": (
                "contract.docx",
                document.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 201
    assert response.json()["media_type"].endswith("wordprocessingml.document")


def test_iis_skill_requires_uploaded_attachment(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={"message": "analyze IIS logs", "skill": "iis-logs"},
    )

    assert response.status_code == 422
    assert "uploaded attachment" in response.json()["detail"]


def test_client_cannot_supply_host_log_path(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message": "analyze IIS logs",
            "skill": "iis-logs",
            "arguments": {"log_path": "C:\\Windows\\secret.log"},
        },
    )

    assert response.status_code == 422
    assert "filesystem paths" in response.json()["detail"]


def test_unknown_attachment_id_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={"message": "analyze this", "attachment_ids": ["not-real"]},
    )

    assert response.status_code == 404


def test_chat_page_is_served(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "SAGE" in response.text
    assert 'id="skillOverview"' in response.text
    assert 'id="agentSelect"' in response.text
    assert 'id="argumentsInput"' in response.text
    assert 'href="/admin"' in response.text
    assert "/static/app.js?v=20260911-evidence-blocks" in response.text


def test_admin_page_exposes_quality_workspace(client: TestClient) -> None:
    response = client.get("/admin")

    assert response.status_code == 200
    assert 'data-view="quality"' in response.text
    assert 'data-view-panel="quality"' in response.text
    assert 'id="qualityFeedbackList"' in response.text
    assert 'id="qualitySampleSetForm"' in response.text
    assert 'id="qualityComparisonForm"' in response.text
    assert '<a class="nav-item" href="/solution-api">Solution API</a>' in response.text
    assert "/static/admin.js?v=20260911-quality-v2" in response.text


def test_solution_api_page_exposes_http_and_sse_client(client: TestClient) -> None:
    response = client.get("/solution-api")

    assert response.status_code == 200
    assert 'id="solutionSelect"' in response.text
    assert 'id="sendHttp"' in response.text
    assert 'id="sendSse"' in response.text
    assert "/v1/messages/stream" in response.text
    assert "/static/solution-api.js" in response.text


def test_configured_cors_origin_allows_message_preflight(tmp_path: Path) -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(
            data_root=tmp_path,
            cors_allowed_origins=("https://client.example.com",),
        ),
    )

    with TestClient(app) as client:
        response = client.options(
            "/v1/messages",
            headers={
                "Origin": "https://client.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://client.example.com"
    assert "authorization" in response.headers["access-control-allow-headers"].casefold()


def test_lists_discovered_skills_for_route_selection(client: TestClient) -> None:
    response = client.get("/v1/skills")

    assert response.status_code == 200
    skills = {item["name"]: item for item in response.json()}
    assert set(skills) == {
        "contract-review",
        "echo",
        "enterprise-qa",
        "iis-logs",
        "netlog",
        "news-intelligence",
        "report-summary",
        "skills-collection",
    }
    assert skills["iis-logs"]["risk"] == "read-only"


def test_lists_hierarchical_skills_collection_routes(client: TestClient) -> None:
    response = client.get("/v1/routes")

    assert response.status_code == 200
    routes = {route["id"]: route for route in response.json()}
    netlog = routes["skills-collection/iis/netlog"]
    assert netlog["label"] == "SkillsCollection - IIS - netlog"
    assert netlog["target_skill"] == "skills-collection"
    assert netlog["arguments"] == {"product": "IIS", "child_skill": "netlog"}
    assert netlog["selectable"] is True
    assert routes["skills-collection/iis/security_audit"]["selectable"] is False
    assert routes["skills-collection/common/common_validator"]["availability"] == "internal"


def test_agent_status_exposes_runtime_model_and_memory(client: TestClient) -> None:
    response = client.get("/v1/agent")

    assert response.status_code == 200
    assert response.json() == {
        "agent_id": "diagnostic-agent",
        "version": "1.1.0",
        "runtime": "deepagents",
        "model": "sage-demo-model",
        "subagents": ["incident-analyst"],
        "capabilities": [
            "skill:echo",
            "skill:iis-logs",
            "skill:netlog",
            "skill:report-summary",
            "skill:skills-collection",
        ],
        "memory": "thread",
        "configuration": {
            "env_file_present": True,
            "authentication": "offline",
            "token_scope": None,
            "temperature": 0.0,
            "max_tokens": 4096,
            "timeout_seconds": 120.0,
            "max_retries": 2,
            "recursion_limit": 40,
        },
    }


def test_lists_published_agents(client: TestClient) -> None:
    response = client.get("/v1/agents")

    assert response.status_code == 200
    assert response.json() == [
        {
            "agent_id": "diagnostic-agent",
            "name": "sage-coordinator",
            "version": "1.1.0",
            "subagents": ["incident-analyst"],
            "capabilities": [
                "skill:echo",
                "skill:iis-logs",
                "skill:netlog",
                "skill:report-summary",
                "skill:skills-collection",
            ],
            "memory": "thread",
            "max_steps": 40,
            "max_model_calls": 100,
            "max_tool_calls": 100,
            "max_subagent_calls": 20,
        }
    ]


def test_agent_request_rejects_unknown_agent(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "agent_id": "draft-agent",
            "message": "run unpublished agent",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent not found: draft-agent"


def test_agent_status_reports_injected_provider_model() -> None:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(model_name="provider-stub"),
        agent_model_name="azure_openai:test-deployment",
        agent_settings=AgentModelSettings(
            model="azure_openai:test-deployment",
            azure_openai_auth="entra-id",
            azure_openai_endpoint="https://example.openai.azure.com/openai/v1",
        ),
    )

    with TestClient(app) as provider_client:
        response = provider_client.get("/v1/agent")

    assert response.status_code == 200
    assert response.json()["model"] == "azure_openai:test-deployment"
    assert response.json()["configuration"]["authentication"] == "entra-id"


def test_post_sse_stream_exposes_route_and_one_terminal_event(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/messages/stream",
        json={"message": "echo streaming"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 1" in body
    assert "id: 2" in body
    assert "event: run.started" in body
    assert "event: route.selected" in body
    assert "event: skill.started" in body
    assert "event: skill.completed" in body
    assert body.count("event: result.available") == 1
    assert body.count("event: run.completed") == 1
    assert "event: run.failed" not in body
    assert '"skill": "echo"' in body


def test_agent_sse_plans_delegates_and_runs_multiple_skills(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={"file": ("sample.log", IIS_FIXTURE.read_bytes(), "text/plain")},
    )

    with client.stream(
        "POST",
        "/v1/messages/stream",
        json={
            "mode": "agent",
            "message": "Use a subagent to analyze this IIS log and create a concise report",
            "thread_id": "api-agent-demo",
            "attachment_ids": [upload.json()["attachment_id"]],
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: agent.started" in body
    assert "event: plan.updated" in body
    assert body.count("event: skill.loading") == 3
    assert body.count("event: skill.loaded") == 2
    assert body.count("event: skill.resource.loaded") == 1
    assert "/iis-logs/references/triage.md" in body
    assert "event: subagent.started" in body
    assert "event: subagent.completed" in body
    assert body.count("event: tool.started") == 2
    assert '"skills_used": ["iis-logs", "report-summary"]' in body
    assert '"subagent_calls": 1' in body
    assert '"model_calls": 0' not in body
    assert body.count("event: run.completed") == 1
    assert "event: run.failed" not in body


def test_agent_plan_is_queryable_after_completion(client: TestClient) -> None:
    upload = client.post(
        "/v1/artifacts",
        files={"file": ("sample.log", IIS_FIXTURE.read_bytes(), "text/plain")},
    )
    response = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "message": "Use a subagent to analyze this IIS log and create a concise report",
            "thread_id": "queryable-plan",
            "artifact_ids": [upload.json()["artifact_id"]],
        },
    )

    plan = client.get(f"/v1/runs/{response.json()['run_id']}/plan")

    assert response.status_code == 200
    assert plan.status_code == 200
    assert plan.json()["status"] == "completed"
    assert plan.json()["version"] >= 1
    assert [step["status"] for step in plan.json()["steps"]] == [
        "completed",
        "completed",
    ]


def test_agent_sse_uses_netlog_skill_for_browser_capture(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={
            "file": (
                "chrome-net-export-log.json",
                NETLOG_FIXTURE.read_bytes(),
                "application/json",
            )
        },
    )

    with client.stream(
        "POST",
        "/v1/messages/stream",
        json={
            "mode": "agent",
            "message": "分析这个 netlog 的浏览器认证失败并给出解决方案",
            "thread_id": "api-netlog-agent-demo",
            "attachment_ids": [upload.json()["attachment_id"]],
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"tool": "netlog"' in body
    assert '"tool": "iis-logs"' not in body
    assert '"skills_used": ["netlog", "report-summary"]' in body
    assert "event: run.completed" in body
    assert "event: run.failed" not in body


def test_agent_remembers_prior_turn_in_same_thread(client: TestClient) -> None:
    first = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "message": "Remember that the project codename is Orion",
            "thread_id": "memory-demo",
        },
    )
    second = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "message": "What is the project codename?",
            "thread_id": "memory-demo",
        },
    )

    assert first.status_code == 200
    assert first.json()["thread_id"] == "memory-demo"
    assert second.status_code == 200
    assert second.json()["message"] == "The project codename is Orion."
    assert second.json()["usage"]["model_calls"] == 1


def test_agent_memory_is_isolated_between_threads(client: TestClient) -> None:
    remembered = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "message": "Remember that the project codename is Vega",
            "thread_id": "memory-thread-a",
        },
    )
    isolated = client.post(
        "/v1/messages",
        json={
            "mode": "agent",
            "message": "What is the project codename?",
            "thread_id": "memory-thread-b",
        },
    )

    assert remembered.status_code == 200
    assert isolated.status_code == 200
    assert isolated.json()["message"] == "No project codename has been stored in this thread."


def test_post_sse_runs_vendored_iis_skill_from_uploaded_log(client: TestClient) -> None:
    upload = client.post(
        "/v1/attachments",
        files={"file": ("sample.log", IIS_FIXTURE.read_bytes(), "text/plain")},
    )
    attachment_id = upload.json()["attachment_id"]

    with client.stream(
        "POST",
        "/v1/messages/stream",
        json={
            "message": "analyze this IIS log",
            "attachment_ids": [attachment_id],
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"skill": "iis-logs"' in body
    assert '"total_requests": 5' in body
    assert '"model_calls": 0' in body
    assert body.count("event: run.completed") == 1
    assert "event: run.failed" not in body
