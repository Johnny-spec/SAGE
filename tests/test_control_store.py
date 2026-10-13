import json
from pathlib import Path

from sage.control_models import (
    CatalogCandidateRecord,
    GenerationRecord,
    SolutionComponentLock,
    SolutionDefinitionRecord,
    SolutionReleaseRecord,
)
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest, directory_digest
from sage.evaluations import EvaluationManager, evaluation_report_digest
from sage.solutions import SolutionManager

FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def _initial_generation(skills_root: Path) -> GenerationRecord:
    return GenerationRecord(
        generation_id="gen_startup",
        content_digest="sha256:startup",
        skills_root=str(skills_root),
        skill_versions={"echo": "1.0.0"},
        agent_definitions={},
    )


def test_local_control_store_survives_reopen(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path / "data", _initial_generation(tmp_path))

    def add_candidate(state: object) -> None:
        state.candidates["cand_1"] = CatalogCandidateRecord(  # type: ignore[attr-defined]
            candidate_id="cand_1",
            base_generation_id="gen_startup",
            title="Add diagnostics",
        )

    store.update(add_candidate)
    reopened = LocalControlStore(tmp_path / "data", _initial_generation(tmp_path))
    state = reopened.snapshot()

    assert state.active_generation_id == "gen_startup"
    assert state.candidates["cand_1"].title == "Add diagnostics"


def test_local_control_store_persists_solution_release_lock(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path / "data", _initial_generation(tmp_path))
    digest = "sha256:" + "a" * 64
    solution = SolutionDefinitionRecord(
        solution_id="incident-diagnostics",
        title="Incident diagnostics",
        owner="platform-team",
        entry_agent_id="diagnostic-agent",
    )
    release = SolutionReleaseRecord(
        release_id="incident-diagnostics@1.0.0",
        solution_id=solution.solution_id,
        version="1.0.0",
        content_digest=digest,
        components=(
            SolutionComponentLock(
                kind="agent",
                component_id="diagnostic-agent",
                version="1.0.0",
                content_digest=digest,
            ),
        ),
        entry_agent_id="diagnostic-agent",
        source_generation_id="gen_startup",
    )

    def add_solution(state: object) -> None:
        state.solutions[solution.solution_id] = solution  # type: ignore[attr-defined]
        state.solution_releases[release.release_id] = release  # type: ignore[attr-defined]

    store.update(add_solution)
    reopened = LocalControlStore(tmp_path / "data", _initial_generation(tmp_path))
    state = reopened.snapshot()

    assert state.schema_version == 13
    assert state.solutions[solution.solution_id].owner == "platform-team"
    assert state.solution_releases[release.release_id].components[0].kind == "agent"


def test_control_store_rejects_artifact_path_escape(tmp_path: Path) -> None:
    store = LocalControlStore(tmp_path / "data", _initial_generation(tmp_path))

    try:
        store.resolve_path("../outside")
    except ValueError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("path escape was accepted")


def test_control_store_migrates_v1_generation_digest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    LocalControlStore(data_root, _initial_generation(tmp_path))
    raw = json.loads((data_root / "control-plane.json").read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    raw["generations"]["gen_startup"]["content_digest"] = "sha256:legacy"
    (data_root / "control-plane.json").write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(tmp_path))

    assert reopened.snapshot().schema_version == 13
    assert reopened.snapshot().generations["gen_startup"].content_digest != "sha256:legacy"


def test_control_store_migrates_v2_skill_digests(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    LocalControlStore(data_root, _initial_generation(FIXTURES))
    raw = json.loads((data_root / "control-plane.json").read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    raw["generations"]["gen_startup"]["skill_digests"] = {"echo": "sha256:empty"}
    (data_root / "control-plane.json").write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(FIXTURES))

    state = reopened.snapshot()
    assert state.schema_version == 13
    assert state.generations["gen_startup"].skill_digests["echo"] != "sha256:empty"


def test_control_store_migrates_v4_generation_digest_format(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    LocalControlStore(data_root, _initial_generation(FIXTURES))
    state_path = data_root / "control-plane.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 4
    raw["generations"]["gen_startup"]["skill_digests"] = {
        "echo": directory_digest(FIXTURES / "echo")
    }
    raw["generations"]["gen_startup"]["content_digest"] = "sha256:legacy-v4"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(FIXTURES))

    state = reopened.snapshot()
    assert state.schema_version == 13
    assert state.generations["gen_startup"].content_digest != "sha256:legacy-v4"


def test_control_store_migrates_v5_with_empty_solution_catalog(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    LocalControlStore(data_root, _initial_generation(tmp_path))
    state_path = data_root / "control-plane.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 5
    raw.pop("solutions")
    raw.pop("solution_releases")
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(tmp_path))

    state = reopened.snapshot()
    assert state.schema_version == 13
    assert state.solutions == {}
    assert state.solution_releases == {}


def test_control_store_migrates_v6_solution_release_digest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = LocalControlStore(data_root, _initial_generation(tmp_path))
    digest = "sha256:" + "a" * 64
    solution = SolutionDefinitionRecord(
        solution_id="incident-diagnostics",
        title="Incident diagnostics",
        owner="platform-team",
    )
    release = SolutionReleaseRecord(
        release_id="incident-diagnostics@1.0.0",
        solution_id=solution.solution_id,
        version="1.0.0",
        content_digest=digest,
        components=(
            SolutionComponentLock(
                kind="skill",
                component_id="echo",
                version="1.0.0",
                content_digest=digest,
            ),
        ),
    )

    def save(state: object) -> None:
        state.solutions[solution.solution_id] = solution  # type: ignore[attr-defined]
        state.solution_releases[release.release_id] = release  # type: ignore[attr-defined]

    store.update(save)
    state_path = data_root / "control-plane.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 6
    raw["solutions"][solution.solution_id].pop("allowed_roles")
    raw["solution_releases"][release.release_id].pop("allowed_roles")
    raw["solution_releases"][release.release_id]["content_digest"] = digest
    raw["generations"]["gen_startup"].pop("solution_releases")
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(tmp_path))

    state = reopened.snapshot()
    migrated = state.solution_releases[release.release_id]
    assert state.schema_version == 13
    assert state.solutions[solution.solution_id].allowed_roles == ("user",)
    assert migrated.allowed_roles == ("user",)
    assert migrated.content_digest != digest
    assert state.generations["gen_startup"].solution_releases == {}


