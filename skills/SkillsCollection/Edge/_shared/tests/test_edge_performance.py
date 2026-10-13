from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_module(project_root: Path):
    path = project_root / "edge_performance" / "scripts" / "edge_performance.py"
    spec = importlib.util.spec_from_file_location("edge_performance_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sample_distinguishes_no_process_from_tool_failure(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module(project_root)
    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="[]", stderr=""
        ),
    )
    empty = module._sample(2)
    assert empty.status == "success"
    assert empty.rows == []

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(module.subprocess, "run", missing_tool)
    failed = module._sample(2)
    assert failed.status == "tool_unavailable"
    assert failed.error == "PowerShell was not found."


def test_main_reports_sampling_failure_as_tool_unavailable(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    module = _load_module(project_root)
    monkeypatch.setattr(
        module,
        "_sample",
        lambda *_: module.SampleResult(status="timeout", error="timed out"),
    )

    exit_code = module.main([])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "timeout"
    assert result["raw"]["quality"]["sample_status"] == "timeout"
    assert "No msedge.exe" not in json.dumps(result)


def test_main_redacts_process_command_line(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    module = _load_module(project_root)
    monkeypatch.setattr(
        module,
        "_sample",
        lambda *_: module.SampleResult(status="success", rows=[{
            "Pid": 42,
            "CpuPct": 12.0,
            "WorkingSetMB": 256.0,
            "CommandLine": "msedge.exe --profile-directory=Secret",
        }]),
    )

    assert module.main([]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is True
    assert "CommandLine" not in result["raw"]["top_processes"][0]
    assert "Secret" not in json.dumps(result)