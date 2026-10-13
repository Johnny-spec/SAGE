import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile

from sage.agent_definitions import default_diagnostic_agent
from sage.agent_drafts import AgentDraftManager, agent_to_dict
from sage.agent_model import DemoAgentModel
from sage.attachments import AttachmentStore
from sage.catalog import (
    CatalogError,
    CatalogManager,
    _promote_directory,
    initial_generation,
)
from sage.control_models import SolutionComponentLock
from sage.control_settings import ControlPlaneSettings
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest
from sage.evaluations import EvaluationManager
from sage.mcp_control import McpServerManager
from sage.runs import InMemoryRunStore
from sage.runtime_manager import RuntimeManager
from sage.skill_packages import SkillPackageManager
from sage.solutions import SolutionManager


class NoopConnector:
    async def test(self, server: object) -> dict[str, str]:
        return {}

    async def discover(self, server: object) -> tuple[object, ...]:
        return ()

    async def invoke(
        self, server: object, tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return {}


def test_generation_directory_promotion_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".generation.tmp"
    target = tmp_path / "generation"
    source.mkdir()
    (source / "marker.txt").write_text("complete", encoding="utf-8")
    original_replace = Path.replace
    attempts = 0

    def transient_replace(path: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary file handle contention")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", transient_replace)

    _promote_directory(source, target)

    assert attempts == 3
    assert not source.exists()
    assert (target / "marker.txt").read_text(encoding="utf-8") == "complete"


def test_generation_directory_promotion_does_not_overwrite_existing_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".generation.tmp"
    target = tmp_path / "generation"
    source.mkdir()
    target.mkdir()

    with pytest.raises(FileExistsError, match="already exists"):
        _promote_directory(source, target)


def _skill_zip() -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "new-skill/SKILL.md",
            "---\nname: new-skill\ndescription: New uploaded code.\n---\n",
        )
        archive.writestr(
            "new-skill/skill.yaml",
            """apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata: {name: new-skill, version: 1.0.0}
spec:
  execution: {type: python-subprocess, entrypoint: main.py}
  inputSchema: {type: object}
  outputSchema: {type: object}
""",
        )
        archive.writestr("new-skill/main.py", "print('{}')\n")
    return stream.getvalue()


def test_candidate_activation_and_approved_rollback(tmp_path: Path) -> None:
    from sage.app import DEFAULT_SKILLS_ROOT

    definition = default_diagnostic_agent()
    initial = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {definition.agent_id: agent_to_dict(definition)},
        definition.agent_id,
    )
    store = LocalControlStore(tmp_path / "state", initial)
    skills = SkillPackageManager(store)
    agents = AgentDraftManager(store)
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    mcp = McpServerManager(store, settings, NoopConnector(), skills)  # type: ignore[arg-type]
    runtimes = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    catalog = CatalogManager(store, agents, skills, mcp, runtimes)
    package = asyncio.run(
        skills.upload(UploadFile(filename="new-skill.zip", file=BytesIO(_skill_zip())))
    )
    skills.validate(package.package_id)
    candidate = catalog.create_candidate(
        title="Publish disabled uploaded code",
        description="Catalog the package without executing it.",
        agent_draft_ids=(),
        skill_package_ids=(package.package_id,),
        default_agent_id=None,
    )
    validated = catalog.validate(candidate.candidate_id)
    approval = catalog.submit_approval(candidate.candidate_id)
    with pytest.raises(CatalogError, match="current state"):
        catalog.validate(candidate.candidate_id)
    catalog.decide_approval(
        approval.approval_id,
        approve=True,
        actor_fingerprint="admin-test",
        reason="Reviewed package and execution is disabled.",
    )
    with pytest.raises(CatalogError, match="current state"):
        catalog.validate(candidate.candidate_id)

    generation = catalog.activate(candidate.candidate_id)
    with pytest.raises(CatalogError, match="current state"):
        catalog.validate(candidate.candidate_id)
    assert catalog.get_candidate(candidate.candidate_id).approval_id == approval.approval_id
    assert catalog.get_candidate(candidate.candidate_id).status == "active"

    assert validated.status == "valid"
    assert runtimes.active.generation.generation_id == generation.generation_id
    published = runtimes.active.registry.get("new-skill")
    assert published is not None
    assert published.manifest.spec.policy.execution_enabled is False
    rollback = catalog.request_rollback(initial.generation_id)
    catalog.decide_approval(
        rollback.approval_id,
        approve=True,
        actor_fingerprint="admin-test",
        reason="Emergency rollback test.",
    )
    restored = catalog.rollback(rollback.approval_id)
    assert restored.generation_id == initial.generation_id
    assert runtimes.active.generation.generation_id == initial.generation_id


