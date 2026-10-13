from __future__ import annotations

import sys
import time
import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path


PROCMON_ROOT = (
    Path(__file__).resolve().parents[1] / "log_skills" / "skills" / "procmon"
)
LOG_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "log_skills"
for module_root in (PROCMON_ROOT, LOG_SKILLS_ROOT):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

import procmon_analysis  # noqa: E402
import procmon_io  # noqa: E402
import symptom_relevance  # noqa: E402


HEADER = (
    "Time of Day,Process Name,Parent PID,PID,TID,Operation,Result,Path,"
    "Detail,User,Command Line,Session\n"
)
ROW = (
    "5:08:40.3306159 PM,app.exe,100,200,300,CreateFile,ACCESS DENIED,"
    "C:\\secret\\config.json,Desired Access: Generic Read,DOMAIN\\user,"
    "C:\\app\\app.exe --token secret,1\n"
)


def test_procmon_csv_is_structurally_identified(tmp_path: Path):
    path = tmp_path / "trace.csv"
    path.write_text(HEADER + ROW, encoding="utf-8")

    valid, info = procmon_io.inspect_csv(path)

    assert valid is True
    assert info["status"] == "success"
    assert procmon_io.classify_source(path) == "csv"
    assert procmon_io.discover_sources(folder=tmp_path) == [path.resolve()]


def test_unrelated_csv_is_rejected(tmp_path: Path):
    path = tmp_path / "inventory.csv"
    path.write_text("name,value\nalpha,1\n", encoding="utf-8")

    valid, info = procmon_io.inspect_csv(path)

    assert valid is False
    assert info["status"] == "unsupported_schema"
    assert procmon_io.classify_source(path) == "unknown"
    assert procmon_io.discover_sources(folder=tmp_path) == []


def test_folder_discovery_stops_at_candidate_budget(tmp_path: Path):
    (tmp_path / "00-inventory.csv").write_text("name,value\na,1\n", encoding="utf-8")
    trace = tmp_path / "01-trace.csv"
    trace.write_text(HEADER + ROW, encoding="utf-8")

    assert procmon_io.discover_sources(folder=tmp_path, max_candidates=1) == []
    assert procmon_io.discover_sources(folder=tmp_path, max_candidates=2) == [trace.resolve()]


def test_utf16_procmon_csv_streams_normalized_rows(tmp_path: Path):
    path = tmp_path / "trace-utf16.csv"
    path.write_text(HEADER + ROW, encoding="utf-16")
    rows: list[dict[str, str]] = []

    result = procmon_io.stream_csv(path, rows.append)

    assert result.status == "success"
    assert result.rows_seen == 1
    assert result.truncated is False
    assert rows[0]["process_name"] == "app.exe"
    assert rows[0]["result"] == "ACCESS DENIED"
    assert rows[0]["line_number"] == "2"


def test_streaming_row_and_deadline_budgets_are_explicit(tmp_path: Path):
    path = tmp_path / "large.csv"
    path.write_text(HEADER + ROW * 4, encoding="utf-8")
    rows: list[dict[str, str]] = []

    limited = procmon_io.stream_csv(path, rows.append, max_rows=2)
    expired = procmon_io.stream_csv(
        path, lambda _row: None, deadline_monotonic=time.monotonic() - 1,
    )

    assert limited.status == "budget_exhausted"
    assert limited.rows_seen == 2
    assert limited.stop_reason == "row_budget_exhausted"
    assert len(rows) == 2
    assert expired.status == "budget_exhausted"
    assert expired.rows_seen == 0
    assert expired.stop_reason == "deadline_exhausted"


def test_noise_aware_analysis_keeps_signal_and_suppresses_control_flow():
    analyzer = procmon_analysis.ProcmonAnalyzer()
    base = {
        "timestamp": "5:08:40.3306159 PM",
        "process_name": "app.exe",
        "parent_pid": "100",
        "pid": "200",
        "operation": "CreateFile",
        "path": r"C:\app\config.json",
        "source_file": "trace.csv",
        "line_number": "2",
    }
    for result, count in (
        ("ACCESS DENIED", 3),
        ("NAME NOT FOUND", 2),
        ("BUFFER OVERFLOW", 10),
        ("SUCCESS", 10),
    ):
        for _ in range(count):
            analyzer.consume({**base, "result": result})

    summary = analyzer.summary()

    assert summary["incident"]["rows"] == 25
    assert [item["type"] for item in summary["patterns"]] == ["access_denied"]
    assert summary["candidate_cause"].startswith("access_denied pattern")
    assert summary["patterns"][0]["falsifier"]


