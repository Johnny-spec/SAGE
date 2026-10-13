import hashlib
import io
import zipfile
from pathlib import Path

import pytest
import yaml

from sage.control_models import ControlPlaneState, GenerationRecord, SolutionDefinitionRecord
from sage.control_store import LocalControlStore
from sage.digests import directory_digest
from sage.registry import SkillRegistry
from sage.solution_builder import plan_solution_bundle, solution_bundle_bytes
from sage.workflow_draft_models import WorkflowDraftRecord
from sage.workflow_drafts import DraftConflict


def example():
    registry = SkillRegistry.load(Path(__file__).parent / "fixtures" / "skills")
    skills = {skill.name: skill for skill in registry}
    generation = GenerationRecord(
        generation_id="base",
        content_digest="sha256:base",
        skills_root=str(Path.cwd()),
        skill_versions={name: skill.version for name, skill in skills.items()},
        skill_digests={
            name: directory_digest(skill.package_root) for name, skill in skills.items()
        },
        agent_definitions={
            "writer": {
                "agent_id": "writer",
                "version": "1.0.0",
                "capability_ids": ["skill:echo"],
                "delegations": [{"target_agent_id": "reviewer"}],
            },
            "reviewer": {
                "agent_id": "reviewer",
                "version": "2.0.0",
                "capability_ids": ["skill:echo"],
            },
        },
    )
    draft = WorkflowDraftRecord(
        title="Review",
        solution_id="demo",
        draft_id="draft",
        workspace_id="ws",
        created_by="a",
        definition={
            "apiVersion": "sage.dev/v1beta1",
            "kind": "WorkflowTemplate",
            "workflowId": "review",
            "version": "1.0.0",
            "steps": [{"id": "write", "kind": "agent", "target": "writer"}],
        },
    )
    state = ControlPlaneState(
        active_generation_id="base",
        generations={"base": generation},
        workflow_drafts={"draft": draft},
        solutions={"demo": SolutionDefinitionRecord(solution_id="demo", title="Demo", owner="a")},
    )
    return state, registry


def test_recursive_dependencies_and_deterministic_bundle() -> None:
    state, registry = example()
    plan = plan_solution_bundle(
        state, registry, draft_id="draft", workspace_id="ws", revision=1, version="1.0.0"
    )
    assert [(item["kind"], item["id"]) for item in plan["dependencies"]] == [
        ("agent", "reviewer"),
        ("agent", "writer"),
        ("skill", "echo"),
    ]
    assert len(plan["dependencies"][-1]["required_by"]) == 2
    payload = solution_bundle_bytes(plan)
    assert payload == solution_bundle_bytes(plan)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = yaml.safe_load(archive.read("solution.yaml"))
        lock = manifest["spec"]["components"][-1]
        assert (
            lock["contentDigest"]
            == "sha256:"
            + hashlib.sha256(
                archive.read("workflow.yaml"),
            ).hexdigest()
        )
    assert state.active_generation_id == "base"
    assert not state.solution_releases


def test_revision_workspace_and_dependency_failures() -> None:
    state, registry = example()
    with pytest.raises(KeyError):
        plan_solution_bundle(
            state, registry, draft_id="draft", workspace_id="other", revision=1, version="1.0.0"
        )
    with pytest.raises(DraftConflict):
        plan_solution_bundle(
            state, registry, draft_id="draft", workspace_id="ws", revision=2, version="1.0.0"
        )
    state.generations["base"].agent_definitions["reviewer"]["delegations"] = [
        {"target_agent_id": "writer"},
    ]
    with pytest.raises(ValueError, match="cycle"):
        plan_solution_bundle(
            state, registry, draft_id="draft", workspace_id="ws", revision=1, version="1.0.0"
        )


def test_manager_transaction_rolls_back_and_persists(tmp_path: Path) -> None:
    state, _ = example()
    store = LocalControlStore(tmp_path, state.generations["base"])
    with pytest.raises(ValueError, match="rollback"), store.transaction():
        store.update(lambda current: current.solutions.update(state.solutions))
        assert store.snapshot().solutions
        raise ValueError("rollback")
    assert not store.snapshot().solutions
    assert not LocalControlStore(tmp_path).snapshot().solutions
    with store.transaction():
        store.update(lambda current: current.solutions.update(state.solutions))
        store.update(lambda current: current.workflow_drafts.update(state.workflow_drafts))
    assert LocalControlStore(tmp_path).snapshot().workflow_drafts["draft"].solution_id == "demo"


def test_field_contract_rejects_bad_literal() -> None:
    state, registry = example()
    state.workflow_drafts["draft"].definition["steps"][0]["input"] = {"message": 42}
    with pytest.raises(ValueError, match="Invalid literal"):
        plan_solution_bundle(
            state, registry, draft_id="draft", workspace_id="ws", revision=1, version="1.0.0"
        )
