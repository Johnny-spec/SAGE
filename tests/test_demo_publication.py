import httpx
import pytest

from sage.workflows import WorkflowTemplate
from scripts.demo_publication import Demo, disabled_package, local_url, workflow_definition


def test_demo_reuses_steps_and_rejects_changed_requests(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"candidate_id": "candidate-demo"})

    with httpx.Client(
        base_url="http://127.0.0.1:8015", transport=httpx.MockTransport(handler)
    ) as client:
        manifest = tmp_path / "manifest.json"
        demo = Demo(client, manifest, str(client.base_url), "http://127.0.0.1:8766/mcp")
        first = demo.step("create", "/candidates", {"title": "Demo"})
        reopened = Demo(client, manifest, str(client.base_url), "http://127.0.0.1:8766/mcp")
        assert reopened.step("create", "/candidates", {"title": "Demo"}) == first
        assert len(calls) == 1
        with pytest.raises(ValueError, match="step changed"):
            reopened.step("create", "/candidates", {"title": "Changed"})
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:secret@localhost:8015",
        "http://localhost:8015?token=secret",
    ],
)
def test_demo_refuses_remote_or_credential_urls(url):
    with pytest.raises(ValueError, match="local HTTP"):
        local_url(url)


@pytest.mark.parametrize("production", [True, False])
def test_demo_refuses_production_or_nonempty_state(tmp_path, production):
    def handler(request):
        path = request.url.path
        payload = {
            "/v1/control/bootstrap": {"environment": "production" if production else "development"},
            "/v1/admin/status": {"active_generation_id": "gen_original"},
            "/v1/admin/generations": [{"generation_id": "gen_original"}],
            "/v1/admin/solutions": [{"solution_id": "user-data"}],
        }[path]
        assert request.method == "GET"
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="http://127.0.0.1:8015", transport=httpx.MockTransport(handler)
    ) as client:
        demo = Demo(
            client, tmp_path / "manifest.json", str(client.base_url), "http://127.0.0.1:8766/mcp"
        )
        with pytest.raises(ValueError, match="production|non-empty"):
            demo.preflight()
        assert not demo.manifest.exists()


def test_demo_workflow_and_upload_are_deterministic():
    template = WorkflowTemplate.model_validate(workflow_definition("tool:mcp:calc:total", 10))
    assert template.steps[1].input["discount_percent"] == 10
    assert disabled_package() == disabled_package()
