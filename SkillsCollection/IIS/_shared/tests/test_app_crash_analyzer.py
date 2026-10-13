from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module(project_root: Path):
    path = project_root / "app_crash" / "scripts" / "app_crash_analyzer.py"
    spec = importlib.util.spec_from_file_location("app_crash_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _context() -> str:
    return json.dumps({
        "time_range": {
            "start": "2026-01-01T00:00:00",
            "end": "2026-01-01T01:00:00",
        }
    })


def test_app_crash_does_not_turn_query_failure_into_no_events(
    project_root: Path, monkeypatch, capsys
):
    module = _load_module(project_root)
    monkeypatch.setattr(module, "query_events_result", lambda *args, **kwargs: {
        "status": "tool_unavailable",
        "entries": [],
        "acquisitions": [{"source": "Application", "status": "tool_unavailable"}],
        "quality": {"capture_complete": False, "records_seen": 0},
    })

    exit_code = module.main([_context()])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "tool_unavailable"
    assert "No crash events" not in json.dumps(result)


def test_app_crash_verified_empty_is_not_a_tool_failure(
    project_root: Path, monkeypatch, capsys
):
    module = _load_module(project_root)
    monkeypatch.setattr(module, "query_events_result", lambda *args, **kwargs: {
        "status": "success",
        "entries": [],
        "acquisitions": [{"source": "Application", "status": "success"}],
        "quality": {"capture_complete": True, "records_seen": 0, "truncated": False},
    })

    assert module.main([_context()]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is True
    assert any("No crash events" in finding["summary"] for finding in result["findings"])


def test_app_crash_classification_is_hypothesis_and_message_is_hashed(
    project_root: Path, monkeypatch, capsys
):
    module = _load_module(project_root)
    secret_message = "System.OutOfMemoryException at Private.App.Run() C:\\secret\\app.dll"
    monkeypatch.setattr(module, "query_events_result", lambda *args, **kwargs: {
        "status": "success",
        "entries": [{
            "TimeCreated": "2026-01-01T00:30:00",
            "ProviderName": ".NET Runtime",
            "Id": 1026,
            "Message": secret_message,
        }],
        "acquisitions": [{"source": "Application", "status": "success"}],
        "quality": {"capture_complete": True, "records_seen": 1, "truncated": False},
    })

    assert module.main([_context()]) == 0
    result = json.loads(capsys.readouterr().out)
    body = json.dumps(result)

    assert result["root_cause"] is None
    assert result["evidence_state"] == "COLLECTION_PLAN_READY"
    assert result["collection_plan"]["requests"]
    assert result["raw"]["candidate_cause"] == "Worker process exhausted available memory"
    assert "message_sha256" in result["raw"]["samples"][0]
    assert secret_message not in body