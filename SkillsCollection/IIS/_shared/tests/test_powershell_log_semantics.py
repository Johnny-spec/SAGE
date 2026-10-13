"""PowerShell log parsers must preserve the same diagnostic semantics as Python."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_pwsh(script: Path, *args: str) -> dict:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_httperr_pwsh_maps_reason_and_queue_by_header(
    project_root: Path, fixtures_dir: Path
):
    script = project_root / "httperror" / "scripts" / "httperr_analyzer.ps1"
    result = _run_pwsh(script, "-Log", str(fixtures_dir / "httperr1.log"))

    reasons = {item[0] for item in result["raw"]["top_reasons"]}
    assert reasons == {"ConnLimit", "Timer_ConnectionIdle"}
    assert "Pool-A" not in reasons


def test_iis_pwsh_preserves_substatus_win32_and_provenance(
    project_root: Path, tmp_path: Path
):
    log = tmp_path / "u_ex.log"
    log.write_text(
        "#Software: Microsoft Internet Information Services 10.0\n"
        "#Version: 1.0\n"
        "#Fields: date time s-ip cs-method cs-uri-stem cs-uri-query s-port "
        "cs-username c-ip cs-host s-sitename sc-status sc-substatus "
        "sc-win32-status time-taken\n"
        "2026-08-11 10:00:00 10.0.0.5 GET /web.config - 443 - 192.0.2.10 "
        "app.contoso.test W3SVC1 500 19 5 12\n",
        encoding="utf-8",
    )
    script = project_root / "IIS_logs" / "scripts" / "iis_analyzer.ps1"
    result = _run_pwsh(script, "-LogPath", str(log))

    signature = result["raw"]["metrics"]["top_error_signatures"][0]
    assert signature["signature"] == "500.19/5"
    assert signature["samples"][0]["site_name"] == "W3SVC1"
    assert signature["samples"][0]["line_number"] == 4
    assert result["raw"]["timestamp_basis"] == "W3C UTC"


def test_iis_pwsh_recurses_and_excludes_other_log_kinds(
    project_root: Path, fixtures_dir: Path, tmp_path: Path
):
    iis_dir = tmp_path / "LogFiles" / "W3SVC1"
    httperr_dir = tmp_path / "LogFiles" / "HTTPERR"
    iis_dir.mkdir(parents=True)
    httperr_dir.mkdir(parents=True)
    shutil.copy(fixtures_dir / "sample_iis.log", iis_dir / "u_ex.log")
    shutil.copy(fixtures_dir / "httperr1.log", httperr_dir / "httperr.log")

    script = project_root / "IIS_logs" / "scripts" / "iis_analyzer.ps1"
    result = _run_pwsh(script, "-LogPath", str(tmp_path))

    assert result["raw"]["log_files_parsed"] == 1
    assert result["raw"]["metrics"]["total_requests"] == 122
    assert result["raw"]["source_files"][0].endswith("u_ex.log")