from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.control_settings import ControlPlaneSettings
from sage.telemetry import configure_telemetry

SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def test_http_trace_uses_route_template_without_request_payload(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="sage-test",
        endpoint=None,
        insecure=True,
        span_exporter=exporter,
        batch=False,
    )
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
        telemetry_runtime=telemetry,
    )

    with TestClient(app) as client:
        response = client.get("/health/live")

    spans = exporter.get_finished_spans()
    span = next(item for item in spans if item.name == "GET /health/live")
    attributes = dict(span.attributes)
    assert response.status_code == 200
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.route"] == "GET /health/live"
    assert attributes["http.response.status_code"] == 200
    assert "payload" not in str(attributes).casefold()