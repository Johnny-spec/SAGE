from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVENT_LOG_SCRIPTS = ROOT / "event_log" / "scripts"
if str(EVENT_LOG_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EVENT_LOG_SCRIPTS))

import event_log_analyzer  # noqa: E402
from _shared.logs import evtx  # noqa: E402


def test_classify_aspnet_different_grant_set_signature():
    ev = {
        "Id": 1309,
        "Message": "",
        "Properties": (
            "FileLoadException || Loading this assembly would produce a different "
            "grant set from other instances. (Exception from HRESULT: 0x80131401)"
        ),
    }

    sig = event_log_analyzer.classify_event(ev)

    assert sig is not None
    assert sig["type"] == "aspnet_different_grant_set"
    assert "UNC" in sig["root_cause"]
    assert sig["priority"] < 30


def test_infer_root_cause_prioritizes_grant_set_over_backend_500():
    correlated = [
        {
            "signature": {
                "type": "aspnet_backend_500",
                "priority": 30,
                "root_cause": "backend symptom",
            }
        },
        {
            "signature": {
                "type": "aspnet_different_grant_set",
                "priority": 10,
                "root_cause": "grant set root cause",
            }
        },
    ]

    root, confidence = event_log_analyzer.infer_root_cause(None, correlated)

    assert root == "grant set root cause"
    assert confidence == "high"


def test_offline_evtx_query_uses_filterhashtable_not_pipeline(monkeypatch, tmp_path):
    commands: list[str] = []

    def fake_run(args, **kwargs):
        commands.append(args[-1])

        class Result:
            returncode = 0
            stdout = "[]"
            stderr = ""

        return Result()

    monkeypatch.setattr(event_log_analyzer.subprocess, "run", fake_run)

    evtx_path = tmp_path / "Application's.evtx"
    evtx_path.write_bytes(evtx.EVTX_MAGIC + b"fixture")
    event_log_analyzer._query_winevent_from_path(
        evtx_path,
        datetime(2026, 6, 5, 9, 30),
        datetime(2026, 6, 5, 11, 30),
    )

    assert commands
    assert all("-FilterHashtable" in cmd for cmd in commands)
    assert all("Where-Object" not in cmd for cmd in commands)
    assert any("Level=1,2,3" in cmd for cmd in commands)
    assert any("Id=" in cmd for cmd in commands)


def test_shared_evtx_query_file_uses_provider_side_filter(monkeypatch, tmp_path):
    commands: list[str] = []

    def fake_run(args, **kwargs):
        commands.append(args[-1])

        class Result:
            returncode = 0
            stdout = json.dumps([])
            stderr = ""

        return Result()

    monkeypatch.setattr(evtx.subprocess, "run", fake_run)

    evtx_path = tmp_path / "Application's.evtx"
    evtx_path.write_bytes(evtx.EVTX_MAGIC + b"fixture")
    evtx.query_file(
        evtx_path,
        datetime(2026, 6, 5, 9, 30),
        datetime(2026, 6, 5, 11, 30),
    )

    assert len(commands) == 1
    assert "-FilterHashtable" in commands[0]
    assert "Where-Object" not in commands[0]
    assert "Application''s.evtx" in commands[0]


def test_shared_evtx_distinguishes_empty_from_tool_failure(monkeypatch):
    def empty_run(args, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr(evtx.subprocess, "run", empty_run)
    empty = evtx.query_live_result(
        "Application", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert empty.status == "success"
    assert empty.entries == []

    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(evtx.subprocess, "run", missing_tool)
    failed = evtx.query_live_result(
        "Application", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert failed.status == "tool_unavailable"
    assert failed.entries == []

    def denied(*args, **kwargs):
        class Result:
            returncode = 3
            stdout = ""
            stderr = "Access is denied"
        return Result()

    monkeypatch.setattr(evtx.subprocess, "run", denied)
    blocked = evtx.query_live_result(
        "Security", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert blocked.status == "permission_blocked"
    assert blocked.error == "Event Log access was denied."


def test_event_log_main_reports_verified_empty(monkeypatch, capsys):
    monkeypatch.setattr(
        event_log_analyzer._COMMON,
        "_query_winevent_result",
        lambda *args, **kwargs: evtx.EvtxReadResult(status="success", entries=[]),
    )

    exit_code = event_log_analyzer.main([
        json.dumps({
            "time_range": {
                "start": "2026-01-01T00:00:00",
                "end": "2026-01-01T01:00:00",
            }
        }),
        "--logs", "Application",
    ])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["raw"]["quality"]["capture_complete"] is True
    assert any("No correlated events" in item["summary"] for item in result["findings"])


def test_event_log_signature_remains_candidate_without_exact_join(monkeypatch, capsys):
    monkeypatch.setattr(
        event_log_analyzer._COMMON,
        "_query_winevent_result",
        lambda *args, **kwargs: evtx.EvtxReadResult(status="success", entries=[{
            "TimeCreated": "2026-01-01T00:30:00",
            "Id": 7031,
            "ProviderName": "Service Control Manager",
            "Message": "service terminated",
        }]),
    )

    assert event_log_analyzer.main([
        json.dumps({
            "problem_type": "5xx_error",
            "time_range": {
                "start": "2026-01-01T00:00:00",
                "end": "2026-01-01T01:00:00",
            },
        }),
        "--logs", "System",
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert result["raw"]["candidate_cause"]
    assert result["raw"]["quality"]["direct_join"] is False


def test_event_log_main_does_not_turn_tool_failure_into_no_events(monkeypatch, capsys):
    monkeypatch.setattr(
        event_log_analyzer._COMMON,
        "_query_winevent_result",
        lambda *args, **kwargs: evtx.EvtxReadResult(
            status="tool_unavailable", error="PowerShell was not found."
        ),
    )

    exit_code = event_log_analyzer.main([
        json.dumps({
            "time_range": {
                "start": "2026-01-01T00:00:00",
                "end": "2026-01-01T01:00:00",
            }
        }),
        "--logs", "Application",
    ])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "tool_unavailable"
    assert result["raw"]["quality"]["capture_complete"] is False
    body = json.dumps(result)
    assert "No correlated events" not in body
    assert "absence of events is not established" in body


def test_invalid_evtx_header_is_not_verified_empty(tmp_path: Path):
    path = tmp_path / "fake.evtx"
    path.write_bytes(b"not-an-evtx")

    result = evtx.query_file_result(
        path, datetime(2026, 1, 1), datetime(2026, 1, 2)
    )

    assert result.status == "unsupported_schema"
    assert result.entries == []