def test_candidate_detects_skill_tampering_after_validation(tmp_path: Path) -> None:
    from sage.app import DEFAULT_SKILLS_ROOT

    definition = default_diagnostic_agent()
    initial = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {definition.agent_id: agent_to_dict(definition)},
        definition.agent_id,
    )
    store = LocalControlStore(tmp_path / "state", initial)
    skills = SkillPackageManager(store)
    agents = AgentDraftManager(store)
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    mcp = McpServerManager(
        store,
        settings,
        NoopConnector(),  # type: ignore[arg-type]
        skills,
    )
    runtimes = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    catalog = CatalogManager(store, agents, skills, mcp, runtimes)
    package = asyncio.run(
        skills.upload(UploadFile(filename="new-skill.zip", file=BytesIO(_skill_zip())))
    )
    validated_package = skills.validate(package.package_id)
    candidate = catalog.create_candidate(
        title="Tamper test",
        description="",
        agent_draft_ids=(),
        skill_package_ids=(package.package_id,),
        default_agent_id=None,
    )
    validated = catalog.validate(candidate.candidate_id)
    approval = catalog.submit_approval(candidate.candidate_id)
    catalog.decide_approval(
        approval.approval_id,
        approve=True,
        actor_fingerprint="admin-test",
        reason="Approved before tampering.",
    )
    package_root = store.resolve_path(validated_package.package_relpath)
    (package_root / "main.py").write_text("print('changed')\n", encoding="utf-8")

    assert validated.status == "valid"
    with pytest.raises(CatalogError, match="no longer valid"):
        catalog.activate(candidate.candidate_id)
    assert store.snapshot().active_generation_id == initial.generation_id


def test_candidate_atomically_activates_solution_release(tmp_path: Path) -> None:
    from sage.app import DEFAULT_SKILLS_ROOT

    definition = default_diagnostic_agent()
    definition_data = agent_to_dict(definition)
    initial = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {definition.agent_id: definition_data},
        definition.agent_id,
    )
    store = LocalControlStore(tmp_path / "state", initial)
    skills = SkillPackageManager(store)
    agents = AgentDraftManager(store)
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    mcp = McpServerManager(store, settings, NoopConnector(), skills)  # type: ignore[arg-type]
    runtimes = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    catalog = CatalogManager(store, agents, skills, mcp, runtimes)
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="incident-diagnostics",
        title="Incident diagnostics",
        description="",
        owner="platform-team",
        entry_agent_id=definition.agent_id,
        supported_profiles=("direct", "interactive"),
    )
    release = solutions.create_release(
        "incident-diagnostics",
        version="1.0.0",
        components=(
            SolutionComponentLock(
                kind="agent",
                component_id=definition.agent_id,
                version=definition.version,
                content_digest=canonical_digest(definition_data),
            ),
            *(
                SolutionComponentLock(
                    kind="skill",
                    component_id=name,
                    version=initial.skill_versions[name],
                    content_digest=initial.skill_digests[name],
                )
                for name in sorted(
                    capability.removeprefix("skill:")
                    for capability in definition.capability_ids
                )
            ),
        ),
        source_generation_id=initial.generation_id,
    )
    candidate = catalog.create_candidate(
        title="Publish incident diagnostics Solution",
        description="",
        agent_draft_ids=(),
        skill_package_ids=(),
        solution_release_ids=(release.release_id,),
        default_agent_id=None,
    )

    validated = catalog.validate(candidate.candidate_id)
    approval = catalog.submit_approval(candidate.candidate_id)
    catalog.decide_approval(
        approval.approval_id,
        approve=True,
        actor_fingerprint="admin-test",
        reason="Release lock reviewed.",
    )
    generation = catalog.activate(candidate.candidate_id)

    assert validated.status == "valid"
    assert generation.solution_releases == {
        "incident-diagnostics": "incident-diagnostics@1.0.0"
    }
    assert generation.content_digest != initial.content_digest
    assert runtimes.active.generation.generation_id == generation.generation_id