def test_control_store_migrates_v7_with_empty_evaluation_catalog(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    LocalControlStore(data_root, _initial_generation(tmp_path))
    state_path = data_root / "control-plane.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 7
    raw.pop("evaluations")
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = LocalControlStore(data_root, _initial_generation(tmp_path))

    state = reopened.snapshot()
    assert state.schema_version == 13
    assert state.evaluations == {}


def test_control_store_migrates_v8_evaluation_to_release_digest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = LocalControlStore(data_root, _initial_generation(tmp_path))
    solutions = SolutionManager(store)
    solutions.create(
        solution_id="evaluated-solution",
        title="Evaluated",
        description="",
        owner="platform-team",
        entry_agent_id=None,
        supported_profiles=("direct",),
    )
    component = SolutionComponentLock(
        kind="evaluation-suite",
        component_id="conformance",
        version="1.0.0",
        content_digest="sha256:" + "e" * 64,
    )
    release = solutions.create_release(
        "evaluated-solution",
        version="1.0.0",
        components=(component,),
    )
    report = EvaluationManager(store).record(
        release.release_id,
        suite_id="conformance",
        status="passed",
        metrics={"success_rate": 1.0},
        summary="Passed.",
        actor_fingerprint="evaluator",
    )
    state_path = data_root / "control-plane.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 8
    raw_release = raw["solution_releases"][release.release_id]
    raw_release.pop("component_paths")
    raw_release.pop("package_relpath")
    legacy_release_digest = canonical_digest(
        {
            "solution_id": release.solution_id,
            "version": release.version,
            "entry_agent_id": release.entry_agent_id,
            "allowed_roles": release.allowed_roles,
            "supported_profiles": release.supported_profiles,
            "components": [component.model_dump(mode="json")],
        }
    )
    raw_release["content_digest"] = legacy_release_digest
    raw_report = raw["evaluations"][report.report_id]
    raw_report["release_digest"] = legacy_release_digest
    raw_report["content_digest"] = canonical_digest(
        {
            "release_id": raw_report["release_id"],
            "release_digest": legacy_release_digest,
            "suite_id": raw_report["suite_id"],
            "suite_version": raw_report["suite_version"],
            "suite_digest": raw_report["suite_digest"],
            "status": raw_report["status"],
            "metrics": raw_report["metrics"],
            "summary": raw_report["summary"],
            "actor_fingerprint": raw_report["actor_fingerprint"],
        }
    )
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    migrated = LocalControlStore(data_root, _initial_generation(tmp_path)).snapshot()

    migrated_release = migrated.solution_releases[release.release_id]
    migrated_report = migrated.evaluations[report.report_id]
    assert migrated.schema_version == 13
    assert migrated_report.release_digest == migrated_release.content_digest
    assert evaluation_report_digest(migrated_report) == migrated_report.content_digest


def test_version_eleven_migration_preserves_historical_records(tmp_path: Path):
    generation = _initial_generation(tmp_path)
    store = LocalControlStore(tmp_path / "state", generation)
    before = store.snapshot().model_dump(mode="json")
    before["schema_version"] = 11
    before.pop("mcp_tests")
    before.pop("mcp_test_reviews")
    migrated = LocalControlStore._migrate(before)
    assert migrated["schema_version"] == 13
    assert migrated["generations"] == before["generations"]
    assert migrated["approvals"] == before["approvals"]
    assert migrated["tool_registrations"] == before["tool_registrations"]
    assert migrated["mcp_tests"] == {}


def test_version_twelve_adds_drafts_without_rehashing(tmp_path: Path):
    store = LocalControlStore(tmp_path / "state", _initial_generation(tmp_path))
    before = store.snapshot().model_dump(mode="json")
    before["schema_version"] = 12
    before.pop("workflow_drafts")
    migrated = LocalControlStore._migrate(before)
    assert migrated["schema_version"] == 13
    assert migrated["workflow_drafts"] == {}
    assert {key: value for key, value in migrated.items()
            if key not in {"schema_version", "workflow_drafts"}} == {
                key: value for key, value in before.items() if key != "schema_version"
            }