def test_procmon_duration_parser_returns_numeric_seconds():
    assert procmon_analysis.parse_duration_seconds("0.7500000") == 0.75
    assert procmon_analysis.parse_duration_seconds("not recorded") is None


def test_procmon_symptom_relevance_keeps_background_out_of_candidate():
    patterns = [{
        "type": "network_failure", "severity": "warning",
        "process_name": "updater.exe", "operation": "TCP Connect",
        "result": "CONNECTION REFUSED", "path": "updates.example:443",
        "operation_family": "network", "count": 20,
        "baseline_ratio": None, "falsifier": "network succeeds",
    }, {
        "type": "access_denied", "severity": "warning",
        "process_name": "w3wp.exe", "operation": "CreateFile",
        "result": "ACCESS DENIED", "path": r"C:\site\web.config",
        "operation_family": "filesystem", "count": 4,
        "baseline_ratio": "new_in_incident", "falsifier": "same access succeeds",
    }]

    assessed, context = symptom_relevance.assess_procmon_patterns(
        patterns,
        {
            "problem_type": "5xx_error",
            "question": "IIS w3wp returns 500 after deployment with access denied",
        },
    )

    assert assessed[0]["type"] == "access_denied"
    assert assessed[0]["signal_classification"] == "supportive"
    network = next(item for item in assessed if item["type"] == "network_failure")
    assert network["signal_classification"] == "background"
    assert context["candidate_pattern"]["type"] == "access_denied"


def test_procmon_without_symptom_scope_stays_contextual():
    assessed, context = symptom_relevance.assess_procmon_patterns(
        [{
            "type": "access_denied", "severity": "warning",
            "process_name": "w3wp.exe", "operation": "CreateFile",
            "result": "ACCESS DENIED", "path": r"C:\site\web.config",
            "count": 250, "falsifier": "same access succeeds",
        }],
        {},
    )

    assert assessed[0]["signal_classification"] == "contextual"
    assert assessed[0]["symptom_relevance"] == "unscoped"
    assert context["candidate_pattern"] is None


def test_structured_auth_problem_type_infers_authentication_processes():
    context = symptom_relevance.extract_context({"problem_type": "auth_error"})

    assert {"lsass.exe", "msedge.exe", "w3wp.exe"} <= set(
        context["target_processes"]
    )


def test_procmon_auth_file_probe_from_off_target_process_is_background():
    assessed, context = symptom_relevance.assess_procmon_patterns(
        [{
            "type": "repeated_path_probe", "severity": "info",
            "process_name": "Explorer.EXE", "operation": "CreateFile",
            "result": "NAME NOT FOUND", "path": r"C:\shell\marker",
            "count": 417, "falsifier": "healthy baseline matches",
        }],
        {
            "problem_type": "auth_error",
            "question": "Browser repeatedly prompts for authentication and fails",
        },
    )

    assert assessed[0]["signal_classification"] == "background"
    assert assessed[0]["noise_risk"] == "high"
    assert context["candidate_pattern"] is None


def test_procmon_auth_does_not_promote_browser_file_access_noise():
    assessed, context = symptom_relevance.assess_procmon_patterns(
        [{
            "type": "access_denied", "severity": "warning",
            "process_name": "chrome.exe", "operation": "CreateFile",
            "result": "PRIVILEGE NOT HELD", "path": r"C:\browser\cache.dat",
            "count": 56, "falsifier": "the same access succeeds",
        }],
        {
            "problem_type": "auth_error",
            "question": "Browser repeatedly prompts for authentication and fails",
        },
    )

    assert assessed[0]["signal_classification"] == "contextual"
    assert context["candidate_pattern"] is None