def test_candidate_rejects_solution_release_with_wrong_skill_digest(tmp_path: Path) -> None:
    from sage.app import DEFAULT_SKILLS_ROOT

    definition = default_diagnostic_agent()
    initial = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {definition.agent_id: agent_to_dict(definition)},
        definition.agent_id,
    )
    store = LocalControlStore(tmp_path / "state", initial)
    skills = SkillPackageManager(store)
    agents = AgentDraftManager(store)
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    mcp = McpServerManager(store, settings, NoopConnector(), skills)  # type: ignore[arg-type]
    runtimes = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    catalog = CatalogManager(store, agents, skills, mcp, runtimes)
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
                version=initial.skill_versions["echo"],
                content_digest="sha256:" + "f" * 64,
            ),
        ),
    )
    candidate = catalog.create_candidate(
        title="Publish invalid lock",
        description="",
        agent_draft_ids=(),
        skill_package_ids=(),
        solution_release_ids=(release.release_id,),
        default_agent_id=None,
    )

    validated = catalog.validate(candidate.candidate_id)

    assert validated.status == "invalid"
    assert any(
        issue.code == "solution_skill_lock_mismatch"
        for issue in validated.validation.issues
    )


@pytest.mark.parametrize(
    ("gate_mode", "unpassed_status", "issue_severity"),
    [
        ("advisory", "valid", "warning"),
        ("required", "invalid", "error"),
    ],
)
def test_candidate_evaluation_gate_modes(
    tmp_path: Path,
    gate_mode,
    unpassed_status: str,
    issue_severity: str,
) -> None:
    from sage.app import DEFAULT_SKILLS_ROOT

    definition = default_diagnostic_agent()
    initial = initial_generation(
        DEFAULT_SKILLS_ROOT,
        {definition.agent_id: agent_to_dict(definition)},
        definition.agent_id,
    )
    store = LocalControlStore(tmp_path / "state", initial)
    skills = SkillPackageManager(store)
    agents = AgentDraftManager(store)
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    mcp = McpServerManager(store, settings, NoopConnector(), skills)  # type: ignore[arg-type]
    runtimes = RuntimeManager(
        store,
        AttachmentStore(tmp_path / "attachments"),
        InMemoryRunStore(),
        agent_model=DemoAgentModel(),
    )
    catalog = CatalogManager(
        store,
        agents,
        skills,
        mcp,
        runtimes,
        evaluation_gate_mode=gate_mode,
    )
    solutions = SolutionManager(store)
    evaluations = EvaluationManager(store)
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
                version=initial.skill_versions["echo"],
                content_digest=initial.skill_digests["echo"],
            ),
            SolutionComponentLock(
                kind="evaluation-suite",
                component_id="echo-conformance",
                version="1.0.0",
                content_digest="sha256:" + "e" * 64,
            ),
        ),
    )
    candidate = catalog.create_candidate(
        title="Publish evaluated Echo",
        description="",
        agent_draft_ids=(),
        skill_package_ids=(),
        solution_release_ids=(release.release_id,),
        default_agent_id=None,
    )

    missing = catalog.validate(candidate.candidate_id)
    evaluations.record(
        release.release_id,
        suite_id="echo-conformance",
        status="failed",
        metrics={"success_rate": 0.5},
        summary="Threshold not met.",
        actor_fingerprint="evaluator",
    )
    failed = catalog.validate(candidate.candidate_id)
    evaluations.record(
        release.release_id,
        suite_id="echo-conformance",
        status="passed",
        metrics={"success_rate": 1.0},
        summary="All conformance checks passed.",
        actor_fingerprint="evaluator",
    )
    passed = catalog.validate(candidate.candidate_id)

    assert missing.status == unpassed_status
    assert failed.status == unpassed_status
    assert passed.status == "valid"
    issue = next(
        issue
        for issue in missing.validation.issues
        if issue.code == "solution_evaluation_not_passed"
    )
    assert issue.severity == issue_severity
