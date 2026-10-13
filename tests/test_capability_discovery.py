from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sage.agent_catalog import AgentCatalog
from sage.agent_drafts import agent_to_dict
from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.capabilities import CapabilityViewResolver
from sage.catalog import initial_generation
from sage.control_models import SolutionComponentLock, SolutionReleaseRecord
from sage.control_settings import ControlPlaneSettings
from sage.discovery import build_capability_descriptors
from sage.domain import AgentDefinition, Principal, RuntimeContext
from sage.models import SkillPolicy
from sage.policy import PolicyEnforcer
from sage.registry import SkillRegistry


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_discovery_exposes_typed_skill_and_agent_operations(client: TestClient) -> None:
    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    descriptors = {item["capability_id"]: item for item in response.json()}
    echo = descriptors["skill:echo"]
    agent = descriptors["agent:diagnostic-agent"]
    assert echo["schema_version"] == "1.0"
    assert echo["content_digest"].startswith("sha256:")
    assert echo["input_schema"]["required"] == ["text"]
    assert echo["output_schema"]["required"] == ["text"]
    operations = [
        (item["operation_id"], item["path"], item["streaming"])
        for item in echo["operations"]
    ]
    assert operations == [
        ("invoke", "/v1/invocations", False),
        ("message-stream", "/v1/messages/stream", True),
        ("start-operation", "/v1/operations", False),
    ]
    assert agent["kind"] == "agent"
    assert agent["operations"][0]["profile"] == "interactive"
    assert agent["operations"][0]["streaming"] is True
    assert agent["operations"][1]["operation_id"] == "start-operation"
    assert agent["operations"][1]["restart_recoverable"] is False
    assert "skill:echo" in agent["accessible_capability_ids"]
    assert agent["links"]["result"] == "/v1/runs/{run_id}/result"


def test_solution_discovery_contains_only_release_locked_capabilities(
    client: TestClient,
) -> None:
    solution_id = "discovery-echo"
    created = client.post(
        "/v1/admin/solutions",
        json={"solution_id": solution_id, "title": "Discovery", "owner": "tests"},
    )
    assert created.status_code == 201, created.text
    generation = client.app.state.runtime_manager.active.generation
    release = client.post(
        f"/v1/admin/solutions/{solution_id}/releases",
        json={
            "version": "1.0.0",
            "components": [
                {
                    "kind": "skill",
                    "component_id": "echo",
                    "version": generation.skill_versions["echo"],
                    "content_digest": generation.skill_digests["echo"],
                }
            ],
            "supported_profiles": ["direct"],
        },
    )
    assert release.status_code == 201, release.text
    candidate = client.post(
        "/v1/admin/candidates",
        json={
            "title": "Publish discovery Solution",
            "solution_release_ids": [release.json()["release_id"]],
        },
    )
    published = client.post(
        f"/v1/admin/candidates/{candidate.json()['candidate_id']}/publish"
    )
    assert published.status_code == 200, published.text

    nested = client.get(f"/v1/solutions/{solution_id}/capabilities")
    queried = client.get("/v1/capabilities", params={"solution_id": solution_id})
    invoked = client.post(
        nested.json()[0]["operations"][0]["path"],
        json={
            "solution_id": solution_id,
            "capability_id": nested.json()[0]["capability_id"],
            "input": {"text": "discovered invocation"},
        },
    )

    assert nested.status_code == 200
    assert queried.json() == nested.json()
    assert [item["capability_id"] for item in nested.json()] == ["skill:echo"]
    descriptor = nested.json()[0]
    assert descriptor["solution_id"] == solution_id
    assert descriptor["solution_release_id"] == release.json()["release_id"]
    assert descriptor["content_digest"] == generation.skill_digests["echo"]
    assert descriptor["links"]["discovery"].endswith(f"solution_id={solution_id}")
    assert invoked.status_code == 200
    assert invoked.json()["output"] == {"text": "discovered invocation"}


