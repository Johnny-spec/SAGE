"""End-to-end test: pipe iis_analyzer output into the orchestrator via stdin."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import skill_orchestrator as orchestrator  # type: ignore[reportMissingImports]


def test_pipeline_iis_then_orchestrator(project_root: Path, sample_iis_log: Path):
    iis = project_root / "IIS_logs" / "scripts" / "iis_analyzer.py"
    orch = project_root / "orchestrator" / "scripts" / "skill_orchestrator.py"

    p1 = subprocess.run(
        [sys.executable, str(iis), str(sample_iis_log)],
        capture_output=True, text=True, check=True,
    )
    iis_result = json.loads(p1.stdout)
    assert iis_result["ok"] is True
    assert iis_result["skill"] == "iis_logs"
    triggered = iis_result["raw"]["skills_to_trigger"]
    assert triggered, "expected at least one follow-up skill"

    ctx = json.dumps(iis_result["raw"])
    p2 = subprocess.run(
        [sys.executable, str(orch)],
        input=ctx, capture_output=True, text=True,
    )
    assert p2.returncode in (0, 1), f"orchestrator crashed: {p2.stderr}"
    orch_result = json.loads(p2.stdout)
    assert orch_result["skill"] == "orchestrator"

    assert set(orch_result["raw"]["executed"]) == set(triggered)
    results = orch_result["raw"]["results"]
    assert set(results.keys()) == set(triggered)
    for skill_id, res in results.items():
        assert res.get("skill") == skill_id, f"{skill_id}: {res}"


def test_legacy_orchestrator_adds_procmon_only_when_evidence_is_supplied(tmp_path: Path):
    trace = tmp_path / "trace.csv"
    trace.write_text(
        "Date & Time,Process Name,PID,Operation,Result,Path,Detail\n"
        + (
            "2026-08-20 17:08:40,w3wp.exe,100,CreateFile,ACCESS DENIED,"
            "C:\\inetpub\\wwwroot\\web.config,Desired Access: Read\n"
        ) * 25,
        encoding="utf-8",
    )
    result = orchestrator.orchestrate({
        "skills_to_trigger": [],
        "problems": [],
        "extra": {"procmon_paths": [str(trace)]},
    }, per_skill_timeout=30, total_timeout=60)

    assert result.raw["executed"] == ["procmon"]
    assert result.raw["results"]["procmon"]["ok"] is True


def test_iis_procmon_cross_context_never_claims_exact_join():
    context = orchestrator._cross_log_context(
        {
            orchestrator.IIS_KIND: ["iis.log"],
            orchestrator.PROCMON_KIND: ["trace.csv"],
        },
        {"start": "2026-08-20T17:00:00", "end": "2026-08-20T17:10:00"},
        {"procmon": {"ok": True, "raw": {"quality": {"time_comparable": True}}}},
    )

    assert context["pairs"]["iis_procmon"]["quality"] == "time_only"
    assert "no request/PID join proven" in context["pairs"]["iis_procmon"]["basis"]


def test_python_orchestrator_prefers_complete_runtime(project_root: Path):
    if not shutil.which("pwsh"):
        pytest.skip("PowerShell 7 is not installed")

    orch = project_root / "orchestrator" / "scripts" / "skill_orchestrator.py"
    payload = {
        "metrics": {
            "time_range": {
                "start": "2099-01-15T10:00:00",
                "end": "2099-01-15T10:05:00",
            }
        },
        "problems": [{"type": "auth_error", "evidence": {}}],
        "skills_to_trigger": ["security_audit"],
    }
    proc = subprocess.run(
        [sys.executable, str(orch)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    result = json.loads(proc.stdout)
    assert result["raw"]["runtimes"]["security_audit"] == "pwsh"
    child = result["raw"]["results"]["security_audit"]
    assert child["raw"].get("status") != "framework"


def test_parse_skill_process_preserves_failure_envelope():
    child = {
        "skill": "demo",
        "ok": False,
        "findings": [],
        "error": "structured failure",
        "raw": {"reason": "access_denied"},
    }
    proc = subprocess.CompletedProcess(
        args=["python", "demo.py"],
        returncode=2,
        stdout=json.dumps(child),
        stderr="diagnostic stderr",
    )
    result = orchestrator._parse_skill_process(
        "demo", "python", list(proc.args), proc
    )
    assert result["error"] == "structured failure"
    assert result["raw"]["reason"] == "access_denied"
    assert result["raw"]["execution"]["exit_code"] == 2
    assert result["raw"]["execution"]["runtime"] == "python"
    assert "diagnostic stderr" not in json.dumps(result)
    assert len(result["raw"]["execution"]["stderr_sha256"]) == 64
    assert result["raw"]["contract_validation"]["valid"] is True


def test_parse_skill_process_rejects_malformed_envelope():
    child = {
        "skill": "demo",
        "ok": True,
        "findings": [{"summary": "x", "severity": "fatal"}],
    }
    proc = subprocess.CompletedProcess(
        args=["python", "demo.py"],
        returncode=0,
        stdout=json.dumps(child),
        stderr="",
    )

    result = orchestrator._parse_skill_process(
        "demo", "python", list(proc.args), proc
    )

    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    validation = result["raw"]["contract_validation"]
    assert validation["valid"] is False
    assert validation["issues"][0]["path"] == "findings[0].severity"
    assert result["raw"]["execution"]["exit_code"] == 0


def test_run_one_normalizes_runtime_resolution_failure(monkeypatch):
    def unavailable_runtime(*_):
        raise RuntimeError("no complete runtime")

    monkeypatch.setattr(orchestrator, "_resolve_skill_runtime", unavailable_runtime)
    result = orchestrator._run_one("missing", {}, timeout=10)

    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "skill_resolution_failed"
    assert result["raw"]["execution"]["stage"] == "resolve"


def test_run_one_normalizes_timeout(tmp_path: Path, monkeypatch):
    child = tmp_path / "slow.py"
    child.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator,
        "_resolve_skill_runtime",
        lambda *_: ("python", child, sys.executable),
    )

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(orchestrator.subprocess, "run", time_out)
    result = orchestrator._run_one("slow", {}, timeout=9)

    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "timeout"
    assert result["raw"]["execution"]["timeout_seconds"] == 9


def test_parse_skill_process_rejects_nonzero_success_envelope():
    child = {
        "skill": "demo",
        "ok": True,
        "root_cause": "must not survive",
        "confidence": "high",
        "findings": [],
        "raw": {},
    }
    proc = subprocess.CompletedProcess(
        args=["python", "demo.py"],
        returncode=2,
        stdout=json.dumps(child),
        stderr="sensitive child detail",
    )

    result = orchestrator._parse_skill_process(
        "demo", "python", list(proc.args), proc
    )

    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["confidence"] == "low"
    assert result["raw"]["failure"]["code"] == "child_exit_mismatch"
    assert "sensitive child detail" not in json.dumps(result)


def test_orchestrator_does_not_promote_unproven_child_root(monkeypatch):
    candidate = {
        "skill": "event_log",
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
        "metrics": {"time_range": {}},
        "problems": [{"type": "5xx_error"}],
        "skills_to_trigger": ["event_log"],
        "extra": {"skip_validation": True},
    })

    assert result.root_cause is None
    assert result.evidence_state != "ROOT_CAUSE_FOUND"
    assert result.raw["results"]["event_log"]["root_cause"] == "candidate only"


def test_orchestrator_propagates_proven_child_root_quality(monkeypatch):
    proven = {
        "skill": "event_log",
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
        "metrics": {"time_range": {}},
        "problems": [],
        "skills_to_trigger": ["event_log"],
        "extra": {"skip_validation": True},
    })

    assert result.root_cause == "directly proven cause"
    assert result.evidence_state == "ROOT_CAUSE_FOUND"
    assert result.raw["quality"]["direct_join"] is True


def test_orchestrator_surfaces_validator_failure(monkeypatch):
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
        "metrics": {"time_range": {}},
        "problems": [],
        "skills_to_trigger": ["event_log", "validator"],
    })

    assert result.ok is False
    assert any(gap.code == "validator_failure" for gap in result.evidence_gaps)
    assert result.evidence_state != "ROOT_CAUSE_FOUND"


def test_total_timeout_returns_failure_envelope(monkeypatch):
    def slow_skill(skill_id, ctx, timeout):
        time.sleep(0.1)
        return {"skill": skill_id, "ok": True, "findings": [], "raw": {}}

    monkeypatch.setattr(orchestrator, "_run_one", slow_skill)
    result = orchestrator.orchestrate(
        {
            "metrics": {"time_range": {}},
            "problems": [{"type": "5xx_error"}],
            "skills_to_trigger": ["slow"],
        },
        per_skill_timeout=1,
        total_timeout=0.01,
    )
    assert result.ok is False
    assert "total orchestration timeout" in result.raw["results"]["slow"]["error"]


def test_folder_secondary_failure_sets_top_level_failure(
    monkeypatch, tmp_path: Path, sample_iis_log: Path
):
    incident = tmp_path / "incident"
    incident.mkdir()
    shutil.copy(sample_iis_log, incident / "u_ex.log")

    monkeypatch.setattr(
        orchestrator,
        "_run_cli_skill",
        lambda *args, **kwargs: {
            "skill": "iis_logs",
            "ok": True,
            "findings": [],
            "raw": {
                "problems": [{"type": "5xx_error", "evidence": {}}],
                "skills_to_trigger": ["not_registered"],
            },
        },
    )
    result = orchestrator.orchestrate_folder(
        folder=incident,
        around=[],
        window="5m",
        error_pattern=None,
        recursive=True,
        per_skill_timeout=1,
        total_timeout=1,
        report_path=None,
    )
    assert result.ok is False
    assert any("secondary:not_registered" in item for item in result.recommendations)


def test_cross_log_context_requires_calibrated_netlog_time():
    relative = orchestrator._cross_log_context(
        None,
        {"start": "2026-08-11T10:00:00Z", "end": "2026-08-11T10:05:00Z"},
        {
            "iis_logs": {"ok": True},
            "netlog": {
                "ok": True,
                "raw": {"per_file": [{"time_model": {"calibrated": False}}]},
            },
        },
    )
    assert relative["pairs"]["iis_netlog"]["quality"] == "unavailable"
    assert relative["correlatable"] is False

    calibrated = orchestrator._cross_log_context(
        None,
        {"start": "2026-08-11T10:00:00Z", "end": "2026-08-11T10:05:00Z"},
        {
            "iis_logs": {"ok": True},
            "netlog": {
                "ok": True,
                "raw": {"per_file": [{"time_model": {"calibrated": True}}]},
            },
        },
    )
    assert calibrated["pairs"]["iis_netlog"]["quality"] == "time_only"
    assert calibrated["correlatable"] is True


def test_iis_validator_runs_after_evidence_children(monkeypatch):
    real_run_one = orchestrator._run_one

    def fake_run_one(skill_id, ctx, timeout):
        if skill_id == "validator":
            return real_run_one(skill_id, ctx, timeout)
        return {
            "skill": skill_id,
            "ok": True,
            "confidence": "medium",
            "findings": [{
                "summary": f"{skill_id} evidence",
                "severity": "warning",
                "evidence": {"component": skill_id},
            }],
            "root_cause": None,
            "recommendations": [],
            "solutions": [],
            "next_steps": [],
            "additional_logs_needed": [],
            "raw": {},
        }

    monkeypatch.setattr(orchestrator, "_run_one", fake_run_one)
    analyses = {
        "model_a": {
            "confidence": 0.8,
            "claims": [{
                "claim_key": "shared-cause",
                "conclusion": "Shared evidence confirms the failing component",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The referenced evidence belongs to a different component"],
                "confidence": 0.8,
            }],
        },
        "model_b": {
            "confidence": 0.7,
            "claims": [{
                "claim_key": "shared-cause",
                "conclusion": "Shared evidence confirms the failing component",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The referenced evidence belongs to a different component"],
                "confidence": 0.7,
            }],
        },
    }
    result = orchestrator.orchestrate({
        "metrics": {"time_range": {}},
        "problems": [{"type": "5xx_error", "severity": "critical"}],
        "skills_to_trigger": ["app_crash", "event_log"],
        "extra": {"model_analyses": analyses},
    })

    assert result.cross_validation is not None
    assert result.cross_validation["status"] == "completed"
    assert result.raw["validation"]["skill"] == "validator"
    assert "validator" not in result.raw["executed"]
    assert result.root_cause is None
    assert result.evidence_state != "ROOT_CAUSE_FOUND"
    assert result.confidence == "low"


def test_iis_folder_cli_runs_explicit_source_discovery(
    project_root: Path, tmp_path: Path
):
    incident = tmp_path / "incident"
    source = tmp_path / "source"
    incident.mkdir()
    source.mkdir()
    (source / "web.config").write_text(
        '<configuration><system.webServer><windowsAuthentication enabled="true" /></system.webServer></configuration>',
        encoding="utf-8",
    )
    script = project_root / "orchestrator" / "scripts" / "skill_orchestrator.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(incident), "--source-root", str(source), "--quiet"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    source_result = result["raw"]["results"]["source_logging_discovery"]
    assert source_result["raw"]["logging_discovery"]["safety"]["target_code_executed"] is False
    assert source_result["collection_plan"]["requests"]
    assert result["evidence_state"] == "COLLECTION_PLAN_READY"


def test_powershell_orchestrator_runs_common_validator_last(project_root: Path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    analyses = {
        "model_a": {
            "confidence": 0.8,
            "claims": [{
                "claim_key": "shared-cause",
                "conclusion": "Independent evidence confirms the component",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The referenced evidence belongs to a different component"],
                "confidence": 0.8,
            }],
        },
        "model_b": {
            "confidence": 0.7,
            "claims": [{
                "claim_key": "shared-cause",
                "conclusion": "Independent evidence confirms the component",
                "evidence_refs": ["E0001"],
                "falsifiers": ["The referenced evidence belongs to a different component"],
                "confidence": 0.7,
            }],
        },
    }
    payload = {
        "metrics": {
            "time_range": {
                "start": "2099-01-15T10:00:00",
                "end": "2099-01-15T10:05:00",
            }
        },
        "problems": [{"type": "5xx_error", "evidence": {}}],
        "skills_to_trigger": ["httperror", "app_crash"],
        "extra": {"model_analyses": analyses},
    }
    script = project_root / "orchestrator" / "scripts" / "skill_orchestrator.ps1"
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script),
         "-Context", json.dumps(payload), "-Runtime", "Auto"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert "validator" not in result["raw"]["executed"]
    assert result["raw"]["validation"]["skill"] == "validator"
    assert result["cross_validation"]["status"] == "completed"
    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert result["confidence"] == "low"


def test_powershell_orchestrator_uses_common_source_discovery(
    project_root: Path, tmp_path: Path
):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    (tmp_path / "web.config").write_text(
        '<configuration><system.webServer><windowsAuthentication enabled="true" /></system.webServer></configuration>',
        encoding="utf-8",
    )
    payload = {
        "metrics": {"time_range": {}},
        "problems": [],
        "skills_to_trigger": [],
        "extra": {"repo_roots": [str(tmp_path)]},
    }
    script = project_root / "orchestrator" / "scripts" / "skill_orchestrator.ps1"
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script),
         "-Context", json.dumps(payload), "-Runtime", "Auto"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    source = result["raw"]["results"]["source_logging_discovery"]
    assert source["raw"]["logging_discovery"]["safety"]["target_code_executed"] is False
    assert result["evidence_state"] == "COLLECTION_PLAN_READY"


@pytest.mark.parametrize(
    ("runtime", "expected_runtime"),
    [("Pwsh", "pwsh"), ("Python", "python")],
)
def test_powershell_orchestrator_selects_runtime(
    project_root: Path, runtime: str, expected_runtime: str
):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")

    script = project_root / "orchestrator" / "scripts" / "skill_orchestrator.ps1"
    payload = {
        "metrics": {
            "time_range": {
                "start": "2025-01-15T10:00:00",
                "end": "2025-01-15T10:05:00",
            }
        },
        "problems": [{"type": "5xx_error", "evidence": {}}],
        "skills_to_trigger": ["httperror"],
    }

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Context",
            json.dumps(payload),
            "-Runtime",
            runtime,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    result = json.loads(proc.stdout)
    assert result["skill"] == "orchestrator"
    assert result["ok"] is True
    assert result["raw"]["executed"] == ["httperror"]
    assert result["raw"]["runtimes"]["httperror"] == expected_runtime
    assert result["raw"]["results"]["httperror"]["skill"] == "httperror"


def test_powershell_orchestrator_auto_falls_back_to_python(
    project_root: Path, tmp_path: Path
):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")

    netlog = tmp_path / "netlog.json"
    netlog.write_text(
        json.dumps(
            {
                "constants": {
                    "logSourceType": {"URL_REQUEST": 1},
                    "logEventTypes": {"URL_REQUEST_START_JOB": 100},
                    "logEventPhase": {
                        "PHASE_BEGIN": 1,
                        "PHASE_END": 2,
                        "PHASE_NONE": 0,
                    },
                },
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "metrics": {"time_range": {}},
        "problems": [{"type": "client_request_failure", "evidence": {}}],
        "skills_to_trigger": ["netlog"],
        "extra": {"netlog_paths": [str(netlog)]},
    }
    script = project_root / "orchestrator" / "scripts" / "skill_orchestrator.ps1"
    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Context",
            json.dumps(payload),
            "-Runtime",
            "Auto",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["raw"]["runtimes"]["netlog"] == "python"
    assert result["raw"]["results"]["netlog"]["skill"] == "netlog"
