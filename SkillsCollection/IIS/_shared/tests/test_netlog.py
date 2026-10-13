"""IIS netlog skill smoke + log_discovery classification tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from log_discovery import classify_file, NETLOG_KIND, UNKNOWN_KIND  # type: ignore


SYNTH_NETLOG = {
    "constants": {
        "logSourceType": {
            "URL_REQUEST": 1, "SOCKET": 2, "HOST_RESOLVER_IMPL_JOB": 3,
        },
        "logEventTypes": {
            "URL_REQUEST_START_JOB": 100, "REQUEST_ALIVE": 102,
            "SSL_HANDSHAKE_ERROR": 201,
            "HOST_RESOLVER_IMPL_JOB": 300,
        },
        "logEventPhase": {"PHASE_BEGIN": 1, "PHASE_END": 2, "PHASE_NONE": 0},
    },
    "events": [
        {"time": "100", "phase": 1, "source": {"id": 1, "type": 1},
         "type": 100, "params": {"url": "https://x.example/"}},
        {"time": "110", "phase": 0, "source": {"id": 1, "type": 1},
         "type": 201, "params": {"net_error": -202}},
    ],
}


@pytest.fixture()
def synth_netlog(tmp_path: Path) -> Path:
    p = tmp_path / "synth-netlog.json"
    p.write_text(json.dumps(SYNTH_NETLOG), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# log_discovery classification
# ---------------------------------------------------------------------------

def test_log_discovery_classifies_netlog(synth_netlog: Path):
    assert classify_file(synth_netlog) == NETLOG_KIND


def test_log_discovery_skips_random_json(tmp_path: Path):
    p = tmp_path / "random.json"
    p.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert classify_file(p) == UNKNOWN_KIND


# ---------------------------------------------------------------------------
# Skill smoke
# ---------------------------------------------------------------------------

def _run(project_root: Path, ctx: dict) -> dict:
    script = project_root / "netlog" / "scripts" / "netlog_analyzer.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(script), json.dumps(ctx)],
        capture_output=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.returncode == 0, f"netlog skill crashed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_iis_netlog_no_input(project_root: Path):
    result = _run(project_root, {})
    assert result["ok"] is True
    assert result["confidence"] == "low"
    assert result["additional_logs_needed"], "should ask for the missing log"
    assert result["additional_logs_needed"][0]["log_kind"] == "netlog"


def test_iis_netlog_with_synthetic(project_root: Path, synth_netlog: Path):
    result = _run(project_root, {"extra": {"netlog_paths": [str(synth_netlog)]}})
    assert result["ok"] is True
    raw = result.get("raw") or {}
    # Either observed_problem_types or per_file should have something.
    assert raw.get("per_file"), "expected per_file analysis output"


# ---------------------------------------------------------------------------
# HTTP-auth analysis: SPN mismatch + handshake loop
# ---------------------------------------------------------------------------

def _build_auth_netlog(spn: str, origin: str,
                       sec_status: int = -2146893022,  # SEC_E_WRONG_PRINCIPAL
                       net_error: int = -338,
                       missing_creds: int = 0) -> dict:
    """Synthesise a Chromium net-export with a Kerberos SPN failure."""
    blob = {
        "constants": {
            "logSourceType": {"URL_REQUEST": 1, "HTTP_AUTH_CONTROLLER": 39},
            "logEventTypes": {
                "URL_REQUEST_START_JOB": 100,
                "AUTH_LIBRARY_INIT_SEC_CTX": 200,
                "AUTH_HANDLER_CREATE_RESULT": 201,
                "AUTH_GENERATE_TOKEN": 202,
                "HTTP_TRANSACTION_DRAIN_BODY_FOR_AUTH_RESTART": 203,
            },
            "logEventPhase": {"PHASE_NONE": 0, "PHASE_BEGIN": 1, "PHASE_END": 2},
        },
        "events": [
            {"time": "1000", "phase": 1, "source": {"id": 1, "type": 1},
             "type": 100, "params": {"url": origin + "/page"}},
            # Successful handler creation
            {"time": "1010", "phase": 0, "source": {"id": 10, "type": 39},
             "type": 201, "params": {"scheme": "negotiate", "challenge": "Negotiate",
                                       "origin": origin,
                                       "allows_default_credentials": True}},
            # Kerberos init BEGIN — carries SPN
            {"time": "1020", "phase": 1, "source": {"id": 10, "type": 39},
             "type": 200, "params": {"spn": spn,
                                       "flags": {"value": "0x0", "delegated": False,
                                                 "mutual": False}}},
            # Kerberos init END — carries the failure status
            {"time": "1030", "phase": 2, "source": {"id": 10, "type": 39},
             "type": 200, "params": {"status": {"net_error": net_error,
                                                  "security_status": sec_status}}},
            # AUTH_GENERATE_TOKEN failure
            {"time": "1040", "phase": 0, "source": {"id": 10, "type": 39},
             "type": 202, "params": {"net_error": net_error}},
        ],
    }
    for i in range(missing_creds):
        blob["events"].append(
            {"time": str(1100 + i), "phase": 0,
             "source": {"id": 10, "type": 39},
             "type": 201, "params": {"scheme": "negotiate", "origin": origin,
                                       "challenge": "Negotiate",
                                       "net_error": -341}}  # ERR_MISSING_AUTH_CREDENTIALS
        )
    return blob


def test_netlog_detects_kerberos_spn_mismatch(project_root: Path, tmp_path: Path):
    p = tmp_path / "spn-mismatch.json"
    blob = _build_auth_netlog(spn="HTTP/backend.corp.example",
                              origin="https://public.example")
    p.write_text(json.dumps(blob), encoding="utf-8")
    result = _run(project_root, {
        "problem_type": "kerberos_spn_mismatch",
        "question": "Windows authentication fails with a Kerberos SPN error",
        "extra": {"netlog_paths": [str(p)]},
    })
    assert result["ok"] is True
    assert "kerberos_spn_mismatch" in (result["raw"] or {}).get(
        "observed_problem_types", []), result
    # Without an exact cross-source join this remains a candidate conclusion.
    assert result["root_cause"] is None
    assert "SEC_E_WRONG_PRINCIPAL" in result["raw"]["candidate_cause"]
    # Evidence on the dedicated finding must carry the SPN string and the
    # host mismatch we synthesised (public.example vs backend.corp.example).
    spn_finding = next(
        f for f in result["findings"]
        if "SEC_E_WRONG_PRINCIPAL" in f.get("summary", "")
    )
    ev = spn_finding["evidence"]
    assert ev["failed_spn"] == "HTTP/backend.corp.example"
    assert ev["url_host"] == "public.example"
    assert ev["spn_host"] == "backend.corp.example"
    assert ev["host_mismatch"] is True
    # Playbook should have injected the Kerberos solutions.
    sol_titles = " | ".join(s["title"] for s in result.get("solutions") or [])
    assert "setspn" in sol_titles.lower() or "spn" in sol_titles.lower()


def test_netlog_detects_auth_downgrade(project_root: Path, tmp_path: Path):
    p = tmp_path / "auth-downgrade.json"
    blob = _build_auth_netlog(
        spn="HTTP/backend.corp.example",
        origin="http://public.example",
        sec_status=-2146892976,  # SEC_E_DOWNGRADE_DETECTED (0x80090350)
        net_error=-344,  # ERR_UNDOCUMENTED_SECURITY_LIBRARY_STATUS
    )
    p.write_text(json.dumps(blob), encoding="utf-8")

    result = _run(project_root, {
        "problem_type": "auth_error",
        "question": "Windows authentication fails after negotiation downgrade",
        "extra": {"netlog_paths": [str(p)]},
    })

    assert result["ok"] is True
    per_file = result["raw"]["per_file"][0]
    assert per_file["auth"]["sspi_status_counts"] == {
        "SEC_E_DOWNGRADE_DETECTED": 1,
    }
    assert per_file["auth"]["net_error_counts"] == {
        "ERR_UNDOCUMENTED_SECURITY_LIBRARY_STATUS": 2,
    }
    finding = next(
        item for item in result["findings"]
        if "SEC_E_DOWNGRADE_DETECTED" in item.get("summary", "")
    )
    assert finding["severity"] == "warning"
    assert finding["evidence"]["failed_spn"] == "HTTP/backend.corp.example"
    assert result["root_cause"] is None
    assert result["raw"]["candidate_cause"] == "Windows-auth failure in net-export"
    assert "no SSPI failure recorded" not in json.dumps(result)


def test_netlog_detects_handshake_loop(project_root: Path, tmp_path: Path):
    p = tmp_path / "handshake-loop.json"
    # No SEC_E_WRONG_PRINCIPAL — just repeated ERR_MISSING_AUTH_CREDENTIALS.
    blob = _build_auth_netlog(spn="HTTP/host.example",
                              origin="https://host.example",
                              sec_status=0, net_error=0,
                              missing_creds=4)
    # Remove the wrong-principal failure END frame so only the loop pattern
    # remains.
    blob["events"] = [e for e in blob["events"]
                      if not (e["type"] == 200 and e["phase"] == 2)]
    for offset in range(3):
        blob["events"].append(
            {"time": str(1200 + offset), "phase": 0,
             "source": {"id": 1, "type": 1}, "type": 203,
             "params": {"http_status_code": 401}}
        )
    blob["events"].append(
        {"time": "1210", "phase": 2,
         "source": {"id": 1, "type": 1}, "type": 100,
            "params": {"http_status_code": 401, "net_error": -341}}
    )
    p.write_text(json.dumps(blob), encoding="utf-8")
    result = _run(project_root, {"extra": {"netlog_paths": [str(p)]}})
    assert result["ok"] is True
    problems = (result["raw"] or {}).get("observed_problem_types") or []
    assert "auth_handshake_loop" in problems
    assert "kerberos_spn_mismatch" not in problems


def test_missing_credentials_without_restarts_is_not_a_loop(
    project_root: Path, tmp_path: Path
):
    p = tmp_path / "missing-creds-only.json"
    blob = _build_auth_netlog(
        spn="HTTP/host.example",
        origin="https://host.example",
        sec_status=0,
        net_error=0,
        missing_creds=4,
    )
    blob["events"] = [e for e in blob["events"]
                      if not (e["type"] == 200 and e["phase"] == 2)]
    p.write_text(json.dumps(blob), encoding="utf-8")

    result = _run(project_root, {"extra": {"netlog_paths": [str(p)]}})
    problems = (result["raw"] or {}).get("observed_problem_types") or []
    assert "auth_error" in problems
    assert "auth_handshake_loop" not in problems


def test_netlog_applies_calibrated_time_filter_and_redacts_url(
    project_root: Path, tmp_path: Path
):
    anchor = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    anchor_ms = int(anchor.timestamp() * 1000)
    blob = {
        "constants": {
            "logFormatVersion": 1,
            "timeTickOffset": str(anchor_ms - 1000),
            "logSourceType": {"URL_REQUEST": 1},
            "logEventTypes": {"URL_REQUEST_START_JOB": 100},
            "logEventPhase": {"PHASE_NONE": 0, "PHASE_BEGIN": 1, "PHASE_END": 2},
        },
        "events": [
            {"time": "1000.25", "phase": 1, "source": {"id": 1, "type": 1},
             "type": 100,
             "params": {
                 "url": "https://user:secret@app.example/private?token=secret#fragment",
                 "method": "GET",
             }},
            {"time": "1010.75", "phase": 2, "source": {"id": 1, "type": 1},
             "type": 100, "params": {"net_error": -105}},
            {"time": "5000", "phase": 1, "source": {"id": 2, "type": 1},
             "type": 100, "params": {"url": "https://outside.example/?key=value"}},
        ],
    }
    path = tmp_path / "timed.json"
    path.write_text(json.dumps(blob), encoding="utf-8")
    result = _run(project_root, {
        "time_range": {
            "start": "2026-08-11T09:59:59Z",
            "end": "2026-08-11T10:00:01Z",
        },
        "extra": {"netlog_paths": [str(path)]},
    })

    per_file = result["raw"]["per_file"][0]
    assert per_file["time_model"]["calibrated"] is True
    assert per_file["time_filter"]["applied"] is True
    assert per_file["capture_integrity"]["events_filtered_out"] == 1
    sample = per_file["sample_failures"][0]
    assert sample["url"] == "https://app.example/private"
    assert sample["duration_ms"] == pytest.approx(10.5)
    assert "secret" not in json.dumps(result)


def test_netlog_requested_window_without_offset_is_explicitly_degraded(
    project_root: Path, synth_netlog: Path
):
    result = _run(project_root, {
        "time_range": {
            "start": "2026-08-11T09:59:59Z",
            "end": "2026-08-11T10:00:01Z",
        },
        "extra": {"netlog_paths": [str(synth_netlog)]},
    })
    per_file = result["raw"]["per_file"][0]
    assert per_file["time_model"]["quality"] == "relative_only"
    assert per_file["time_filter"]["applied"] is False
    assert result["confidence"] != "high"
    assert any("absolute NetLog time is unavailable" in f["summary"]
               for f in result["findings"])


def test_auth_begin_end_pairing_uses_fifo(project_root: Path, tmp_path: Path):
    blob = _build_auth_netlog(
        spn="HTTP/first.example",
        origin="https://first.example",
    )
    second_begin = {
        "time": "1021", "phase": 1, "source": {"id": 10, "type": 39},
        "type": 200, "params": {"spn": "HTTP/second.example"},
    }
    second_end = {
        "time": "1031", "phase": 2, "source": {"id": 10, "type": 39},
        "type": 200,
        "params": {"status": {"net_error": -338,
                                "security_status": -2146893022}},
    }
    blob["events"].insert(3, second_begin)
    blob["events"].insert(5, second_end)
    path = tmp_path / "fifo-auth.json"
    path.write_text(json.dumps(blob), encoding="utf-8")

    result = _run(project_root, {"extra": {"netlog_paths": [str(path)]}})
    auth = result["raw"]["per_file"][0]["auth"]
    assert [attempt["spn"] for attempt in auth["spn_attempts"]] == [
        "HTTP/first.example",
        "HTTP/second.example",
    ]
    assert auth["pairing"] == {
        "unmatched_begin": 0,
        "unmatched_end": 0,
        "paired_attempts": 2,
    }


# ---------------------------------------------------------------------------
# log_discovery: realistic Edge net-export prepends ~80KB of trial flags
# before logSourceType. The sniffer must still classify it.
# ---------------------------------------------------------------------------

def test_log_discovery_handles_large_constants_prefix(tmp_path: Path):
    huge_prefix = ["trial:value:" + str(i) for i in range(5000)]
    blob = {
        "constants": {
            "activeFieldTrialGroups": huge_prefix,
            "logSourceType": {"URL_REQUEST": 1},
            "logEventTypes": {"URL_REQUEST_START_JOB": 100},
        },
        "events": [],
    }
    p = tmp_path / "edge-netlog.json"
    p.write_text(json.dumps(blob), encoding="utf-8")
    # Must NOT be classified as 'unknown'.
    assert classify_file(p) == NETLOG_KIND
