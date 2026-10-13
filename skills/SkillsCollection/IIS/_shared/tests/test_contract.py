"""Unit tests for _shared/contract.py."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import base64
import json
import shutil
import subprocess

import pytest

import contract


def test_load_context_inline_json():
    ctx = contract.load_context(['{"problem_type":"5xx_error"}'])
    assert ctx["problem_type"] == "5xx_error"


def test_load_context_at_file(tmp_path):
    f = tmp_path / "ctx.json"
    f.write_text('{"problem_type":"high_latency"}', encoding="utf-8")
    ctx = contract.load_context([f"@{f}"])
    assert ctx["problem_type"] == "high_latency"


def test_load_context_empty():
    assert contract.load_context([]) == {}


def test_parse_time_iso_z_suffix():
    t = contract.parse_time("2025-01-15T10:00:00Z")
    assert t.year == 2025 and t.minute == 0


def test_time_range_returns_two():
    start, end = contract.time_range(
        {"time_range": {"start": "2025-01-15T10:00:00", "end": "2025-01-15T11:00:00"}}
    )
    assert (end - start) == timedelta(hours=1)


def test_in_window_tolerance():
    s = datetime(2025, 1, 15, 10, 0, 0)
    e = datetime(2025, 1, 15, 11, 0, 0)
    assert contract.in_window(s - timedelta(minutes=1), s, e, tolerance_minutes=2)
    assert not contract.in_window(s - timedelta(minutes=5), s, e, tolerance_minutes=2)


def test_skill_result_envelope_keys():
    d = asdict(contract.SkillResult(skill="iis_logs", ok=True))
    for key in ("skill", "ok", "findings", "root_cause", "confidence",
                "recommendations", "cross_validation", "raw", "error",
                "evidence_state", "evidence_gaps", "collection_plan",
                "generated_at"):
        assert key in d


def test_skill_result_to_json_round_trip():
    import json
    parsed = json.loads(contract.SkillResult(skill="iis_logs").to_json())
    assert parsed["skill"] == "iis_logs"


def test_registry_has_iis_logs_and_skill_entry_returns_path():
    reg = contract.registry()
    assert "iis_logs" in reg["skills"]
    p_py = contract.skill_entry("iis_logs", runtime="python")
    assert p_py.suffix == ".py"
    p_ps = contract.skill_entry("event_log", runtime="pwsh")
    assert p_ps.suffix == ".ps1"
    orchestrator_ps = contract.skill_entry("orchestrator", runtime="pwsh")
    assert orchestrator_ps.name == "skill_orchestrator.ps1"
    procmon = contract.skill_entry("procmon", runtime="python")
    assert procmon.name == "procmon_analyzer.py"


def test_skill_entry_unknown_raises():
    with pytest.raises(KeyError):
        contract.skill_entry("does_not_exist")


def test_contract_auto_derives_investigation_state_and_plan():
    import json
    rooted = json.loads(contract.SkillResult(
        skill="probe", root_cause="proven", confidence="medium"
    ).to_json())
    assert rooted["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert rooted["root_cause"] is None
    assert rooted["raw"]["candidate_cause"] == "proven"

    collecting = json.loads(contract.SkillResult(
        skill="probe",
        additional_logs_needed=[contract.LogRequest(
            log_kind="evtx", why="Need events", skill="event_log"
        )],
    ).to_json())
    assert collecting["evidence_state"] == "COLLECTION_PLAN_READY"
    assert collecting["evidence_gaps"][0]["code"] == "missing_log"
    assert collecting["collection_plan"]["requests"][0]["requires_admin"] is True


def test_powershell_contract_applies_default_redaction(project_root: Path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    module = project_root / "_shared" / "Contract.psm1"
    script = (
        f"Import-Module {str(module)!r} -Force; "
        "$f = New-SkillFinding -Summary 'located' -Evidence @{"
        "source_file='C:\\Users\\alice\\private\\u_ex.log';line_number=42};"
        "$r = New-SkillResult -Skill 'probe' -Findings @($f) -Raw @{"
        "source_file='C:\\Users\\alice\\private\\u_ex.log';"
        "username='alice@example.test';"
        "message='password=hunter2';"
        "url='https://alice:hunter2@example.test/path?q=secret';"
        "note='failed C:\\Users\\alice\\private\\trace.log';"
        "posix_note='failed /var/private/app.log';"
        "collection=@{command='wevtutil epl Application <bundle>\\Application.evtx'}"
        "}; Write-SkillResult $r"
    )
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    body = json.dumps(result)

    for secret in (
        "alice", "hunter2", "q=secret", r"C:\Users\alice", "/var/private/app.log"
    ):
        assert secret not in body
    assert result["raw"]["source_file"].endswith("u_ex.log")
    assert result["raw"]["url"] == "https://example.test/path"
    assert result["raw"]["privacy"]["default_redaction"] is True
    assert result["raw"]["collection"]["command"].startswith("wevtutil epl")
    assert result["raw"]["note"].endswith("/trace.log")
    assert result["raw"]["posix_note"].endswith("/app.log")
    finding = result["findings"][0]
    assert finding["evidence_id"].startswith("EV-")
    assert finding["provenance"]["quality"] == "located"
    assert finding["provenance"]["record"]["line_number"] == 42


def _run_powershell_contract(project_root: Path, body: str) -> dict:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    module = project_root / "_shared" / "Contract.psm1"
    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Import-Module {str(module)!r} -Force; {body}",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_powershell_child_boundary_hashes_malformed_output(project_root: Path):
    result = _run_powershell_contract(
        project_root,
        "$r=ConvertTo-SkillChildResult -Skill 'probe' "
        "-Stdout 'password=hunter2 C:\\private\\file.log' "
        "-Stderr 'secret stderr' -ExitCode 0 -Runtime 'python';"
        "Write-SkillResult $r",
    )
    body = json.dumps(result)

    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["raw"]["failure"]["code"] == "contract_validation_failed"
    assert len(result["raw"]["invalid_output"]["sha256"]) == 64
    assert "hunter2" not in body
    assert "secret stderr" not in body
    assert "raw_stdout" not in body


def test_powershell_child_boundary_normalizes_legacy_and_hashes_stderr(
    project_root: Path,
):
    encoded = base64.b64encode(json.dumps({
        "skill": "probe", "ok": True, "findings": [], "raw": {}
    }).encode("utf-8")).decode("ascii")
    result = _run_powershell_contract(
        project_root,
        f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$r=ConvertTo-SkillChildResult -Skill 'probe' -Stdout $json "
        "-Stderr 'private diagnostic' -ExitCode 0 -Runtime 'python';"
        "Write-SkillResult $r",
    )
    body = json.dumps(result)

    assert result["ok"] is True
    assert result["schema_version"] == "2.3"
    assert result["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert result["raw"]["contract_validation"]["valid"] is True
    assert len(result["raw"]["execution"]["stderr_sha256"]) == 64
    assert "private diagnostic" not in body


def test_powershell_child_boundary_rejects_exit_success_mismatch(project_root: Path):
    encoded = base64.b64encode(json.dumps({
        "skill": "probe", "ok": True, "findings": [], "raw": {}
    }).encode("utf-8")).decode("ascii")
    result = _run_powershell_contract(
        project_root,
        f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$r=ConvertTo-SkillChildResult -Skill 'probe' -Stdout $json "
        "-Stderr '' -ExitCode 2 -Runtime 'python';Write-SkillResult $r",
    )

    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["raw"]["failure"]["code"] == "child_exit_mismatch"


def test_powershell_child_boundary_rejects_malformed_finding(project_root: Path):
    encoded = base64.b64encode(json.dumps({
        "skill": "probe",
        "ok": True,
        "findings": [{"summary": "x", "severity": "fatal"}],
        "raw": {},
    }).encode("utf-8")).decode("ascii")
    result = _run_powershell_contract(
        project_root,
        f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$r=ConvertTo-SkillChildResult -Skill 'probe' -Stdout $json "
        "-Stderr '' -ExitCode 0 -Runtime 'python';Write-SkillResult $r",
    )

    assert result["ok"] is False
    issues = result["raw"]["contract_validation"]["issues"]
    assert any(issue["path"] == "findings[0].severity" for issue in issues)


def test_powershell_child_boundary_enforces_v24_required_fields(project_root: Path):
    encoded = base64.b64encode(json.dumps({
        "schema_version": "2.4", "skill": "probe", "ok": True
    }).encode("utf-8")).decode("ascii")
    result = _run_powershell_contract(
        project_root,
        f"$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$r=ConvertTo-SkillChildResult -Skill 'probe' -Stdout $json "
        "-Stderr '' -ExitCode 0 -Runtime 'python';Write-SkillResult $r",
    )

    assert result["ok"] is False
    paths = {issue["path"] for issue in result["raw"]["contract_validation"]["issues"]}
    assert {"findings", "confidence", "evidence_state", "raw"} <= paths


def test_powershell_direct_result_enforces_root_quality(project_root: Path):
    unproven = _run_powershell_contract(
        project_root,
        "$r=New-SkillResult -Skill 'probe' -RootCause 'unproven' "
        "-Confidence high -EvidenceState ROOT_CAUSE_FOUND -Raw @{};"
        "Write-SkillResult $r",
    )
    assert unproven["root_cause"] is None
    assert unproven["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert unproven["raw"]["candidate_cause"] == "unproven"

    proven = _run_powershell_contract(
        project_root,
        "$q=@{direct_join=$true;independent_sources=1;sample_sufficient=$true;"
        "capture_complete=$true;time_comparable=$true;blocking_gaps=$false};"
        "$r=New-SkillResult -Skill 'probe' -RootCause 'direct proof' "
        "-Confidence high -EvidenceState ROOT_CAUSE_FOUND -Raw @{quality=$q};"
        "Write-SkillResult $r",
    )
    assert proven["root_cause"] == "direct proof"
    assert proven["evidence_state"] == "ROOT_CAUSE_FOUND"
