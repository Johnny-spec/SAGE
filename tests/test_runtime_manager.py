import asyncio
import shutil
from pathlib import Path

import pytest

from sage.agent_definitions import default_diagnostic_agent
from sage.agent_drafts import agent_to_dict
from sage.agent_model import DemoAgentModel
from sage.api_models import MessageRequest
from sage.app import DEFAULT_SKILLS_ROOT
from sage.attachments import AttachmentStore
from sage.control_models import GenerationRecord, SolutionComponentLock
from sage.control_store import LocalControlStore
from sage.digests import directory_digest
from sage.domain import Principal, RuntimeContext
from sage.errors import PolicyDeniedError, TargetNotFoundError
from sage.registry import SkillRegistry
from sage.runs import InMemoryRunStore
from sage.runtime_manager import RuntimeManager
from sage.solutions import SolutionManager


def _generation(generation_id: str, skills_root: Path) -> GenerationRecord:
    registry = SkillRegistry.load(skills_root)
    definition = default_diagnostic_agent()
    return GenerationRecord(
        generation_id=generation_id,
        content_digest=directory_digest(skills_root),
        skills_root=str(skills_root),
        skill_versions={item.name: item.version for item in registry},
        skill_digests={item.name: directory_digest(item.package_root) for item in registry},
        agent_definitions={definition.agent_id: agent_to_dict(definition)},
        default_agent_id=definition.agent_id,
    )


def test_runtime_manager_pins_existing_thread_to_generation(tmp_path: Path) -> None:
    first_root = tmp_path / "skills-v1"
    second_root = tmp_path / "skills-v2"
    shutil.copytree(DEFAULT_SKILLS_ROOT, first_root)
    shutil.copytree(DEFAULT_SKILLS_ROOT, second_root)
    first = _generation("gen_1", first_root)
    second = _generation("gen_2", second_root)
    second.agent_definitions["searchragmd"] = {
        **second.agent_definitions[second.default_agent_id],
        "agent_id": "searchragmd",
    }
    store = LocalControlStore(tmp_path / "state", first)
    attachments = AttachmentStore(tmp_path / "attachments")
    manager = RuntimeManager(
        store,
        attachments,
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    principal = Principal.anonymous()

    original, _ = manager.resolve(
        MessageRequest(message="first", thread_id="thread-stable"),
        principal,
    )

    def add_generation(state: object) -> None:
        state.generations[second.generation_id] = second  # type: ignore[attr-defined]

    store.update(add_generation)
    manager.activate(second.generation_id)
    pinned, _ = manager.resolve(
        MessageRequest(message="second", thread_id="thread-stable"),
        principal,
    )
    newest, created = manager.resolve(MessageRequest(message="new"), principal)

    assert original.generation.generation_id == "gen_1"
    assert pinned.generation.generation_id == "gen_1"
    assert newest.generation.generation_id == "gen_2"
    assert created.thread_id is not None
    with pytest.raises(TargetNotFoundError, match="Start a new thread"):
        manager.resolve(
            MessageRequest(
                message="search", mode="agent", agent_id="searchragmd",
                thread_id="thread-stable",
            ),
            principal,
        )
    current, _ = manager.resolve(
        MessageRequest(message="search", mode="agent", agent_id="searchragmd"), principal,
    )
    assert current.agent_catalog.get("searchragmd").agent_id == "searchragmd"
    with pytest.raises(TargetNotFoundError, match="Agent not found: missing"):
        manager.resolve(
            MessageRequest(message="search", mode="agent", agent_id="missing"), principal,
        )


def test_runtime_manager_resolves_child_from_parent_generation(tmp_path: Path) -> None:
    first_root = tmp_path / "skills-v1"
    second_root = tmp_path / "skills-v2"
    shutil.copytree(DEFAULT_SKILLS_ROOT, first_root)
    shutil.copytree(DEFAULT_SKILLS_ROOT, second_root)
    first = _generation("gen_1", first_root)
    second = _generation("gen_2", second_root)
    store = LocalControlStore(tmp_path / "state", first)
    runs = InMemoryRunStore()
    manager = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        runs,
        agent_model=DemoAgentModel(),
    )
    principal = Principal.anonymous()
    parent_context = RuntimeContext.create(
        mode="agent",
        lifecycle="durable",
        principal=principal,
        generation_id="gen_1",
    )
    asyncio.run(runs.begin(parent_context))

    def activate_second(state: object) -> None:
        state.generations[second.generation_id] = second  # type: ignore[attr-defined]

    store.update(activate_second)
    manager.activate("gen_2")

    snapshot, resolved_parent = asyncio.run(
        manager.resolve_child(
            parent_context.run_id,
            principal,
            target_agent_id=first.default_agent_id,
        )
    )

    assert snapshot.generation.generation_id == "gen_1"
    assert resolved_parent.run_id == parent_context.run_id