def test_procmon_preserves_dc_udp_send_receive_imbalance_for_auth_scope():
    analyzer = procmon_analysis.ProcmonAnalyzer()
    for index in range(10):
        analyzer.consume({
            "timestamp": f"5:08:{50 + index // 3:02d}.{index:03d} PM",
            "process_name": "lsass.exe", "pid": "1356",
            "operation": "UDP Send", "result": "SUCCESS",
            "path": f"client.local:{61000 + index} -> 10.0.0.{index + 1}:389",
            "source_file": "trace.csv", "line_number": str(index + 2),
        })
    analyzer.consume({
        "timestamp": "5:08:55.000 PM",
        "process_name": "lsass.exe", "pid": "1356",
        "operation": "UDP Receive", "result": "SUCCESS",
        "path": "client.local:62000 -> 10.0.0.1:389",
        "source_file": "trace.csv", "line_number": "20",
    })

    patterns = analyzer.summary()["patterns"]
    dc_gap = next(item for item in patterns if item["type"] == "dc_udp_response_gap")
    assessed, context = symptom_relevance.assess_procmon_patterns(
        patterns,
        {
            "problem_type": "auth_error",
            "question": "Browser repeatedly prompts for authentication and fails",
        },
    )
    assessed_gap = next(item for item in assessed if item["type"] == "dc_udp_response_gap")

    assert dc_gap["send_count"] == 10
    assert dc_gap["receive_count"] == 1
    assert dc_gap["distinct_remote_endpoints"] == 10
    assert assessed_gap["signal_classification"] == "supportive"
    assert context["candidate_pattern"]["type"] == "dc_udp_response_gap"


def test_time_only_events_are_anchored_across_midnight():
    incident = procmon_analysis.parse_range({
        "start": "2026-08-20T23:59:50",
        "end": "2026-08-21T00:00:10",
    })
    analyzer = procmon_analysis.ProcmonAnalyzer(incident_range=incident)
    base = {
        "process_name": "app.exe",
        "pid": "200",
        "operation": "ReadFile",
        "result": "SUCCESS",
        "path": r"C:\app\data.bin",
    }

    analyzer.consume({**base, "timestamp": "11:59:59.900 PM"})
    analyzer.consume({**base, "timestamp": "12:00:00.100 AM"})

    summary = analyzer.summary()
    assert summary["incident"]["rows"] == 2
    assert summary["timestamp_model"]["wall_clock_comparable"] is True
    assert summary["incident"]["last_time"].startswith("2026-08-21")


def test_detail_metrics_capture_io_and_process_profiling_without_raw_detail():
    analyzer = procmon_analysis.ProcmonAnalyzer()
    analyzer.consume({
        "timestamp": "5:08:40 PM",
        "process_name": "msedge.exe",
        "pid": "100",
        "operation": "ReadFile",
        "result": "SUCCESS",
        "path": r"C:\Edge\data.bin",
        "detail": "Offset: 0, Length: 4,096, I/O Flags: Non-cached",
    })
    analyzer.consume({
        "timestamp": "5:08:41 PM",
        "process_name": "msedge.exe",
        "pid": "100",
        "operation": "Process Profiling",
        "result": "SUCCESS",
        "path": "",
        "detail": (
            "User Time: 51.3437500 seconds, Kernel Time: 103.6250000 seconds, "
            "Private Bytes: 9,334,784, Working Set: 28,004,352"
        ),
    })

    incident = analyzer.summary()["incident"]
    profile = incident["process_profiling"][0]

    assert incident["io"]["observed_transfer_bytes"] == 4096
    assert incident["io"]["top_processes_by_observed_bytes"][0]["key"] == "msedge.exe"
    assert profile["user_time_seconds_max"] == 51.34375
    assert profile["kernel_time_seconds_max"] == 103.625
    assert profile["private_bytes_max"] == 9_334_784
    assert profile["working_set_bytes_max"] == 28_004_352
    assert "detail" not in profile


