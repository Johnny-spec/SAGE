from pathlib import Path

import pytest

from sage.agent_drafts import AgentDraftManager
from sage.control_models import GenerationRecord
from sage.control_store import LocalControlStore


def _manager(tmp_path: Path) -> AgentDraftManager:
    store = LocalControlStore(
        tmp_path / "data",
        GenerationRecord(
            generation_id="gen_startup",
            content_digest="sha256:startup",
            skills_root=str(tmp_path),
            skill_versions={},
            agent_definitions={},
        ),
    )
    return AgentDraftManager(store)


def _definition(version: str = "1.0.0") -> dict[str, object]:
    return {
        "agent_id": "support-agent",
        "name": "Support Agent",
        "version": version,
        "instructions": "Use governed tools to answer support requests.",
        "capability_ids": ["skill:echo"],
        "subagents": [],
        "memory_policy": "thread",
        "max_steps": 20,
    }


def test_agent_draft_validates_capability_bindings(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    draft = manager.create(_definition())

    validated = manager.validate(draft.draft_id, {"skill:echo"}, set())

    assert validated.status == "valid"
    assert validated.validation.content_digest is not None


def test_agent_draft_rejects_unknown_capability(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    draft = manager.create(_definition())

    validated = manager.validate(draft.draft_id, set(), set())

    assert validated.status == "invalid"
    assert validated.validation.issues[0].code == "unknown_capability"


def test_agent_draft_requires_published_delegation_target(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    definition = _definition()
    definition["delegations"] = [
        {
            "alias": "reviewer",
            "target_agent_id": "review-agent",
            "description": "Verify the proposed answer.",
        }
    ]
    draft = manager.create(definition)

    rejected = manager.validate(draft.draft_id, {"skill:echo"}, set())
    accepted = manager.validate(
        draft.draft_id,
        {"skill:echo"},
        {("review-agent", "1.0.0")},
    )

    assert rejected.validation.issues[0].code == "unknown_delegation_target"
    assert accepted.status == "valid"


def test_published_agent_cannot_be_edited(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    draft = manager.create(_definition())

    def publish(state: object) -> None:
        state.agents[draft.draft_id].status = "published"  # type: ignore[attr-defined]

    manager._store.update(publish)

    with pytest.raises(ValueError, match="immutable"):
        manager.update(draft.draft_id, _definition("1.0.1"))


def test_active_agent_is_seeded_as_published_and_can_be_cloned(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.ensure_published({"support-agent": _definition()})

    published = manager.list_drafts()[0]
    cloned = manager.clone(published.draft_id, "1.1.0")

    assert published.status == "published"
    assert cloned.status == "draft"
    assert cloned.definition["version"] == "1.1.0"
