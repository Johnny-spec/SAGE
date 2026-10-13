#!/usr/bin/env python3
"""
event_log_analyzer.py — Windows Event Log correlator (v2)

Improvements vs v1:
  * Uses Get-WinEvent + FilterHashtable (faster, supports newer logs,
    works on systems where Get-EventLog has been deprecated)
  * Actually honours context.time_range instead of the fake "last 1 hour"
  * Uses _shared.contract for envelope + path resolution
  * Graceful degradation when not running elevated
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_default_product_root = Path(__file__).resolve().parents[4] / "IIS"
_product_root = Path(os.environ.get("COMMON_SKILL_PRODUCT_ROOT", str(_default_product_root))).resolve()
sys.path.insert(0, str(_product_root))
from _shared.contract import (  # noqa: E402
    Finding, SkillResult, fail, load_context, parse_time, time_range, in_window,
    _normalize_tz,
)
from _shared import playbook  # noqa: E402
from _shared.logs import evtx as evtx_reader  # noqa: E402  (shared wrappers; kept for downstream reuse)
from investigation_protocol import (  # type: ignore[reportMissingImports]  # noqa: E402
    EvidenceGap, EvidenceState,
)

_ = evtx_reader  # silence unused-import while preserving the documented dependency

SKILL_ID = "event_log"

EVENT_ID_MAP: dict[int, dict[str, str]] = {
    1000: {"name": ".NET application crash", "severity": "critical"},
    1001: {"name": "Application pool recycle", "severity": "warning"},
    1026: {"name": ".NET runtime error",      "severity": "critical"},
    1022: {"name": ".NET runtime profiler/load error", "severity": "warning"},
    1309: {"name": "ASP.NET unhandled exception", "severity": "critical"},
    2004: {"name": "Resource exhaustion (perfmon alert)", "severity": "critical"},
     219: {"name": "Driver / disk warning",   "severity": "warning"},
      12: {"name": "Operating system started", "severity": "info"},
      13: {"name": "Operating system shutdown", "severity": "info"},
    1074: {"name": "Planned restart/shutdown initiated", "severity": "info"},
    6005: {"name": "Event log service started", "severity": "info"},
    6006: {"name": "Event log service stopped", "severity": "info"},
    6008: {"name": "Unexpected shutdown", "severity": "critical"},
    7000: {"name": "Service failed to start", "severity": "critical"},
    7009: {"name": "Service start timeout",   "severity": "warning"},
    7031: {"name": "Service terminated unexpectedly", "severity": "critical"},
    7034: {"name": "Service terminated unexpectedly", "severity": "critical"},
    7036: {"name": "Service state changed", "severity": "info"},
    5719: {"name": "Domain controller unreachable", "severity": "warning"},
}

# Map IIS problem type → which event ids most likely explain it
ROOT_CAUSE_HINTS: dict[str, list[int]] = {
    "5xx_error":      [1000, 1001, 1026, 1309, 7031, 7034],
    "high_latency":   [2004, 219],
    "auth_error":     [],   # security log, see security_audit skill
}

INFO_EVENT_IDS = (12, 13, 1074, 6005, 6006, 6008, 7031, 7034, 7036)
DEFAULT_LEVELS = (1, 2, 3)


def _ps_quote(value: str) -> str:
    """Return a PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _select_projection() -> str:
    return (
        " Select-Object @{n='TimeCreated';e={$_.TimeCreated.ToString('o')}},"
        "                Id, ProviderName, LevelDisplayName, RecordId,"
        "                @{n='MachineName';e={$_.MachineName}},"
        "                @{n='Properties';e={($_.Properties | ForEach-Object { [string]$_.Value }) -join ' || ' }},"
        "                @{n='Message';e={$_.Message -replace \"`r`n\",' ' }}"
    )