def test_runtime_manager_scopes_direct_run_to_solution_release(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    shutil.copytree(DEFAULT_SKILLS_ROOT, skills_root)
    generation = _generation("gen_1", skills_root)
    store = LocalControlStore(tmp_path / "state", generation)
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="echo-solution",
        title="Echo",
        description="",
        owner="platform-team",
        entry_agent_id=None,
        supported_profiles=("direct",),
    )
    release = solutions.create_release(
        "echo-solution",
        version="1.0.0",
        components=(
            SolutionComponentLock(
                kind="skill",
                component_id="echo",
                version=generation.skill_versions["echo"],
                content_digest=generation.skill_digests["echo"],
            ),
        ),
    )

    def publish(state: object) -> None:
        current = state.generations[generation.generation_id]  # type: ignore[attr-defined]
        state.generations[generation.generation_id] = current.model_copy(  # type: ignore[attr-defined]
            update={"solution_releases": {"echo-solution": release.release_id}}
        )

    store.update(publish)
    runs = InMemoryRunStore()
    manager = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        runs,
        agent_model=DemoAgentModel(),
    )
    request = MessageRequest(
        message="hello",
        solution_id="echo-solution",
        skill="echo",
        arguments={"text": "hello"},
    )

    snapshot, resolved = manager.resolve(request, Principal.anonymous())
    response = asyncio.run(
        snapshot.message_service.run(resolved, principal=Principal.anonymous())
    )
    run = asyncio.run(runs.get(response.run_id or ""))

    assert [item.name for item in snapshot.registry] == ["echo"]
    assert len(snapshot.agent_catalog) == 0
    assert response.result == {"text": "hello"}
    assert run is not None
    assert run.context.solution_id == "echo-solution"
    assert run.context.solution_release_id == release.release_id
    assert run.events[0].data["solution_id"] == "echo-solution"
    with pytest.raises(TargetNotFoundError, match="Skill not found"):
        asyncio.run(
            snapshot.message_service.run(
                resolved.model_copy(update={"skill": "iis-logs"}),
                principal=Principal.anonymous(),
            )
        )


def test_runtime_manager_requires_authorized_solution_selection(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    shutil.copytree(DEFAULT_SKILLS_ROOT, skills_root)
    generation = _generation("gen_1", skills_root)
    store = LocalControlStore(tmp_path / "state", generation)
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="legal-only",
        title="Legal",
        description="",
        owner="legal-team",
        entry_agent_id=None,
        allowed_roles=("legal-reviewer",),
        supported_profiles=("direct",),
    )
    release = solutions.create_release(
        "legal-only",
        version="1.0.0",
        components=(
            SolutionComponentLock(
                kind="skill",
                component_id="echo",
                version=generation.skill_versions["echo"],
                content_digest=generation.skill_digests["echo"],
            ),
        ),
    )

    def publish(state: object) -> None:
        current = state.generations[generation.generation_id]  # type: ignore[attr-defined]
        state.generations[generation.generation_id] = current.model_copy(  # type: ignore[attr-defined]
            update={"solution_releases": {"legal-only": release.release_id}}
        )

    store.update(publish)
    manager = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )

    with pytest.raises(TargetNotFoundError, match="Select a Solution"):
        manager.resolve(MessageRequest(message="hello"), Principal.anonymous())
    with pytest.raises(PolicyDeniedError, match="access denied"):
        manager.resolve(
            MessageRequest(message="hello", solution_id="legal-only"),
            Principal.anonymous(),
        )


def test_runtime_manager_preserves_legacy_thread_binding_after_key_upgrade(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "skills-v1"
    second_root = tmp_path / "skills-v2"
    shutil.copytree(DEFAULT_SKILLS_ROOT, first_root)
    shutil.copytree(DEFAULT_SKILLS_ROOT, second_root)
    first = _generation("gen_1", first_root)
    second = _generation("gen_2", second_root)
    store = LocalControlStore(tmp_path / "state", first)

    def seed(state: object) -> None:
        state.generations[second.generation_id] = second  # type: ignore[attr-defined]
        state.active_generation_id = second.generation_id  # type: ignore[attr-defined]
        state.thread_bindings["ws_default:anonymous:legacy-thread"] = "gen_1"  # type: ignore[attr-defined]

    store.update(seed)
    manager = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )

    pinned, _ = manager.resolve(
        MessageRequest(message="continue", thread_id="legacy-thread"),
        Principal.anonymous(),
    )

    assert pinned.generation.generation_id == "gen_1"
    assert (
        store.snapshot().thread_bindings["ws_default:anonymous:_legacy:legacy-thread"]
        == "gen_1"
    )


def test_runtime_manager_rejects_tampered_solution_release(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    shutil.copytree(DEFAULT_SKILLS_ROOT, skills_root)
    generation = _generation("gen_1", skills_root)
    store = LocalControlStore(tmp_path / "state", generation)
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="echo-solution",
        title="Echo",
        description="",
        owner="platform-team",
        entry_agent_id=None,
        supported_profiles=("direct",),
    )
    release = solutions.create_release(
        "echo-solution",
        version="1.0.0",
        components=(
            SolutionComponentLock(
                kind="skill",
                component_id="echo",
                version=generation.skill_versions["echo"],
                content_digest=generation.skill_digests["echo"],
            ),
        ),
    )

    def tamper(state: object) -> None:
        current = state.generations[generation.generation_id]  # type: ignore[attr-defined]
        state.generations[generation.generation_id] = current.model_copy(  # type: ignore[attr-defined]
            update={"solution_releases": {"echo-solution": release.release_id}}
        )
        state.solution_releases[release.release_id] = release.model_copy(  # type: ignore[attr-defined]
            update={"allowed_roles": ("unexpected-role",)}
        )

    store.update(tamper)
    manager = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )

    with pytest.raises(ValueError, match="digest mismatch"):
        manager.get(generation.generation_id, "echo-solution")
