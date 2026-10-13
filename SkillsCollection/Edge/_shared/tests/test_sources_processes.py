from __future__ import annotations

import subprocess

import pytest

from _shared.sources import processes


def test_count_result_distinguishes_zero_from_tool_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(processes.os, "name", "nt")
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="0\n", stderr=""
        ),
    )
    empty = processes.count_msedge_result()
    assert empty.status == "success"
    assert empty.count == 0

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(processes.subprocess, "run", missing_tool)
    failed = processes.count_msedge_result()
    assert failed.status == "tool_unavailable"
    assert failed.count is None
    with pytest.raises(RuntimeError, match="PowerShell was not found"):
        processes.count_msedge()


def test_count_result_rejects_empty_and_invalid_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(processes.os, "name", "nt")
    outputs = iter(["", "not-a-number"])
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=next(outputs), stderr=""
        ),
    )

    assert processes.count_msedge_result().status == "parse_failed"
    assert processes.count_msedge_result().status == "parse_failed"


def test_process_query_preserves_acquisition_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(processes.os, "name", "nt")

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(processes.subprocess, "run", missing_tool)
    result = processes.query(["msedge"])

    assert result["status"] == "tool_unavailable"
    assert result["entries"] == []
    assert result["quality"]["capture_complete"] is False
    assert result["acquisitions"][0]["status"] == "tool_unavailable"


def test_process_query_distinguishes_verified_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(processes.os, "name", "nt")
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="[]", stderr=""
        ),
    )

    result = processes.query(["msedge"])

    assert result["status"] == "success"
    assert result["summary"]["count"] == 0
    assert result["quality"]["capture_complete"] is True


def test_legacy_process_iterator_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(processes.os, "name", "nt")

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(processes.subprocess, "run", missing_tool)

    with pytest.raises(RuntimeError, match="PowerShell was not found"):
        list(processes.iter_entries("msedge"))