def _run_winevent_filter_result(
    filter_body: str, timeout: int
) -> evtx_reader.EvtxReadResult:
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$ErrorActionPreference='Stop';try {"
        f"$f=@{{{filter_body}}};"
        "$events=@(Get-WinEvent -FilterHashtable $f -ErrorAction Stop);"
        "if(-not $events){'[]'}else{$events |"
        + _select_projection() + " |"
        " ConvertTo-Json -Depth 3 -Compress}"
        "}catch{if($_.Exception.Message -match 'No events were found'){'[]';exit 0};"
        "[Console]::Error.Write($_.Exception.Message);exit 3}"
    )
    return evtx_reader._run_ps_result(ps, timeout)


def _run_winevent_filter(filter_body: str, timeout: int) -> list[dict[str, Any]]:
    """Legacy list API retained for callers that do not consume status."""
    return _run_winevent_filter_result(filter_body, timeout).entries


def _combine_reads(
    reads: list[evtx_reader.EvtxReadResult], source: str
) -> evtx_reader.EvtxReadResult:
    entries = [entry for read in reads for entry in read.entries]
    statuses = {read.status for read in reads}
    if statuses == {"success"}:
        status = "success"
    elif "success" in statuses:
        status = "partial"
    elif len(statuses) == 1:
        status = next(iter(statuses))
    else:
        status = "failed"
    errors = sorted({read.error for read in reads if read.error})
    return evtx_reader.EvtxReadResult(
        status=status,
        entries=[_normalise_event(entry, source) for entry in _dedupe_events(entries)],
        error="; ".join(errors) if errors else None,
    )


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = (
            str(ev.get("SourcePath") or ev.get("LogName") or ""),
            str(ev.get("MachineName") or ""),
            str(ev.get("RecordId") or ""),
            str(ev.get("TimeCreated") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _normalise_event(ev: dict[str, Any], source: str) -> dict[str, Any]:
    ev.setdefault("SourcePath", source)
    ev.setdefault("MachineName", "")
    ev.setdefault("RecordId", "")
    ev.setdefault("Properties", "")
    ev.setdefault("Message", "")
    return ev


def _query_winevent(log_name: str, start: datetime, end: datetime,
                    timeout: int = 45) -> list[dict[str, Any]]:
    """Query a Windows event log via PowerShell."""
    return _query_winevent_result(log_name, start, end, timeout).entries


def _query_winevent_result(
    log_name: str, start: datetime, end: datetime, timeout: int = 45
) -> evtx_reader.EvtxReadResult:
    base = (
        f"LogName={_ps_quote(log_name)}; "
        f"StartTime=[datetime]'{start.strftime('%Y-%m-%d %H:%M:%S')}'; "
        f"EndTime=[datetime]'{end.strftime('%Y-%m-%d %H:%M:%S')}'"
    )
    level_csv = ",".join(str(x) for x in DEFAULT_LEVELS)
    info_csv = ",".join(str(x) for x in INFO_EVENT_IDS)
    reads = [
        _run_winevent_filter_result(f"{base}; Level={level_csv}", timeout),
        _run_winevent_filter_result(f"{base}; Id={info_csv}", timeout),
    ]
    return _combine_reads(reads, log_name)


def _query_winevent_from_path(evtx_path: Path, start: datetime, end: datetime,
                              timeout: int = 60) -> list[dict[str, Any]]:
    """Read a saved .evtx file using provider-side time/level filters."""
    return _query_winevent_from_path_result(evtx_path, start, end, timeout).entries


def _query_winevent_from_path_result(
    evtx_path: Path, start: datetime, end: datetime, timeout: int = 60
) -> evtx_reader.EvtxReadResult:
    if not evtx_path.is_file():
        return evtx_reader.EvtxReadResult(
            status="missing_input",
            error=f"EVTX file not found: {evtx_path.name}",
        )
    if not evtx_reader.has_valid_evtx_header(evtx_path):
        return evtx_reader.EvtxReadResult(
            status="unsupported_schema",
            error=f"File is not a valid EVTX capture: {evtx_path.name}",
        )
    base = (
        f"Path={_ps_quote(str(evtx_path))}; "
        f"StartTime=[datetime]'{start.strftime('%Y-%m-%d %H:%M:%S')}'; "
        f"EndTime=[datetime]'{end.strftime('%Y-%m-%d %H:%M:%S')}'"
    )
    level_csv = ",".join(str(x) for x in DEFAULT_LEVELS)
    info_csv = ",".join(str(x) for x in INFO_EVENT_IDS)
    reads = [
        _run_winevent_filter_result(f"{base}; Level={level_csv}", timeout),
        _run_winevent_filter_result(f"{base}; Id={info_csv}", timeout),
    ]
    return _combine_reads(reads, evtx_path.name)


def _event_text(ev: dict[str, Any]) -> str:
    return " ".join(str(ev.get(k) or "") for k in ("Message", "Properties"))


def classify_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Return a problem signature for high-value IIS/.NET event patterns."""
    eid = int(ev.get("Id", 0) or 0)
    text = _event_text(ev)
    lower = text.lower()
    if eid == 1309 and "fileloadexception" in lower and (
        "different grant set" in lower or "0x80131401" in lower
    ):
        return {
            "type": "aspnet_different_grant_set",
            "priority": 10,
            "severity": "critical",
            "summary": (
                "ASP.NET FileLoadException 0x80131401: assembly load would "
                "produce a different grant set"
            ),
            "root_cause": (
                "ASP.NET loaded assemblies with inconsistent CAS evidence/trust, "
                "commonly caused by mixing UNC share and local application paths "
                "or stale Temporary ASP.NET Files/shadow-copy state."
            ),
        }
    if eid == 1309 and "webexception" in lower and "500" in lower:
        return {
            "type": "aspnet_backend_500",
            "priority": 30,
            "severity": "critical",
            "summary": "ASP.NET handler received a backend HTTP 500",
            "root_cause": "A downstream application endpoint returned HTTP 500 to the web tier.",
        }
    if eid in (7031, 7034):
        return {
            "type": "service_crash",
            "priority": 20,
            "severity": "critical",
            "summary": "Windows service terminated unexpectedly",
            "root_cause": "A Windows service crashed or terminated during the incident window.",
        }
    return None


def correlate(events: list[dict[str, Any]], start: datetime, end: datetime,
              tolerance_min: float = 2.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        tc = ev.get("TimeCreated")
        if not tc:
            continue
        try:
            ts = parse_time(tc)
        except ValueError:
            continue
        if not in_window(ts, start, end, tolerance_min):
            continue
        eid = int(ev.get("Id", 0) or 0)
        meta = EVENT_ID_MAP.get(eid, {"name": "Unmapped event", "severity": "info"})
        diff_min = (_normalize_tz(ts, start)[0] - _normalize_tz(ts, start)[1]).total_seconds() / 60 if start else 0.0
        out.append({
            "event_id": eid,
            "name": meta["name"],
            "severity": meta["severity"],
            "time": tc,
            "provider": ev.get("ProviderName"),
            "machine": ev.get("MachineName"),
            "source": ev.get("SourcePath"),
            "message": (_event_text(ev))[:700],
            "signature": classify_event(ev),
            "correlation": "strong" if abs(diff_min) <= 1 else "weak",
            "minutes_from_window_start": round(diff_min, 2),
        })
    out.sort(key=lambda x: (
        (x.get("signature") or {}).get("priority", 100),
        abs(x["minutes_from_window_start"]),
    ))
    return out


def infer_root_cause(problem_type: str | None,
                     correlated: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Return (root_cause_text, confidence)."""
    if not correlated:
        return None, "low"
    signatures = [e["signature"] for e in correlated if e.get("signature")]
    if signatures:
        sig = sorted(signatures, key=lambda s: s.get("priority", 100))[0]
        return sig["root_cause"], "high"
    hints = set(ROOT_CAUSE_HINTS.get(problem_type or "", []))
    matches = [e for e in correlated if e["event_id"] in hints]
    if matches:
        e = matches[0]
        return f"{e['name']} (event {e['event_id']}) at {e['time']}", "high"
    crit = next((e for e in correlated if e["severity"] == "critical"), None)
    if crit:
        return f"{crit['name']} at {crit['time']}", "medium"
    return f"{correlated[0]['name']} at {correlated[0]['time']}", "low"


def summarise_events(events: list[dict[str, Any]], correlated: list[dict[str, Any]]) -> dict[str, Any]:
    by_source = Counter(str(e.get("SourcePath") or e.get("LogName") or "-") for e in events)
    by_machine = Counter(str(e.get("MachineName") or "-").split(".")[0] for e in events)
    by_provider_id = Counter(
        f"{e.get('ProviderName') or '-'}:{int(e.get('Id', 0) or 0)}" for e in events
    )
    sig_counts: dict[str, int] = defaultdict(int)
    for c in correlated:
        sig = c.get("signature")
        if sig:
            sig_counts[sig["type"]] += 1
    return {
        "by_source": by_source.most_common(20),
        "by_machine": by_machine.most_common(20),
        "by_provider_event_id": by_provider_id.most_common(20),
        "problem_signatures": sorted(sig_counts.items(), key=lambda kv: kv[1], reverse=True),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows Event Log correlator")
    ap.add_argument("context", nargs="?", default=None,
                    help="JSON context (string, @file) or omit to read stdin")
    ap.add_argument("--logs", nargs="+", default=["Application", "System"],
                    help="Event logs to query (live mode)")
    ap.add_argument("--evtx", action="append", default=[],
                    help="Path to a saved .evtx file. Repeat for multiple. "
                         "When given, --logs is ignored and offline mode is used.")
    ap.add_argument("--tolerance-min", type=float, default=2.0)
    args = ap.parse_args(argv)

    if args.context is None and not sys.stdin.isatty():
        ctx = json.loads(sys.stdin.read())
    else:
        ctx = load_context([args.context] if args.context else [])

    # Merge --evtx with extra.evtx_paths from JSON context.
    extra_paths = list((ctx.get("extra") or {}).get("evtx_paths") or [])
    evtx_paths = [Path(p) for p in (list(args.evtx) + extra_paths)]

    start, end = time_range(ctx)
    if start is None or end is None:
        # Fallback: last hour ending now
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        start = end - timedelta(hours=1)

    all_events: list[dict[str, Any]] = []
    acquisitions: list[dict[str, Any]] = []
    source_note: str
    if evtx_paths:
        for path in evtx_paths:
            read = _query_winevent_from_path_result(path, start, end)
            all_events.extend(read.entries)
            acquisitions.append({
                "source": path.name,
                "mode": "offline",
                "status": read.status,
                "record_count": len(read.entries),
                "error": read.error,
            })
        source_note = "evtx:" + ",".join(path.name for path in evtx_paths)
    else:
        for log_name in args.logs:
            read = _query_winevent_result(log_name, start, end)
            all_events.extend(read.entries)
            acquisitions.append({
                "source": log_name,
                "mode": "live",
                "status": read.status,
                "record_count": len(read.entries),
                "error": read.error,
            })
        source_note = "live_eventlog:" + ",".join(args.logs)

    statuses = {item["status"] for item in acquisitions}
    acquisition_complete = bool(acquisitions) and statuses == {"success"}
    acquisition_succeeded = any(item["status"] == "success" for item in acquisitions)
    missing_input = "missing_input" in statuses
    permission_blocked = "permission_blocked" in statuses
    unsupported_schema = "unsupported_schema" in statuses
    acquisition_failed = not acquisition_complete

    correlated = correlate(all_events, start, end, args.tolerance_min)
    candidate_cause, confidence = infer_root_cause(ctx.get("problem_type"), correlated)
    if acquisition_failed:
        candidate_cause = None
        confidence = "low"

    findings = [
        Finding(
            summary=(
                f"{c['signature']['summary']} at {c['time']}"
                if c.get("signature") else
                f"{c['name']} (event {c['event_id']}) at {c['time']}"
            ),
            severity=c["severity"],
            evidence={"provider": c["provider"], "correlation": c["correlation"],
                      "delta_min": c["minutes_from_window_start"],
                      "machine": c.get("machine"),
                      "source": c.get("source"),
                      "signature": (c.get("signature") or {}).get("type")},
        )
        for c in correlated[:10]
    ]
    if not findings:
        findings.append(Finding(
            summary=(
                "No correlated events in the requested window"
                if acquisition_complete else
                "Event acquisition was incomplete; absence of events is not established"
            ),
            severity="info" if acquisition_complete else "warning",
        ))

    signature_types = {c["signature"]["type"] for c in correlated if c.get("signature")}
    recommendations = (
        [
            "Recycle the affected application pool or run iisreset to clear the poisoned AppDomain/shadow-copy state.",
            "Clear Temporary ASP.NET Files during a maintenance window if FileLoadException 0x80131401 recurs.",
            "Avoid running ASP.NET bin/App_Code assemblies from UNC paths; deploy runtime assemblies to local disk consistently on every web node.",
            "Compare IIS physical paths, virtual directories, app pool identity, .NET CLR/pipeline mode, trust level, GAC/bin DLL versions, and UNC share permissions across nodes.",
        ]
        if "aspnet_different_grant_set" in signature_types else
        (
            ["Inspect the listed events; cross-check with app_crash skill if event 1000/1026 is present."]
            if correlated else
            ["Resolve the event acquisition failures, then rerun the same window."]
            if acquisition_failed else
            ["No system-level events explain the IIS symptom in this window."]
        )
    )

    evidence_state = (
        EvidenceState.MISSING_REQUIRED_EVIDENCE if missing_input
        else EvidenceState.PERMISSION_BLOCKED if permission_blocked
        else EvidenceState.UNKNOWN_LOG_TYPE if unsupported_schema
        else EvidenceState.TOOL_UNAVAILABLE if acquisition_failed
        else EvidenceState.INCONCLUSIVE_EVIDENCE
    )
    evidence_gaps = []
    if acquisition_failed:
        evidence_gaps.append(EvidenceGap(
            code="event_acquisition_incomplete",
            summary="One or more Event Log sources could not be queried completely.",
            blocking=True,
            details={"statuses": sorted(statuses)},
        ))

    result = SkillResult(
        skill=SKILL_ID,
        ok=acquisition_succeeded or acquisition_complete,
        findings=findings,
        root_cause=None,
        confidence="medium" if candidate_cause and acquisition_complete else "low",
        recommendations=recommendations,
        evidence_state=evidence_state,
        evidence_gaps=evidence_gaps,
        raw={
            **({
                "failure": {
                    "code": (
                        next(iter(statuses)) if len(statuses) == 1
                        else "partial_acquisition" if acquisition_succeeded
                        else "acquisition_failed"
                    ),
                    "codes": sorted(statuses),
                    "statuses": sorted(statuses),
                }
            } if acquisition_failed else {}),
            "source": source_note,
            "queried_logs": args.logs if not evtx_paths else [],
            "evtx_paths": [p.name for p in evtx_paths],
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "total_events_returned": len(all_events),
            "correlated": correlated,
            "summary": summarise_events(all_events, correlated),
            "candidate_cause": candidate_cause,
            "acquisitions": acquisitions,
            "quality": {
                "capture_complete": acquisition_complete,
                "sample_sufficient": acquisition_complete and bool(all_events),
                "records_seen": len(all_events),
                "blocking_gaps": acquisition_failed,
                "direct_join": False,
                "independent_sources": 1 if all_events else 0,
                "time_comparable": acquisition_complete,
            },
        },
        error=(
            None if acquisition_succeeded or acquisition_complete
            else "Event Log acquisition failed."
        ),
    )
    # Seed playbook merge from the caller-supplied problem_type (e.g. "5xx_error").
    seed_pt = ctx.get("problem_type")
    if seed_pt:
        playbook.merge_into_result(result, [seed_pt])
    result.emit()
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
