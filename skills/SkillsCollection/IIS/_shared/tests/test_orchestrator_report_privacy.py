from __future__ import annotations

from pathlib import Path

import skill_orchestrator as orchestrator


def test_report_redacts_paths_urls_identity_and_event_text(tmp_path: Path):
    report = tmp_path / "report.html"
    private_log = r"C:\Users\alice\private\u_ex.log"
    orchestrator._write_report(
        report,
        folder=Path(r"C:\Users\alice\incident"),
        recursive=True,
        discovery={"iis_w3c": [private_log]},
        anchors=[],
        window_seconds=300,
        error_block={
            "pattern": "password=hunter2",
            "hits": [{"file": private_log, "excerpt": "token=secret"}],
        },
        executed=["iis_logs"],
        results={
            "iis_logs": {
                "ok": True,
                "findings": [{
                    "summary": "failed at https://alice:hunter2@example.test/x?q=secret",
                    "severity": "warning",
                }],
                "root_cause": None,
            }
        },
        secondary=None,
        root_chain=[],
        diagnosis={
            "summary": {},
            "timeline": [{"event": "password=hunter2", "log": private_log}],
            "key_evidence": [{
                "skill": "iis_logs",
                "summary": "user alice",
                "evidence": {"username": "alice", "message": "token=secret"},
            }],
            "key_logs": [{"kind": "iis", "sample_files": [private_log]}],
        },
    )

    html = report.read_text(encoding="utf-8")
    for secret in ("hunter2", "q=secret", "token=secret", r"C:\Users\alice"):
        assert secret not in html
    assert "https://example.test/x" in html
    assert "u_ex.log" in html


def test_html_agent_summary_is_escaped_not_executed(tmp_path: Path):
    summary = tmp_path / "summary.html"
    summary.write_text(
        '<script>alert("secret")</script><b>diagnosis</b>', encoding="utf-8"
    )

    rendered = orchestrator._render_agent_summary(summary)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<b>diagnosis</b>" not in rendered