"""Edge netlog skill smoke tests using a synthetic net-export JSON."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _shared.sources import netlog


# Minimum shape that satisfies the netlog reader. We only need a handful of
# events of the right types; the reader keys off constants.logSourceType.
SYNTH_NETLOG = {
    "constants": {
        "logSourceType": {
            "URL_REQUEST": 1, "SOCKET": 2, "HOST_RESOLVER_IMPL_JOB": 3,
        },
        "logEventTypes": {
            "URL_REQUEST_START_JOB": 100,
            "URL_REQUEST_REDIRECT_JOB": 101,
            "REQUEST_ALIVE": 102,
            "SSL_CERTIFICATES_RECEIVED": 200,
            "SSL_HANDSHAKE_ERROR": 201,
            "HOST_RESOLVER_IMPL_JOB": 300,
            "PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST": 400,
        },
        "logEventPhase": {"PHASE_BEGIN": 1, "PHASE_END": 2, "PHASE_NONE": 0},
    },
    "events": [
        # A request that fails with a cert error
        {"time": "100", "phase": 1, "source": {"id": 1, "type": 1},
         "type": 100, "params": {"url": "https://bad.example/"}},
        {"time": "110", "phase": 0, "source": {"id": 1, "type": 1},
         "type": 201, "params": {"net_error": -202,
                                  "error_description": "CERT_AUTHORITY_INVALID"}},
        # A DNS failure
        {"time": "200", "phase": 1, "source": {"id": 2, "type": 3},
         "type": 300, "params": {"host": "nope.invalid"}},
        {"time": "210", "phase": 2, "source": {"id": 2, "type": 3},
         "type": 300, "params": {"net_error": -105}},  # ERR_NAME_NOT_RESOLVED
        # A slow request
        {"time": "300", "phase": 1, "source": {"id": 3, "type": 1},
         "type": 100, "params": {"url": "https://slow.example/"}},
        {"time": "15000", "phase": 2, "source": {"id": 3, "type": 1},
         "type": 102, "params": {}},
    ],
}


@pytest.fixture()
def synth_netlog_path(tmp_path: Path) -> Path:
    p = tmp_path / "synth-netlog.json"
    p.write_text(json.dumps(SYNTH_NETLOG), encoding="utf-8")
    return p


def _run(project_root: Path, ctx: dict) -> dict:
    script = project_root / "edge_netlog" / "scripts" / "edge_netlog.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(script), json.dumps(ctx)],
        capture_output=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.returncode == 0, f"edge_netlog crashed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_edge_netlog_no_input(project_root: Path):
    """Without any netlog files the skill returns a low-confidence info envelope
    plus an additional_logs_needed entry."""
    result = _run(project_root, {})
    assert result["ok"] is True
    assert result["confidence"] == "low"
    assert result["additional_logs_needed"], "should ask for the missing log"
    assert result["additional_logs_needed"][0]["log_kind"] == "netlog"


def test_edge_netlog_with_synthetic(project_root: Path, synth_netlog_path: Path):
    result = _run(project_root, {"extra": {"netlog_paths": [str(synth_netlog_path)]}})
    assert result["ok"] is True
    raw = result.get("raw") or {}
    assert raw.get("observed_problem_types"), "should classify at least one problem"
    # Findings should include something about the data we put in.
    summaries = " ".join(f.get("summary", "") for f in result.get("findings", []))
    # At least one of cert / dns / slow should be flagged.
    assert any(tok in summaries.lower()
               for tok in ("cert", "dns", "slow", "proxy", "failure"))
    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert raw["supportive_problem_types"] == []
    assert raw["candidate_cause"] is None
    assert raw["quality"]["capture_complete"] is False
    assert raw["quality"]["time_comparable"] is False


def test_edge_netlog_decodes_integer_errors_and_auth_downgrade(
    project_root: Path, tmp_path: Path,
):
    capture = {
        "constants": {
            "logSourceType": {"URL_REQUEST": 1, "HTTP_AUTH_CONTROLLER": 39},
            "logEventTypes": {
                "URL_REQUEST_START_JOB": 100,
                "AUTH_HANDLER_CREATE_RESULT": 200,
                "AUTH_LIBRARY_INIT_SEC_CTX": 201,
            },
            "logEventPhase": {"PHASE_NONE": 0, "PHASE_BEGIN": 1, "PHASE_END": 2},
        },
        "events": [
            {"time": "100", "phase": 1, "source": {"id": 1, "type": 1},
             "type": 100, "params": {"url": "http://pws.example/auth?token=secret"}},
            {"time": "110", "phase": 2, "source": {"id": 1, "type": 1},
             "type": 100, "params": {"net_error": -7}},
            {"time": "120", "phase": 0, "source": {"id": 10, "type": 39},
             "type": 200, "params": {"scheme": "negotiate", "challenge": "Negotiate",
                                      "origin": "http://pws.example"}},
            {"time": "130", "phase": 1, "source": {"id": 10, "type": 39},
             "type": 201, "params": {"spn": "HTTP/pws.ad.example"}},
            {"time": "140", "phase": 2, "source": {"id": 10, "type": 39},
             "type": 201, "params": {"status": {
                 "net_error": -344,
                 "security_status": -2146892976,
             }}},
        ],
    }
    path = tmp_path / "auth-downgrade.json"
    path.write_text(json.dumps(capture), encoding="utf-8")

    result = _run(project_root, {
        "problem_type": "auth_error",
        "question": "Browser repeatedly prompts for credentials and authentication fails",
        "extra": {"netlog_paths": [str(path)]},
    })

    assert result["ok"] is True
    assert result["raw"]["per_file"][0]["requests_failed"] == 1
    assert result["raw"]["per_file"][0]["error_breakdown"] == {"ERR_TIMED_OUT": 1}
    auth = result["raw"]["per_file"][0]["auth"]
    assert auth["schemes_seen"] == {"negotiate": 1}
    assert auth["sspi_status_counts"] == {"SEC_E_DOWNGRADE_DETECTED": 1}
    assert auth["net_error_counts"] == {
        "ERR_UNDOCUMENTED_SECURITY_LIBRARY_STATUS": 1,
    }
    finding = next(
        item for item in result["findings"]
        if "SEC_E_DOWNGRADE_DETECTED" in item.get("summary", "")
    )
    assert finding["evidence"]["failed_spn"] == "HTTP/pws.ad.example"
    assert finding["evidence"]["source_file"] == "auth-downgrade.json"
    assert finding["evidence"]["source_id"] == 10
    assert "SEC_E_DOWNGRADE_DETECTED" in result["raw"]["candidate_cause"]
    assert "HTTP/pws.ad.example" in result["raw"]["candidate_cause"]
    assert "auth_error" in result["raw"]["observed_problem_types"]


def test_edge_netlog_uses_symptom_scope_to_demote_background_noise(
    project_root: Path, synth_netlog_path: Path,
):
    result = _run(project_root, {
        "problem_type": "page_slow",
        "question": "Edge page loads slowly",
        "extra": {"netlog_paths": [str(synth_netlog_path)]},
    })
    raw = result["raw"]

    assert raw["relevant_problem_types"] == ["page_slow", "dns_issue"]
    assert raw["supportive_problem_types"] == ["page_slow"]
    assert raw["background_problem_types"] == ["cert_error"]
    assert raw["candidate_cause"] == "Slow URL requests in net-export"
    slow = next(
        item for item in result["findings"]
        if item.get("evidence", {}).get("observed_problem_type") == "page_slow"
    )
    assert slow["evidence"]["signal_classification"] == "supportive"


def test_edge_netlog_does_not_make_network_candidate_for_crash_symptom(
    project_root: Path, synth_netlog_path: Path,
):
    result = _run(project_root, {
        "problem_type": "crash",
        "question": "Edge crashes on startup",
        "extra": {"netlog_paths": [str(synth_netlog_path)]},
    })

    assert result["raw"]["relevant_problem_types"] == []
    assert result["raw"]["candidate_cause"] is None
    assert result["confidence"] == "low"


def test_netlog_reader_rejects_input_over_budget(tmp_path: Path):
    path = tmp_path / "oversized.json"
    path.write_text(json.dumps(SYNTH_NETLOG), encoding="utf-8")

    result = netlog.read_file(path, max_input_bytes=10)

    assert result.status == "input_too_large"
    assert result.entries == []
    assert result.quality["bytes_read"] == 0
    assert result.quality["truncated"] is True


def test_netlog_reader_builds_closed_dependency_graph_and_time_model(tmp_path: Path):
    capture = {
        "constants": {
            "timeTickOffset": 1_700_000_000_000,
            "logSourceType": {"URL_REQUEST": 1, "SOCKET": 2},
            "logEventTypes": {"REQUEST": 10, "SOCKET_ALIVE": 20},
            "logEventPhase": {"PHASE_NONE": 0, "PHASE_BEGIN": 1, "PHASE_END": 2},
        },
        "events": [
            {
                "time": "100",
                "phase": 0,
                "source": {"id": 2, "type": 2},
                "type": 20,
                "params": {},
            },
            {
                "time": "110",
                "phase": 0,
                "source": {"id": 1, "type": 1},
                "type": 10,
                "params": {"source_dependency": {"id": 2, "type": 2}},
            },
        ],
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(capture), encoding="utf-8")

    result = netlog.read_file(path)

    assert result.status == "success"
    assert result.quality["capture_complete"] is True
    assert result.time_model["wall_clock_available"] is True
    assert result.dependency_graph["closed"] is True
    assert result.dependency_graph["edge_count"] == 1


def test_edge_netlog_over_budget_is_failure(project_root: Path, synth_netlog_path: Path):
    script = project_root / "edge_netlog" / "scripts" / "edge_netlog.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            json.dumps({
                "extra": {
                    "netlog_paths": [str(synth_netlog_path)],
                    "max_input_bytes": 10,
                }
            }),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    result = json.loads(proc.stdout)

    assert proc.returncode == 1
    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert result["raw"]["parse_errors"][0]["status"] == "input_too_large"
