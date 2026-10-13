from __future__ import annotations

from pathlib import Path


def test_source_scan_is_readonly_redacted_and_builds_auth_plan(
    source_discovery_module, tmp_path: Path
):
    marker = tmp_path / "executed.txt"
    (tmp_path / "danger.ps1").write_text(
        f"Set-Content -Path '{marker}' -Value executed\n",
        encoding="utf-8",
    )
    (tmp_path / "web.config").write_text(
        '<configuration><system.webServer><security><authentication>\n'
        '<windowsAuthentication enabled="true" useAppPoolCredentials="true" client_secret=do-not-leak />\n'
        '</authentication></security></system.webServer></configuration>\n',
        encoding="utf-8",
    )
    result = source_discovery_module.analyze({
        "problem_type": "auth_error",
        "extra": {"product": "IIS", "repo_roots": [str(tmp_path)]},
    })

    assert result["ok"] is True
    assert result["evidence_state"] == "COLLECTION_PLAN_READY"
    assert not marker.exists(), "the scanner executed target code"
    raw = result["raw"]["logging_discovery"]
    assert raw["safety"]["target_code_executed"] is False
    assert raw["safety"]["target_modules_imported"] is False
    assert raw["safety"]["network_access_used"] is False
    assert raw["redaction"]["secret_values_removed"] >= 1
    serialized_hits = str(raw["source_hits"])
    assert "do-not-leak" not in serialized_hits
    kinds = {item["log_kind"] for item in result["collection_plan"]["requests"]}
    assert {"iis_w3c", "security_evtx", "evtx", "netlog"} <= kinds


def test_source_scan_respects_file_and_total_budgets(source_discovery_module, tmp_path: Path):
    (tmp_path / "large.cs").write_text("ILogger " * 1000, encoding="utf-8")
    result = source_discovery_module.scan_context({
        "extra": {
            "repo_roots": [str(tmp_path)],
            "max_file_bytes": 10,
            "max_total_bytes": 20,
        }
    })
    assert result["files_skipped_size"] == 1
    assert result["files_scanned"] == 0


def test_source_scan_without_roots_is_explicit_gap(source_discovery_module):
    result = source_discovery_module.analyze({})
    assert result["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert result["evidence_gaps"][0]["code"] == "source_roots_missing"


def test_source_scan_extracts_provider_event_and_netlog_candidates(
    source_discovery_module, tmp_path: Path
):
    (tmp_path / "Telemetry.cs").write_text(
        '[EventSource(Name="Contoso.Web")]\n'
        '[Event(42, Level = EventLevel.Error)]\n'
        'void Failed() { EventLog.WriteEntry("Contoso.Web", "failed"); }\n',
        encoding="utf-8",
    )
    (tmp_path / "network.cc").write_text(
        'TRACE_EVENT("net.auth", "AUTH_LIBRARY_INIT_SEC_CTX");\n'
        'auto event = SSL_HANDSHAKE_ERROR;\n',
        encoding="utf-8",
    )
    raw = source_discovery_module.scan_context({
        "extra": {"repo_roots": [str(tmp_path)]}
    })
    assert any(item["provider_name"] == "Contoso.Web"
               for item in raw["provider_candidates"])
    assert any(item.get("event_id") == 42 for item in raw["event_candidates"])
    assert any(item.get("provider_name") == "Contoso.Web"
               for item in raw["event_candidates"])
    names = {item["event_name"] for item in raw["netlog_candidates"]}
    assert {"AUTH_LIBRARY_INIT_SEC_CTX", "SSL_HANDSHAKE_ERROR"} <= names
    etl = next(item for item in raw["collection_plan"]["requests"]
               if item["log_kind"] == "etl")
    assert "logman create trace SourceGuided" in etl["commands"][0]["command"]
    assert "Contoso.Web" in etl["commands"][0]["command"]


def test_unsafe_provider_name_is_never_interpolated_into_command(
    source_discovery_module, tmp_path: Path
):
    (tmp_path / "Telemetry.cs").write_text(
        '[EventSource(Name="Bad\";Remove-Item C:\\\\*")]\n',
        encoding="utf-8",
    )
    raw = source_discovery_module.scan_context({
        "extra": {"repo_roots": [str(tmp_path)]}
    })
    commands = [
        command["command"]
        for request in raw["collection_plan"]["requests"]
        for command in request["commands"]
    ]
    assert not any("Remove-Item" in command for command in commands)