def test_discovery_routes_are_documented_and_unknown_solution_is_not_hidden(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json")
    unknown = client.get("/v1/solutions/missing/capabilities")

    assert schema.status_code == 200
    assert "/v1/capabilities" in schema.json()["paths"]
    assert "/v1/solutions/{solution_id}/capabilities" in schema.json()["paths"]
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Solution not found: missing"


def test_discovery_does_not_leak_write_capability_or_dependent_agent() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    echo = registry.get("echo")
    assert echo is not None
    policy = SkillPolicy(risk="write-low")
    manifest = echo.manifest.model_copy(
        update={"spec": echo.manifest.spec.model_copy(update={"policy": policy})}
    )
    write_registry = SkillRegistry.from_definitions(
        (echo.model_copy(update={"manifest": manifest}),)
    )
    agent = AgentDefinition(
        agent_id="writer",
        name="Writer",
        version="1.0.0",
        instructions="Use the write capability.",
        capability_ids=("skill:echo",),
    )
    generation = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {agent.agent_id: agent_to_dict(agent)},
        agent.agent_id,
    )
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_default",
            principal_id="user-1",
            roles=frozenset({"user"}),
        ),
        generation_id=generation.generation_id,
    )
    view = CapabilityViewResolver(write_registry, PolicyEnforcer()).resolve(context)

    descriptors = build_capability_descriptors(
        generation=generation,
        release=None,
        capabilities=view,
        agents=AgentCatalog((agent,), default_agent_id=agent.agent_id),
        durable_available=False,
    )

    assert descriptors == ()

    admin_context = RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_default",
            principal_id="admin-1",
            roles=frozenset({"capability_admin"}),
        ),
        generation_id=generation.generation_id,
    )
    admin_view = CapabilityViewResolver(write_registry, PolicyEnforcer()).resolve(
        admin_context
    )
    admin_descriptors = build_capability_descriptors(
        generation=generation,
        release=None,
        capabilities=admin_view,
        agents=AgentCatalog((agent,), default_agent_id=agent.agent_id),
        durable_available=False,
    )

    assert {item.capability_id for item in admin_descriptors} == {
        "skill:echo",
        "agent:writer",
    }
    assert all(
        "start-operation" not in {operation.operation_id for operation in item.operations}
        for item in admin_descriptors
    )


def test_durable_skill_discovery_reports_unavailable_runtime() -> None:
    registry = SkillRegistry.load(DEFAULT_SKILLS_ROOT)
    echo = registry.get("echo")
    assert echo is not None
    execution = echo.manifest.spec.execution.model_copy(
        update={
            "type": "workflow",
            "workflow": "workflow.yaml",
            "capability_ids": ("skill:echo",),
        }
    )
    manifest = echo.manifest.model_copy(
        update={"spec": echo.manifest.spec.model_copy(update={"execution": execution})}
    )
    orchestrated = echo.model_copy(update={"manifest": manifest})
    orchestrated_registry = SkillRegistry.from_definitions((orchestrated,))
    generation = initial_generation(DEFAULT_SKILLS_ROOT, {}, "")
    release = SolutionReleaseRecord(
        release_id="durable-only@1.0.0",
        solution_id="durable-only",
        version="1.0.0",
        content_digest="sha256:" + "1" * 64,
        components=(
            SolutionComponentLock(
                kind="skill",
                component_id="echo",
                version=echo.version,
                content_digest=generation.skill_digests["echo"],
            ),
        ),
        supported_profiles=("durable",),
    )
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        generation_id=generation.generation_id,
        solution_id=release.solution_id,
        solution_release_id=release.release_id,
    )
    view = CapabilityViewResolver(orchestrated_registry, PolicyEnforcer()).resolve(context)

    descriptors = build_capability_descriptors(
        generation=generation,
        release=release,
        capabilities=view,
        agents=AgentCatalog((), default_agent_id=None),
        durable_available=False,
    )

    assert len(descriptors) == 1
    operation = descriptors[0].operations[0]
    assert operation.operation_id == "durable-run"
    assert operation.idempotency_key == "required"
    assert operation.cancellation_supported is True
    assert operation.runtime_available is False
    assert operation.restart_recoverable is True