def test_pml_export_uses_argv_and_validates_csv(tmp_path: Path):
    executable = tmp_path / "Procmon64.exe"
    executable.write_bytes(b"trusted-test-binary")
    pml = tmp_path / "trace.pml"
    pml.write_bytes(b"PML-test")
    output = tmp_path / "trace.csv"
    calls: list[tuple[list[str], dict]] = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("/SaveAs") + 1]).write_text(HEADER + ROW, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = procmon_io.export_pml(pml, output, executable, runner=fake_runner)

    assert result.status == "success"
    assert calls[0][1]["shell"] is False
    assert calls[0][0][0] == str(executable.resolve())
    assert "/OpenLog" in calls[0][0]
    assert "/SaveAs" in calls[0][0]
    assert result.executable_sha256


def test_pml_export_timeout_is_structured(tmp_path: Path):
    executable = tmp_path / "Procmon.exe"
    executable.write_bytes(b"trusted-test-binary")
    pml = tmp_path / "trace.pml"
    pml.write_bytes(b"PML-test")
    output = tmp_path / "trace.csv"

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = procmon_io.export_pml(
        pml, output, executable, timeout_seconds=1, runner=timeout_runner,
    )

    assert result.status == "conversion_timeout"
    assert result.output_csv is None
    assert result.executable_sha256
    assert not output.exists()


def _run_skill(ctx: dict, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    script = PROCMON_ROOT / "procmon_analyzer_common.py"
    process = subprocess.run(
        [sys.executable, str(script), json.dumps(ctx), *args],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
    )
    return process, json.loads(process.stdout)


def test_common_skill_emits_candidate_not_root_cause(tmp_path: Path):
    path = tmp_path / "trace.csv"
    target_row = ROW.replace(",app.exe,", ",w3wp.exe,", 1)
    path.write_text(HEADER + target_row * 25, encoding="utf-8")
    process, result = _run_skill({
        "time_range": {
            "start": "2026-08-20T17:08:39",
            "end": "2026-08-20T17:08:41",
        },
        "extra": {
            "procmon_paths": [str(path)],
            "procmon_time_basis": "context",
        },
        "problem_type": "5xx_error",
        "question": "IIS w3wp returns 500 with access denied",
    })

    assert process.returncode == 0, process.stderr
    assert result["skill"] == "procmon"
    assert result["ok"] is True
    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert result["raw"]["candidate_cause"]
    assert result["raw"]["quality"]["independent_sources"] == 1
    assert result["raw"]["quality"]["time_comparable"] is True


def test_common_skill_applies_inferred_process_scope_before_aggregation(tmp_path: Path):
    path = tmp_path / "auth-trace.csv"
    noise = ROW.replace(",app.exe,", ",Explorer.EXE,", 1)
    sends = "".join(
        (
            f"5:08:5{index}.000 PM,lsass.exe,4,1356,300,UDP Send,SUCCESS,"
            f"client.local:{61000 + index} -> 10.0.0.{index + 1}:389,Length: 64,,,0\n"
        )
        for index in range(5)
    )
    path.write_text(HEADER + noise * 25 + sends, encoding="utf-8")

    process, result = _run_skill({
        "problem_type": "auth_error",
        "question": "Browser repeatedly prompts for authentication and fails",
        "extra": {"procmon_paths": [str(path)]},
    })

    assert process.returncode == 0, process.stderr
    assert result["raw"]["analysis_scope"]["mode"] == "symptom_inferred"
    assert "lsass.exe" in result["raw"]["analysis_scope"]["process_names"]
    assert result["raw"]["analysis"]["rows_filtered"] == 25
    assert result["raw"]["analysis"]["incident"]["rows"] == 5
    assert "UDP/389" in result["raw"]["candidate_cause"]


def test_incident_json_cannot_select_procmon_executable(tmp_path: Path):
    pml = tmp_path / "trace.pml"
    pml.write_bytes(b"PML-test")
    process, result = _run_skill({
        "extra": {
            "procmon_paths": [str(pml)],
            "procmon_exe": str(tmp_path / "attacker.exe"),
        }
    })

    assert process.returncode == 1
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["rejected_control_fields"] == ["procmon_exe"]
    assert result["raw"]["acquisitions"][0]["status"] == "tool_unavailable"


def test_unzoned_procmon_time_is_not_comparable_without_operator_confirmation(tmp_path: Path):
    path = tmp_path / "trace.csv"
    path.write_text(HEADER + ROW * 25, encoding="utf-8")
    process, result = _run_skill({
        "time_range": {
            "start": "2026-08-20T17:08:39",
            "end": "2026-08-20T17:08:41",
        },
        "extra": {"procmon_paths": [str(path)]},
    })

    assert process.returncode == 0
    assert result["raw"]["quality"]["time_comparable"] is False
    assert result["raw"]["timestamp_basis"]["mode"] == "unknown"
    assert result["evidence_state"] == "TIME_NOT_COMPARABLE"


def test_html_report_is_pre_redacted(tmp_path: Path):
    path = tmp_path / "private-trace.csv"
    report = tmp_path / "report.html"
    path.write_text(HEADER + ROW * 25, encoding="utf-8")

    process, result = _run_skill({
        "time_range": {
            "start": "2026-08-20T17:08:39",
            "end": "2026-08-20T17:08:41",
        },
        "extra": {"procmon_paths": [str(path)]},
    }, "--report", str(report))
    html = report.read_text(encoding="utf-8")

    assert process.returncode == 0
    assert result["raw"]["report_html_path"]
    for secret in (
        r"DOMAIN\user", r"C:\secret", r"C:\app", "--token", "secret",
    ):
        assert secret not in html
    assert "config.json" in html
    assert "Procmon-only patterns are candidates" in html