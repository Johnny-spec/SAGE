"""Self-contained, pre-redacted HTML report for Procmon analysis."""
from __future__ import annotations

import json
from html import escape
from typing import Any


_CSS = """
:root { color-scheme: light; --ink:#17202a; --muted:#667085; --line:#d9dee7;
        --paper:#fff; --wash:#f5f7fa; --warn:#9a6700; --critical:#b42318; }
* { box-sizing:border-box; } body { margin:0; background:var(--wash); color:var(--ink);
font:14px/1.45 "Segoe UI", sans-serif; } header { background:#17202a; color:#fff;
padding:24px max(24px,calc((100% - 1120px)/2)); } h1 { margin:0; font-size:24px; }
header p { color:#cbd5e1; margin:6px 0 0; } main { max-width:1120px; margin:auto;
padding:22px 24px 48px; } section { margin:0 0 24px; } h2 { font-size:17px;
border-bottom:1px solid var(--line); padding-bottom:6px; } .kpis { display:grid;
grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; } .kpi { background:var(--paper);
border:1px solid var(--line); padding:10px; border-radius:6px; } .kpi b { display:block;
font-size:20px; } .kpi span { color:var(--muted); font-size:12px; } table { width:100%;
border-collapse:collapse; background:var(--paper); } th,td { text-align:left; padding:7px 9px;
border-bottom:1px solid var(--line); vertical-align:top; } th { background:#eef1f5;
font-size:12px; } .warning { color:var(--warn); } .critical { color:var(--critical); }
code { font-family:Consolas,monospace; font-size:12px; } .note { color:var(--muted); }
"""


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(value if value is not None else ''))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_report(payload: dict[str, Any]) -> str:
    """Render a payload that has already passed through default redaction."""
    analysis = payload.get("analysis") or {}
    incident = analysis.get("incident") or {}
    quality = payload.get("quality") or {}
    acquisitions = payload.get("acquisitions") or []
    patterns = analysis.get("patterns") or []
    kpis = [
        ("Rows in incident", incident.get("rows", 0)),
        ("Input sources", len(acquisitions)),
        ("High-signal patterns", len(patterns)),
        ("Observed I/O bytes", (incident.get("io") or {}).get("observed_transfer_bytes", 0)),
        ("Capture complete", quality.get("capture_complete", False)),
        ("Time comparable", quality.get("time_comparable", False)),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><b>{escape(str(value))}</b><span>{escape(label)}</span></div>'
        for label, value in kpis
    )
    acquisition_rows = [
        [item.get("source"), item.get("kind"), item.get("status"), item.get("rows_seen"), item.get("stop_reason")]
        for item in acquisitions
    ]
    pattern_rows = [
        [
            item.get("severity"), item.get("type"), item.get("process_name"),
            item.get("operation"), item.get("result"), item.get("path"), item.get("count"),
            item.get("baseline_ratio"), item.get("next_source"),
        ]
        for item in patterns
    ]
    process_rows = [
        [item.get("key"), item.get("count")]
        for item in incident.get("top_processes") or []
    ]
    operation_rows = [
        [item.get("key"), item.get("count")]
        for item in incident.get("top_operations") or []
    ]
    profile_rows = [
        [
            item.get("process_name"), item.get("pid"), item.get("samples"),
            item.get("user_time_seconds_max"), item.get("kernel_time_seconds_max"),
            item.get("private_bytes_max"), item.get("working_set_bytes_max"),
        ]
        for item in incident.get("process_profiling") or []
    ]
    candidate = analysis.get("candidate_cause") or "No high-signal candidate met the configured thresholds."
    return "\n".join([
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>Procmon analysis report</title><style>{_CSS}</style></head><body>",
        "<header><h1>Procmon analysis report</h1><p>Bounded, redacted diagnostic evidence</p></header><main>",
        f'<section><div class="kpis">{kpi_html}</div></section>',
        f"<section><h2>Candidate assessment</h2><p>{escape(str(candidate))}</p>"
        '<p class="note">Procmon-only patterns are candidates, not independently proven root causes.</p></section>',
        "<section><h2>Acquisition quality</h2>" + _table(
            ["Source", "Kind", "Status", "Rows", "Limit"], acquisition_rows
        ) + "</section>",
        "<section><h2>High-signal patterns</h2>" + _table(
            ["Severity", "Pattern", "Process", "Operation", "Result", "Redacted target", "Count", "Baseline", "Next evidence"],
            pattern_rows,
        ) + "</section>",
        "<section><h2>Top processes</h2>" + _table(["Process", "Events"], process_rows) + "</section>",
        "<section><h2>Top operations</h2>" + _table(["Operation", "Events"], operation_rows) + "</section>",
        "<section><h2>Process profiling</h2>" + _table(
            ["Process", "PID", "Samples", "User seconds", "Kernel seconds", "Private bytes", "Working set bytes"],
            profile_rows,
        ) + "</section>",
        "<section><h2>Quality model</h2><pre><code>" + escape(json.dumps(quality, indent=2)) + "</code></pre></section>",
        "</main></body></html>",
    ])


__all__ = ["render_report"]