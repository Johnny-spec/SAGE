from pathlib import Path

import pytest
import yaml

from sage.control_models import GenerationRecord, SolutionDefinitionRecord
from sage.control_store import LocalControlStore
from sage.workflow_draft_models import WorkflowDraftInput
from sage.workflow_drafts import DraftConflict, WorkflowDraftManager
from sage.workflows import WorkflowTemplate


def manager_for(tmp_path: Path) -> WorkflowDraftManager:
    store = LocalControlStore(tmp_path / "control", GenerationRecord(
        generation_id="initial", content_digest="sha256:startup", skills_root=str(tmp_path),
        skill_versions={"echo": "1.0.0"}, agent_definitions={},
    ))
    store.update(lambda state: state.solutions.update({"demo": SolutionDefinitionRecord(
        solution_id="demo", title="Demo", owner="tester",
    )}))
    return WorkflowDraftManager(store)


def payload() -> WorkflowDraftInput:
    return WorkflowDraftInput(title="Review", solution_id="demo", definition={
        "apiVersion": "sage.dev/v1beta1", "kind": "WorkflowTemplate",
        "workflowId": "review", "version": "1.0.0", "steps": [
            {"id": "echo", "kind": "skill", "target": "echo", "inheritInput": False},
        ],
    })


def test_save_conflict_restart_and_no_publication(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    draft = manager.create(payload(), "ws", "author")
    saved = manager.update(draft.draft_id, "ws", 1, payload())
    assert saved.revision == 2
    with pytest.raises(DraftConflict):
        manager.update(draft.draft_id, "ws", 1, payload())
    restored = WorkflowDraftManager(LocalControlStore(tmp_path / "control"))
    assert restored.get(draft.draft_id, "ws").revision == 2
    assert manager._store.snapshot().active_generation_id == "initial"
    assert not manager._store.snapshot().candidates
    assert not manager.list("other")
    with pytest.raises(KeyError):
        manager.delete(draft.draft_id, "other", 2)
    with pytest.raises(DraftConflict):
        manager.delete(draft.draft_id, "ws", 1)
    manager.delete(draft.draft_id, "ws", 2)
    assert not manager.list("ws")


def test_validation_export_and_incomplete_draft(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    draft = manager.create(payload(), "ws", "author")
    report = manager.validate(draft.draft_id, "ws", 1)
    assert report["status"] == "valid"
    assert not report["publication_ready"]
    WorkflowTemplate.model_validate(yaml.safe_load(manager.export(draft.draft_id, "ws", 1)))
    invalid = payload()
    invalid.definition["steps"][0]["dependsOn"] = ["echo"]
    manager.update(draft.draft_id, "ws", 1, invalid)
    assert manager.validate(draft.draft_id, "ws", 2)["status"] == "invalid"
    with pytest.raises(ValueError, match="validation"):
        manager.export(draft.draft_id, "ws", 2)
    incomplete = manager.create(WorkflowDraftInput(title="New", solution_id="demo"), "ws", "a")
    assert manager.validate(incomplete.draft_id, "ws", 1)["status"] == "invalid"


@pytest.mark.parametrize("field,value", [("target", "missing"), ("kind", "workflow"),
                                         ("concurrencyKey", "exclusive"),
                                         ("failurePolicy", "escalate")])
def test_unavailable_runtime_features(tmp_path: Path, field: str, value: str) -> None:
    manager = manager_for(tmp_path)
    invalid = payload()
    invalid.definition["steps"][0][field] = value
    draft = manager.create(invalid, "ws", "author")
    assert manager.validate(draft.draft_id, "ws", 1)["status"] == "invalid"


def test_sensitive_fields_rejected() -> None:
    with pytest.raises(ValueError, match="Sensitive"):
        WorkflowDraftInput(title="Unsafe", solution_id="demo", definition={"api_key": "value"})


@pytest.mark.parametrize("design", [{"positions": []}, {"positions": {"one": {"x": -1, "y": 0}}},
                                  {"labels": {"one": {}}}, {"unknown": 1}])
def test_invalid_design_rejected(design: dict) -> None:
    with pytest.raises(ValueError):
        WorkflowDraftInput(title="Invalid", solution_id="demo", design=design)


def test_disabled_skill_rejected(tmp_path: Path) -> None:
    original = manager_for(tmp_path)
    draft = original.create(payload(), "ws", "author")
    manager = WorkflowDraftManager(original._store, frozenset({"echo"}))
    assert manager.validate(draft.draft_id, "ws", 1)["status"] == "invalid"
    with pytest.raises(ValueError):
        manager.export(draft.draft_id, "ws", 1)