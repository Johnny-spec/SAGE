"""End-to-end smoke for Edge orchestrator: aggregation + cross_source_context."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Note: Edge orchestrator spawns child skills which probe the live system.
# These tests must not depend on Edge actually being installed; we use
# skills that work in degraded mode (edge_qa, edge_policy).


def _run_orch(project_root: Path, ctx: dict) -> dict:
    orch = project_root / "orchestrator" / "scripts" / "edge_orchestrator.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(orch), json.dumps(ctx)],
        capture_output=True, encoding="utf-8", env=env, timeout=180,
    )
    assert proc.returncode == 0, f"orchestrator crashed: {proc.stderr}"
    return json.loads(proc.stdout)


def _load_orchestrator_module(project_root: Path):
    path = project_root / "orchestrator" / "scripts" / "edge_orchestrator.py"
    spec = importlib.util.spec_from_file_location("edge_orchestrator_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_one_rejects_malformed_child_envelope(
    project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child = tmp_path / "malformed_child.py"
    child.write_text(
        'import json\nprint(json.dumps({"skill":"probe","ok":true,'
        '"findings":[{"summary":"x","severity":"fatal"}]}))\n'.replace(
            "true", "True"
        ),
        encoding="utf-8",
    )
    orchestrator = _load_orchestrator_module(project_root)
    monkeypatch.setattr(orchestrator, "skill_entry", lambda *_: child)

    result = orchestrator._run_one("probe", {}, timeout=10)

    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    validation = result["raw"]["contract_validation"]
    assert validation["valid"] is False
    assert validation["issues"][0]["path"] == "findings[0].severity"
    assert result["raw"]["execution"]["exit_code"] == 0


def test_run_one_normalizes_skill_resolution_failure(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)

    def missing_skill(*_):
        raise KeyError("unknown skill")

    monkeypatch.setattr(orchestrator, "skill_entry", missing_skill)
    result = orchestrator._run_one("missing", {}, timeout=10)

    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "skill_resolution_failed"
    assert result["raw"]["execution"]["stage"] == "resolve"


def test_run_one_normalizes_timeout(
    project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)
    child = tmp_path / "slow_child.py"
    child.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "skill_entry", lambda *_: child)

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(orchestrator.subprocess, "run", time_out)
    result = orchestrator._run_one("slow", {}, timeout=7)

    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "timeout"
    assert result["raw"]["execution"]["timeout_seconds"] == 7


def test_run_one_hashes_child_stderr(
    project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)
    child = tmp_path / "stderr_child.py"
    child.write_text(
        "import json,sys\n"
        "sys.stderr.write('private diagnostic C:\\\\secret\\\\file.log')\n"
        "print(json.dumps({'skill':'probe','ok':True,'findings':[],'raw':{}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "skill_entry", lambda *_: child)

    result = orchestrator._run_one("probe", {}, timeout=10)
    body = json.dumps(result)

    assert result["ok"] is True
    assert "private diagnostic" not in body
    execution = result["raw"]["execution"]
    assert execution["stderr_character_count"] > 0
    assert len(execution["stderr_sha256"]) == 64


def test_orchestrator_does_not_promote_unproven_child_root(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)
    candidate = {
        "skill": "edge_policy",
        "ok": True,
        "findings": [],
        "root_cause": "candidate only",
        "confidence": "high",
        "recommendations": [],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": [],
        "cross_validation": None,
        "evidence_state": "INCONCLUSIVE_EVIDENCE",
        "evidence_gaps": [],
        "collection_plan": None,
        "raw": {},
        "error": None,
    }
    monkeypatch.setattr(orchestrator, "_run_one", lambda *args, **kwargs: candidate)

    result = orchestrator.orchestrate({
        "skills_to_trigger": ["edge_policy"],
        "problems": [{"type": "managed_browser", "severity": "info"}],
    })

    assert result.root_cause is None
    assert result.evidence_state != "ROOT_CAUSE_FOUND"
    assert result.raw["children"]["edge_policy"]["root_cause"] == "candidate only"


def test_orchestrator_propagates_proven_child_root_quality(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)
    proven = {
        "skill": "edge_policy",
        "ok": True,
        "findings": [],
        "root_cause": "directly proven cause",
        "confidence": "high",
        "recommendations": [],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": [],
        "cross_validation": None,
        "evidence_state": "ROOT_CAUSE_FOUND",
        "evidence_gaps": [],
        "collection_plan": None,
        "raw": {"quality": {
            "direct_join": True,
            "independent_sources": 1,
            "sample_sufficient": True,
            "capture_complete": True,
            "time_comparable": True,
            "blocking_gaps": False,
        }},
        "error": None,
    }
    monkeypatch.setattr(orchestrator, "_run_one", lambda *args, **kwargs: proven)

    result = orchestrator.orchestrate({
        "skills_to_trigger": ["edge_policy"],
        "problems": [],
    })

    assert result.root_cause == "edge_policy: directly proven cause"
    assert result.evidence_state == "ROOT_CAUSE_FOUND"
    assert result.raw["quality"]["direct_join"] is True


def test_orchestrator_surfaces_validator_failure(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)

    def fake_run(skill_id, *_args, **_kwargs):
        if skill_id == "validator":
            return orchestrator.operational_failure(
                "validator", "validator timed out", code="timeout"
            )
        return {
            "skill": skill_id,
            "ok": True,
            "findings": [],
            "root_cause": None,
            "confidence": "low",
            "recommendations": [],
            "solutions": [],
            "next_steps": [],
            "additional_logs_needed": [],
            "cross_validation": None,
            "evidence_state": "INCONCLUSIVE_EVIDENCE",
            "evidence_gaps": [],
            "collection_plan": None,
            "raw": {},
            "error": None,
        }

    monkeypatch.setattr(orchestrator, "_run_one", fake_run)
    result = orchestrator.orchestrate({
        "skills_to_trigger": ["edge_policy", "validator"],
        "problems": [],
    })

    assert result.ok is False
    assert any(gap.code == "validator_failure" for gap in result.evidence_gaps)
    assert result.evidence_state != "ROOT_CAUSE_FOUND"


def test_total_timeout_returns_without_waiting_for_child(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = _load_orchestrator_module(project_root)

    def slow_child(*args, **kwargs):
        time.sleep(0.3)
        return {"skill": "edge_policy", "ok": True}

    monkeypatch.setattr(orchestrator, "_run_one", slow_child)
    started = time.perf_counter()
    result = orchestrator.orchestrate(
        {
            "skills_to_trigger": ["edge_policy"],
            "problems": [{"type": "managed_browser", "severity": "info"}],
        },
        per_skill_timeout=1,
        total_timeout=0.01,
    )
    elapsed = time.perf_counter() - started

    child = result.raw["children"]["edge_policy"]
    assert elapsed < 0.2
    assert child["ok"] is False
    assert child["raw"]["failure"]["code"] == "orchestration_timeout"


def test_orchestrator_aggregates_advisories(project_root: Path):
    ctx = {
        "skills_to_trigger": ["edge_policy"],
        "problems": [{"type": "managed_browser", "severity": "info",
                       "summary": "Edge is managed by policy"}],
    }
    result = _run_orch(project_root, ctx)
    assert result["ok"] is True
    raw = result["raw"]
    assert "cross_source_context" in raw
    assert "operator_summary" in raw
    assert isinstance(raw["operator_summary"], str)
    assert raw["operator_summary"].startswith("# Edge orchestrator summary")

    # Aggregated advisories must be populated (or at minimum present as lists)
    assert isinstance(result["solutions"], list)
    assert isinstance(result["next_steps"], list)
    assert isinstance(result["additional_logs_needed"], list)


def test_orchestrator_no_follow_ups_returns_noop(project_root: Path):
    ctx = {"skills_to_trigger": [], "problems": []}
    result = _run_orch(project_root, ctx)
    assert result["ok"] is True
    assert any("Nothing to orchestrate" in f.get("summary", "")
                for f in result.get("findings", []))


def test_cross_source_context_tracks_sources(project_root: Path):
    ctx = {
        "skills_to_trigger": ["edge_policy"],
        "problems": [{"type": "managed_browser", "severity": "info"}],
    }
    result = _run_orch(project_root, ctx)
    cs = result["raw"]["cross_source_context"]
    assert "sources_used" in cs
    assert "skills_run" in cs
    assert "edge_policy" in cs["skills_run"]
    # edge_policy uses the registry source
    assert "registry" in cs["sources_used"]


def test_aggregated_advisories_have_no_duplicate_keys(project_root: Path):
    ctx = {
        "skills_to_trigger": ["edge_policy", "edge_extensions"],
        "problems": [
            {"type": "managed_browser", "severity": "info"},
            {"type": "extension_issue", "severity": "warning"},
        ],
    }
    result = _run_orch(project_root, ctx)
    sols = result["solutions"]
    keys = [(s.get("problem_ref"), s.get("title")) for s in sols]
    assert len(keys) == len(set(keys)), f"duplicate solutions: {keys}"
    nxts = result["next_steps"]
    nkeys = [(n.get("action"), n.get("skill")) for n in nxts]
    assert len(nkeys) == len(set(nkeys)), f"duplicate next_steps: {nkeys}"


def test_orchestrator_passes_problem_summary_to_known_issue(project_root: Path):
    ctx = {
        "skills_to_trigger": ["edge_known_issue"],
        "problems": [{
            "type": "known_issue",
            "severity": "warning",
            "summary": "Edge 149 works but 150 download popup hangs",
        }],
        "extra": {"use_known_issue_mcp": False},
    }
    result = _run_orch(project_root, ctx)
    assert result["ok"] is True
    child = result["raw"]["children"]["edge_known_issue"]
    assert child["ok"] is True
    assert child["raw"]["problem_text"] == "Edge 149 works but 150 download popup hangs"
    assert child["raw"]["known_issue_lookup"]["enabled"] is False
    assert "Known-issue gaps" in result["raw"]["operator_summary"]


def test_validator_is_final_reducer_not_parallel_child(project_root: Path):
    analyses = {
        "model_a": {
            "confidence": 0.8,
            "claims": [{
                "claim_key": "policy-extension",
                "conclusion": "Managed configuration contributes to the failure",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The same failure occurs with unmanaged configuration"],
                "confidence": 0.8,
            }],
        },
        "model_b": {
            "confidence": 0.7,
            "claims": [{
                "claim_key": "policy-extension",
                "conclusion": "Managed configuration contributes to the failure",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The same failure occurs with unmanaged configuration"],
                "confidence": 0.7,
            }],
        },
    }
    result = _run_orch(project_root, {
        "skills_to_trigger": ["edge_policy", "edge_extensions", "validator"],
        "problems": [{"type": "page_load_failure", "severity": "critical"}],
        "extra": {"model_analyses": analyses},
    })
    assert "validator" not in result["raw"]["children"]
    assert result["raw"]["validation"]["skill"] == "validator"
    assert result["cross_validation"]["status"] == "completed"
    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert result["confidence"] == "low"


def test_edge_orchestrator_uses_explicit_source_roots(project_root: Path, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "network.cc").write_text(
        'TRACE_EVENT("net", "SSL_CONNECT");', encoding="utf-8"
    )
    result = _run_orch(project_root, {
        "skills_to_trigger": [],
        "problems": [],
        "extra": {"repo_roots": [str(source)]},
    })
    child = result["raw"]["children"]["source_logging_discovery"]
    assert child["ok"] is True
    assert child["raw"]["logging_discovery"]["safety"]["target_code_executed"] is False
    assert result["collection_plan"]["requests"]
