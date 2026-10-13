"""Edge wrapper and evidence-driven routing tests for Procmon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_skill(project_root: Path, context: dict) -> tuple[subprocess.CompletedProcess[str], dict]:
    script = project_root / "edge_procmon" / "scripts" / "procmon_analyzer.py"
    process = subprocess.run(
        [sys.executable, str(script), json.dumps(context)],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
    )
    return process, json.loads(process.stdout)


def _write_trace(path: Path) -> None:
    path.write_text(
        "Date & Time,Process Name,Parent PID,PID,TID,Operation,Result,Path,Detail\n"
        + (
            "2026-08-20 17:08:40,msedge.exe,4,100,200,Load Image,NAME NOT FOUND,"
            "C:\\Edge\\missing.dll,Image lookup\n"
        ) * 25,
        encoding="utf-8",
    )


def test_edge_procmon_no_input_is_soft_missing_evidence(project_root: Path):
    process, result = _run_skill(project_root, {})

    assert process.returncode == 0
    assert result["skill"] == "edge_procmon"
    assert result["ok"] is True
    assert result["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert result["additional_logs_needed"][0]["log_kind"] == "procmon"


def test_edge_procmon_analyzes_supplied_capture(project_root: Path, tmp_path: Path):
    trace = tmp_path / "trace.csv"
    _write_trace(trace)
    process, result = _run_skill(project_root, {
        "problem_type": "crash",
        "question": "Edge crashes during startup when a required DLL cannot load",
        "time_range": {
            "start": "2026-08-20T17:08:39",
            "end": "2026-08-20T17:08:41",
        },
        "extra": {"procmon_paths": [str(trace)]},
    })

    assert process.returncode == 0, process.stderr
    assert result["skill"] == "edge_procmon"
    assert result["root_cause"] is None
    assert result["raw"]["candidate_cause"]


def test_edge_orchestrator_auto_adds_only_supplied_procmon(
    project_root: Path, tmp_path: Path,
):
    orchestrator = project_root / "orchestrator" / "scripts" / "edge_orchestrator.py"
    trace = tmp_path / "trace.csv"
    _write_trace(trace)
    base = {
        "skills_to_trigger": ["edge_policy"],
        "problems": [{"type": "slow_startup", "severity": "warning"}],
    }
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    without = subprocess.run(
        [sys.executable, str(orchestrator), json.dumps(base)],
        capture_output=True, encoding="utf-8", env=env, timeout=90,
    )
    with_evidence = subprocess.run(
        [sys.executable, str(orchestrator), json.dumps({
            **base,
            "time_range": {
                "start": "2026-08-20T17:08:39",
                "end": "2026-08-20T17:08:41",
            },
            "extra": {"procmon_paths": [str(trace)]},
        })],
        capture_output=True, encoding="utf-8", env=env, timeout=90,
    )
    without_result = json.loads(without.stdout)
    with_result = json.loads(with_evidence.stdout)

    assert without.returncode == 0, without.stderr
    assert with_evidence.returncode == 0, with_evidence.stderr
    assert "edge_procmon" not in without_result["raw"]["children"]
    assert with_result["raw"]["children"]["edge_procmon"]["ok"] is True
    assert "procmon" in with_result["raw"]["cross_source_context"]["sources_used"]


def test_empty_procmon_folder_does_not_claim_source_was_used(
    project_root: Path, tmp_path: Path,
):
    orchestrator = project_root / "orchestrator" / "scripts" / "edge_orchestrator.py"
    process = subprocess.run(
        [sys.executable, str(orchestrator), json.dumps({
            "skills_to_trigger": ["edge_policy"],
            "problems": [{"type": "slow_startup", "severity": "warning"}],
            "extra": {"procmon_folder": str(tmp_path)},
        })],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=90,
    )
    result = json.loads(process.stdout)

    assert process.returncode == 0, process.stderr
    assert result["raw"]["children"]["edge_procmon"]["ok"] is True
    assert result["raw"]["children"]["edge_procmon"]["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert "procmon" not in result["raw"]["cross_source_context"]["sources_used"]