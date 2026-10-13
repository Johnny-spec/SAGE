from pathlib import Path

import pytest

from sage.control_models import GenerationRecord, SolutionComponentLock
from sage.control_store import LocalControlStore
from sage.solutions import SolutionError, SolutionManager


def _manager(tmp_path: Path) -> SolutionManager:
    generation = GenerationRecord(
        generation_id="gen_startup",
        content_digest="sha256:startup",
        skills_root=str(tmp_path),
        skill_versions={},
        agent_definitions={},
    )
    return SolutionManager(LocalControlStore(tmp_path / "state", generation))


def _component(kind: str, component_id: str) -> SolutionComponentLock:
    return SolutionComponentLock(
        kind=kind,  # type: ignore[arg-type]
        component_id=component_id,
        version="1.0.0",
        content_digest="sha256:" + "a" * 64,
    )


def test_solution_release_is_server_digested_and_component_order_is_stable(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager.create(
        solution_id="incident-diagnostics",
        title="Incident diagnostics",
        description="Governed incident analysis.",
        owner="platform-team",
        entry_agent_id="diagnostic-agent",
        supported_profiles=("interactive", "direct", "interactive"),
    )

    release = manager.create_release(
        "incident-diagnostics",
        version="1.0.0",
        components=(
            _component("skill", "iis-logs"),
            _component("agent", "diagnostic-agent"),
        ),
        source_generation_id="gen_startup",
    )

    assert release.release_id == "incident-diagnostics@1.0.0"
    assert release.content_digest.startswith("sha256:")
    assert len(release.content_digest) == 71
    assert [item.kind for item in release.components] == ["agent", "skill"]
    assert release.supported_profiles == ("interactive", "direct")
    assert manager.list_releases("incident-diagnostics") == [release]


def test_solution_release_rejects_unlocked_entry_agent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create(
        solution_id="news-intelligence",
        title="News intelligence",
        description="",
        owner="news-team",
        entry_agent_id="news-agent",
        supported_profiles=("interactive",),
    )

    with pytest.raises(SolutionError, match="entry Agent"):
        manager.create_release(
            "news-intelligence",
            version="1.0.0",
            components=(_component("skill", "fetch-news"),),
        )


def test_solution_release_version_is_immutable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create(
        solution_id="enterprise-qa",
        title="Enterprise QA",
        description="",
        owner="knowledge-team",
        entry_agent_id=None,
        supported_profiles=("direct",),
    )
    component = _component("skill", "search")
    manager.create_release(
        "enterprise-qa",
        version="1.0.0",
        components=(component,),
    )

    with pytest.raises(SolutionError, match="already exists"):
        manager.create_release(
            "enterprise-qa",
            version="1.0.0",
            components=(component,),
        )