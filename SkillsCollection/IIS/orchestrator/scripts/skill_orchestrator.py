#!/usr/bin/env python3
"""
skill_orchestrator.py — folder-first entry point + fan-out coordinator (v3)

Two operating modes, picked automatically from the first positional argument:

1. **Folder mode** (NEW)  — argument is an existing directory.
   * Discovers IIS / FTP / HTTPERR / EVTX logs underneath.
   * Optionally runs an error-pattern locator (`--error REGEX`) to derive
     anchor timestamps from arbitrary log lines.
   * Dispatches the relevant entry skills (iis_logs, ftp_logs, httperror,
     event_log) against the folder, passing `--around` anchors and a
     `time_range`.
   * Performs a secondary fan-out: every entry-skill payload that emits
     `raw.skills_to_trigger` feeds the existing `orchestrate()` helper.
   * Optionally writes an HTML summary via `--report`.

2. **Legacy pipe mode** (UNCHANGED) — argument is JSON, `@file`, a `.json`
   path, or stdin contains JSON. Behaves exactly like v2.

Skill paths are resolved from _shared/registry.json — never hard-coded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _shared.contract import (  # noqa: E402
    SkillResult, Finding, registry, skill_entry, load_context,
    Solution, NextStep, LogRequest,
)
from _shared.log_discovery import (  # noqa: E402
    discover_logs, IIS_KIND, FTP_KIND, HTTPERR_KIND, EVTX_KIND, NETLOG_KIND,
    PROCMON_KIND, UNKNOWN_KIND,
)
from _shared.error_locator import (  # noqa: E402
    find_error_anchors, dedup_anchors,
)
from _shared import playbook  # noqa: E402
from investigation_protocol import (  # type: ignore[reportMissingImports]  # noqa: E402
    EvidenceGap, EvidenceState, aggregate_root_quality, build_collection_plan,
    derive_evidence_state, gaps_from_missing,
)
from result_contract import (  # type: ignore[reportMissingImports]  # noqa: E402
    ContractIssue, ResultContractError, contract_failure, normalize_result,
    operational_failure,
)
from redaction import redact_default  # type: ignore[reportMissingImports]  # noqa: E402

SKILL_ID = "orchestrator"
DEFAULT_PER_SKILL_TIMEOUT = 60
DEFAULT_TOTAL_TIMEOUT = 180
DEFAULT_WINDOW = "5m"
DEFAULT_ERROR_MAX_HITS = 50
MAX_WORKERS = 3

# Module-level verbosity flag. When True the orchestrator prints a
# human-readable progress trace + final summary to stderr (stdout is
# always reserved for the JSON envelope). Toggled via --quiet / --verbose
# in main(); defaults to ON for folder mode so the user can see which
# skills ran and what they found without having to grep the JSON.
_VERBOSE = True


def _log(msg: str = "") -> None:
    """Print a progress/summary line to stderr (never stdout)."""
    if _VERBOSE:
        print(msg, file=sys.stderr, flush=True)


def _sev_tag(sev: str) -> str:
    return {
        "critical": "[CRIT]",
        "warning":  "[WARN]",
        "info":     "[INFO]",
    }.get(sev, f"[{sev.upper()}]")


def _print_skill_block(skill_id: str, res: dict[str, Any]) -> None:
    """Render one child skill's result as a compact text block on stderr."""
    if not _VERBOSE:
        return
    sep = "-" * 72
    _log(sep)
    if not isinstance(res, dict):
        _log(f"  skill={skill_id}  <invalid result type {type(res).__name__}>")
        return
    ok = res.get("ok")
    status = "OK" if ok else "FAIL"
    conf = res.get("confidence", "?")
    _log(f"  skill={skill_id:<12}  status={status:<4}  confidence={conf}")
    if not ok:
        err = res.get("error") or "unknown error"
        _log(f"    error: {err}")
        return
    rc = res.get("root_cause")
    if rc:
        _log(f"    root_cause: {rc}")
    findings = res.get("findings") or []
    if findings:
        _log(f"    findings ({len(findings)}):")
        for f in findings[:8]:
            sev = f.get("severity", "info")
            _log(f"      {_sev_tag(sev)} {f.get('summary', '')}")
        if len(findings) > 8:
            _log(f"      ... ({len(findings) - 8} more)")
    sols = res.get("solutions") or []
    if sols:
        _log(f"    solutions ({len(sols)}):")
        for s in sols[:5]:
            _log(f"      - [{s.get('severity','info')}] {s.get('title','')}")
        if len(sols) > 5:
            _log(f"      ... ({len(sols) - 5} more)")
    nxts = res.get("next_steps") or []
    if nxts:
        _log(f"    next_steps ({len(nxts)}):")
        for n in nxts[:5]:
            action = n.get("action", "")
            why = n.get("why", "")
            tag = f" ({n.get('skill')})" if n.get("skill") else ""
            line = f"      -> {action}{tag}"
            if why:
                line += f"  :: {why}"
            _log(line)
        if len(nxts) > 5:
            _log(f"      ... ({len(nxts) - 5} more)")

# Discovery LogKind ↔ shorthand surfaced in cross_log_context.available.
KIND_LABEL = {
    IIS_KIND: "iis_w3c", FTP_KIND: "ftp_w3c",
    HTTPERR_KIND: "http_err", EVTX_KIND: "evtx",
    NETLOG_KIND: "netlog", PROCMON_KIND: "procmon",
}

# Backward-compatible defaults; preferred source is registry metadata.
DEFAULT_SKILL_PRODUCES_KIND: dict[str, tuple[str, ...]] = {
    "iis_logs":   ("iis_w3c",),
    "ftp_logs":   ("ftp_w3c",),
    "httperror":  ("http_err",),
    "event_log":  ("evtx",),
    "netlog":     ("netlog",),
    "procmon":    ("procmon",),
}


def _skill_produces_kind_map() -> dict[str, tuple[str, ...]]:
    src = registry().get("source_registry") or {}
    if not isinstance(src, dict) or not src:
        return DEFAULT_SKILL_PRODUCES_KIND
    out: dict[str, tuple[str, ...]] = {}
    for skill, kinds in src.items():
        if isinstance(kinds, (list, tuple)):
            out[str(skill)] = tuple(str(k) for k in kinds if k)
    return out or DEFAULT_SKILL_PRODUCES_KIND


def _build_subcontext(iis_payload: dict[str, Any], problem_type: str | None) -> dict[str, Any]:
    metrics = iis_payload.get("metrics", {})
    extra = dict(iis_payload.get("extra") or {})
    suspicious_ips = sorted({
        str(evidence["client_ip"])
        for problem in iis_payload.get("problems", []) or []
        if isinstance(problem, dict)
        for evidence in [problem.get("evidence") or {}]
        if isinstance(evidence, dict) and evidence.get("client_ip")
    })
    if suspicious_ips:
        extra["suspicious_ips"] = suspicious_ips
    return {
        "time_range": metrics.get("time_range", {}),
        "problem_type": problem_type,
        "metrics": metrics,
        "extra": extra,
    }


def _build_orchestrator_subcontext(
    payload: dict[str, Any],
    skill_id: str,
    problems: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    context = _build_subcontext(
        payload, _problem_type_for_skill(skill_id, problems)
    )
    if skill_id == "procmon":
        context["extra"] = dict(context.get("extra") or {})
        context["extra"].setdefault("deadline_seconds", max(1, timeout - 10))
    return context


def _resolve_skill_runtime(skill_id: str) -> tuple[str, Path, str]:
    """Resolve the first complete, available runtime declared by the registry."""
    skill = (registry().get("skills") or {}).get(skill_id) or {}
    entries = skill.get("entry") or {}
    order = list(skill.get("runtime_order") or ("python", "pwsh"))
    order.extend(runtime for runtime in entries if runtime not in order)
    statuses = skill.get("runtime_status") or {}
    rejected: list[str] = []

    for runtime in order:
        status = str(statuses.get(runtime, "complete")).lower()
        if status in {"framework", "stub", "unimplemented"}:
            rejected.append(f"{runtime}={status}")
            continue
        try:
            entry = skill_entry(skill_id, runtime)
        except KeyError:
            continue
        if not entry.is_file():
            rejected.append(f"{runtime}=missing-entry")
            continue
        if runtime == "python":
            return runtime, entry, sys.executable
        if runtime == "pwsh":
            executable = shutil.which("pwsh")
            if executable:
                return runtime, entry, executable
            rejected.append("pwsh=unavailable")

    detail = f" ({', '.join(rejected)})" if rejected else ""
    raise RuntimeError(f"Skill {skill_id} has no complete available runtime{detail}")


def _parse_skill_process(
    skill_id: str,
    runtime: str,
    cmd: list[str],
    proc: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Prefer a child's contract envelope even when its process exits non-zero."""
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result = contract_failure(
            skill_id,
            [ContractIssue("$", f"invalid JSON: {exc}")],
            source_excerpt=proc.stdout,
        )
        result["error"] = f"non-JSON child output: {exc}"
    else:
        try:
            result = normalize_result(result, expected_skill=skill_id)
        except ResultContractError as exc:
            result = contract_failure(skill_id, exc.issues, source_excerpt=proc.stdout)

    raw = result["raw"]
    execution = raw.setdefault("execution", {})
    if not isinstance(execution, dict):
        execution = {}
        raw["execution"] = execution
    execution.update({"runtime": runtime, "exit_code": proc.returncode})
    if proc.stderr:
        execution["stderr_character_count"] = len(proc.stderr)
        execution["stderr_sha256"] = hashlib.sha256(
            proc.stderr.encode("utf-8", errors="replace")
        ).hexdigest()
    if proc.returncode != 0 and result.get("ok"):
        return operational_failure(
            skill_id,
            f"child exited with code {proc.returncode} despite ok=true",
            code="child_exit_mismatch",
            execution={
                "runtime": runtime,
                "stage": "execute",
                "exit_code": proc.returncode,
            },
        )
    return result


def _run_one(skill_id: str, ctx: dict[str, Any], timeout: int) -> dict[str, Any]:
    try:
        runtime, entry, executable = _resolve_skill_runtime(skill_id)
    except (KeyError, RuntimeError) as exc:
        return operational_failure(
            skill_id,
            str(exc),
            code="skill_resolution_failed",
            execution={"stage": "resolve"},
        )

    context_json = json.dumps(ctx)
    context_argument = context_json
    temporary_context: Path | None = None
    if len(context_json) > 7000:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as stream:
            stream.write(context_json)
            temporary_context = Path(stream.name)
        context_argument = f"@{temporary_context}"
    if runtime == "pwsh":
        cmd = [
            executable, "-NoProfile", "-NonInteractive", "-File", str(entry),
            "-Context", context_argument,
        ]
    else:
        cmd = [executable, str(entry), context_argument]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return operational_failure(
            skill_id,
            f"timed out after {timeout}s",
            code="timeout",
            execution={
                "runtime": runtime,
                "stage": "execute",
                "timeout_seconds": timeout,
            },
        )
    except OSError as exc:
        return operational_failure(
            skill_id,
            f"failed to start child process: {exc}",
            code="process_launch_failed",
            execution={"runtime": runtime, "stage": "launch"},
        )
    finally:
        if temporary_context is not None:
            temporary_context.unlink(missing_ok=True)
    return _parse_skill_process(skill_id, runtime, cmd, proc)


def _problem_type_for_skill(skill_id: str, problems: list[dict[str, Any]]) -> str | None:
    """Pick the most relevant problem_type for this skill (first match wins)."""
    reg = registry()["skills"]
    triggers = set(reg.get(skill_id, {}).get("triggers_for", []))
    for p in problems:
        if p.get("type") in triggers:
            return p["type"]
    return problems[0]["type"] if problems else None


def _aggregate_advisories(sub_results: dict[str, dict[str, Any]]
                          ) -> tuple[list[Solution], list[NextStep], list[LogRequest]]:
    """Collect `solutions` / `next_steps` / `additional_logs_needed` from every
    child result (dicts from JSON), dedup with playbook helpers, return
    canonical dataclass lists."""
    sols: list[Solution] = []
    nxts: list[NextStep] = []
    logs: list[LogRequest] = []
    for res in sub_results.values():
        if not isinstance(res, dict):
            continue
        for s in res.get("solutions") or []:
            if isinstance(s, dict):
                sols.append(Solution(
                    title=str(s.get("title", "")),
                    steps=list(s.get("steps") or []),
                    problem_ref=s.get("problem_ref"),
                    severity=str(s.get("severity") or "info"),
                    references=list(s.get("references") or []),
                ))
        for n in res.get("next_steps") or []:
            if isinstance(n, dict):
                nxts.append(NextStep(
                    action=str(n.get("action", "")),
                    why=n.get("why"),
                    skill=n.get("skill"),
                ))
        for l in res.get("additional_logs_needed") or []:
            if isinstance(l, dict):
                logs.append(LogRequest(
                    log_kind=str(l.get("log_kind", "")),
                    why=str(l.get("why", "")),
                    how_to_collect=l.get("how_to_collect"),
                    skill=l.get("skill"),
                ))
    return (
        playbook._dedup_solutions(sols),
        playbook._dedup_next_steps(nxts),
        playbook._dedup_log_requests(logs),
    )


_SUMMARY_TIME_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
)


def _obj_field(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _short_evidence(evidence: Any, max_items: int = 8) -> dict[str, Any]:
    """Keep diagnosis evidence concise; raw child results remain available."""
    if not isinstance(evidence, dict):
        return {}
    out: dict[str, Any] = {}
    for idx, (key, value) in enumerate(evidence.items()):
        if idx >= max_items:
            out["truncated"] = True
            break
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = value
        else:
            text = json.dumps(value, ensure_ascii=True, default=str)
        if isinstance(text, str) and len(text) > 240:
            text = text[:237] + "..."
        out[str(key)] = text
    return out


def _timeline_sort_key(value: str | None) -> tuple[int, str]:
    """Sort heterogeneous timestamps without mixing aware/naive datetimes."""
    if not value:
        return (1, "")
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return (0, parsed.isoformat(timespec="microseconds"))
    except ValueError:
        return (0, raw)


def _first_timestamp(summary: str, evidence: dict[str, Any] | None = None) -> str | None:
    evidence = evidence or {}
    for key in ("time", "timestamp", "datetime", "event_time", "start", "end"):
        value = evidence.get(key)
        if value:
            return str(value)
    match = _SUMMARY_TIME_RE.search(summary or "")
    return match.group("ts") if match else None


def _solution_dict(item: object) -> dict[str, Any]:
    return {
        "title": str(_obj_field(item, "title", "")),
        "severity": str(_obj_field(item, "severity", "info") or "info"),
        "problem_ref": _obj_field(item, "problem_ref"),
        "steps": list(_obj_field(item, "steps", []) or []),
        "references": list(_obj_field(item, "references", []) or []),
    }


def _next_step_dict(item: object) -> dict[str, Any]:
    return {
        "action": str(_obj_field(item, "action", "")),
        "why": _obj_field(item, "why"),
        "skill": _obj_field(item, "skill"),
    }


def _log_request_dict(item: object) -> dict[str, Any]:
    return {
        "log_kind": str(_obj_field(item, "log_kind", "")),
        "why": str(_obj_field(item, "why", "")),
        "how_to_collect": _obj_field(item, "how_to_collect"),
        "skill": _obj_field(item, "skill"),
    }


def _build_diagnosis(*,
                     results: dict[str, dict[str, Any]],
                     findings: list[Finding],
                     root_chain: list[dict[str, str]],
                     problems: list[dict[str, Any]] | None,
                     discovery: dict[str, list[str]] | None,
                     error_block: dict[str, Any] | None,
                     executed: list[str],
                     failures: list[str],
                     confidence: str,
                     time_range: dict[str, str] | None,
                     cross_log_context: dict[str, Any] | None,
                     solutions: list[Solution],
                     next_steps: list[NextStep],
                     additional_logs_needed: list[LogRequest],
                     missing_logs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build the operator-facing diagnosis section.

    This is deliberately additive and summary-oriented. Full child envelopes
    stay in raw.results; diagnosis gives humans a stable, readable map.
    """
    problems = problems or []
    discovery = discovery or {}
    error_block = error_block or {}
    missing_logs = missing_logs or []

    key_evidence: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []

    for hit in error_block.get("hits", []) or []:
        if not isinstance(hit, dict) or not hit.get("timestamp"):
            continue
        timeline.append({
            "time": str(hit.get("timestamp")),
            "source": "error_locator",
            "event": str(hit.get("excerpt") or "")[:240],
            "log": hit.get("file"),
            "line": hit.get("lineno"),
        })

    for skill, res in (results or {}).items():
        if not isinstance(res, dict) or not res.get("ok"):
            continue
        for raw_finding in (res.get("findings") or [])[:8]:
            if not isinstance(raw_finding, dict):
                continue
            summary = str(raw_finding.get("summary", ""))
            severity = str(raw_finding.get("severity", "info") or "info")
            evidence = raw_finding.get("evidence") if isinstance(raw_finding.get("evidence"), dict) else {}
            compact = {
                "skill": skill,
                "severity": severity,
                "summary": summary,
                "evidence": _short_evidence(evidence),
            }
            key_evidence.append(compact)
            ts = _first_timestamp(summary, evidence)
            if ts:
                timeline.append({
                    "time": ts,
                    "source": skill,
                    "severity": severity,
                    "event": summary,
                })

    # Include top-level fused findings when a child skill failed or missing logs
    # were injected by the orchestrator; these may not be in child envelopes.
    for f in findings:
        if not f.summary:
            continue
        if f.summary.startswith("[") and any(e.get("summary") == f.summary for e in key_evidence):
            continue
        key_evidence.append({
            "skill": "orchestrator",
            "severity": f.severity,
            "summary": f.summary,
            "evidence": _short_evidence(f.evidence),
        })

    timeline = sorted(timeline, key=lambda item: _timeline_sort_key(item.get("time")))[:40]
    key_evidence = key_evidence[:40]

    critical = [e for e in key_evidence if e.get("severity") == "critical"]
    warnings = [e for e in key_evidence if e.get("severity") == "warning"]
    root_text = " -> ".join(item.get("finding", "") for item in root_chain if item.get("finding")) or None
    direct_cause = (
        critical[0]["summary"] if critical else
        root_text if root_text else
        (warnings[0]["summary"] if warnings else None)
    )

    possible_causes: list[dict[str, Any]] = []
    seen_causes: set[str] = set()
    for p in problems:
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type") or "")
        if not ptype or ptype in seen_causes:
            continue
        seen_causes.add(ptype)
        possible_causes.append({
            "type": ptype,
            "summary": p.get("summary") or p.get("title") or ptype,
            "evidence": _short_evidence(p),
        })
    for e in warnings[:8]:
        text = str(e.get("summary") or "")
        if text and text not in seen_causes:
            seen_causes.add(text)
            possible_causes.append({
                "type": "warning_finding",
                "summary": text,
                "evidence": e.get("evidence") or {},
            })

    key_logs: list[dict[str, Any]] = []
    produces_map = _skill_produces_kind_map()
    for kind, paths in discovery.items():
        label = KIND_LABEL.get(kind, kind)
        consumers = [
            skill for skill, kinds in produces_map.items()
            if label in kinds and skill in executed
        ]
        key_logs.append({
            "kind": label,
            "count": len(paths or []),
            "sample_files": list(paths or [])[:5],
            "consumer_skills": consumers,
        })
    for skill in executed:
        if not any(skill in (entry.get("consumer_skills") or []) for entry in key_logs):
            key_logs.append({
                "kind": None,
                "count": None,
                "sample_files": [],
                "consumer_skills": [skill],
            })

    evidence_chain: list[str] = []
    if key_logs:
        evidence_chain.append(
            "Logs discovered: " + ", ".join(
                f"{entry.get('kind') or 'unknown'}={entry.get('count')}"
                for entry in key_logs if entry.get("count") is not None
            )
        )
    if timeline:
        evidence_chain.append(
            f"Timeline extracted from {len(timeline)} timestamped log/finding event(s)."
        )
    for e in critical[:5]:
        evidence_chain.append(f"Critical evidence: {e.get('summary')}")
    for item in root_chain:
        evidence_chain.append(f"Root-cause signal from {item.get('skill', '?')}: {item.get('finding', '')}")
    if missing_logs:
        evidence_chain.append(
            "Analysis gaps: " + ", ".join(str(m.get("log_kind")) for m in missing_logs)
        )

    return {
        "schema_version": "1.0",
        "summary": {
            "confidence": confidence,
            "executed": list(executed),
            "failed": list(failures),
            "time_range": time_range or {},
            "correlatable": bool((cross_log_context or {}).get("correlatable")),
        },
        "timeline": timeline,
        "key_evidence": key_evidence,
        "evidence_chain": evidence_chain,
        "key_logs": key_logs,
        "direct_cause": direct_cause or "Undetermined from available logs.",
        "root_cause": root_text or "Undetermined from available logs.",
        "possible_causes": possible_causes[:12],
        "repair_suggestions": [_solution_dict(s) for s in solutions],
        "next_steps": [_next_step_dict(n) for n in next_steps],
        "additional_logs_needed": (
            [_log_request_dict(l) for l in additional_logs_needed]
            + [dict(m) for m in missing_logs]
        ),
    }


def _cross_log_context(discovery: dict[str, list[str]] | None,
                       time_range: dict[str, str] | None,
                       results: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Summarise which log kinds were present together so the operator can
    correlate IIS W3C entries with Event Log entries inside the same window."""
    discovery = discovery or {}
    results = results or {}
    has_iis = bool(discovery.get(IIS_KIND) or (results.get("iis_logs") or {}).get("ok"))
    has_evtx = bool(discovery.get(EVTX_KIND) or (results.get("event_log") or {}).get("ok"))
    has_httperr = bool(discovery.get(HTTPERR_KIND) or (results.get("httperror") or {}).get("ok"))
    has_ftp = bool(discovery.get(FTP_KIND) or (results.get("ftp_logs") or {}).get("ok"))
    has_netlog = bool(discovery.get(NETLOG_KIND) or (results.get("netlog") or {}).get("ok"))
    has_procmon = bool(discovery.get(PROCMON_KIND) or (results.get("procmon") or {}).get("ok"))
    available = sorted(k for k, v in {
        "iis_w3c": has_iis, "evtx": has_evtx,
        "http_err": has_httperr, "ftp_w3c": has_ftp,
        "netlog": has_netlog, "procmon": has_procmon,
    }.items() if v)
    netlog_files = ((results.get("netlog") or {}).get("raw") or {}).get("per_file") or []
    netlog_calibrated = bool(netlog_files) and any(
        bool((item.get("time_model") or {}).get("calibrated"))
        for item in netlog_files if isinstance(item, dict)
    )
    procmon_time_comparable = bool(
        (((results.get("procmon") or {}).get("raw") or {}).get("quality") or {}).get(
            "time_comparable"
        )
    )
    pairs: dict[str, dict[str, Any]] = {}
    if has_iis and has_evtx:
        pairs["iis_evtx"] = {
            "quality": "time_only",
            "basis": "shared wall-clock window; no request/PID join proven",
        }
    if has_iis and has_httperr:
        pairs["iis_httperr"] = {
            "quality": "time_only",
            "basis": "shared W3C UTC window; use site/queue fields for manual join",
        }
    if has_iis and has_netlog:
        pairs["iis_netlog"] = {
            "quality": "time_only" if netlog_calibrated else "unavailable",
            "basis": (
                "NetLog event ticks calibrated with timeTickOffset"
                if netlog_calibrated else
                "NetLog timeTickOffset missing; only relative event order is available"
            ),
        }
    if has_iis and has_procmon:
        pairs["iis_procmon"] = {
            "quality": "time_only" if procmon_time_comparable else "unavailable",
            "basis": (
                "Procmon wall-clock timestamps overlap the IIS analysis window; no request/PID join proven"
                if procmon_time_comparable else
                "Procmon time-of-day values lack a trustworthy date anchor"
            ),
        }
    usable_pairs = [
        pair for pair in pairs.values() if pair["quality"] != "unavailable"
    ]
    correlation_quality = "time_only" if usable_pairs else "unavailable"
    notes = []
    if "iis_evtx" in pairs:
        notes.append("IIS W3C and Event Log can be compared by wall-clock window")
    if "iis_httperr" in pairs:
        notes.append("IIS W3C and HTTPERR can be compared by UTC window and site/queue")
    if "iis_netlog" in pairs:
        notes.append(
            "Edge NetLog absolute time is calibrated"
            if netlog_calibrated else
            "Edge NetLog is relative-only and cannot be wall-clock correlated"
        )
    if "iis_procmon" in pairs:
        notes.append(
            "IIS and Procmon can be compared by wall-clock window only"
            if procmon_time_comparable else
            "Procmon is not wall-clock comparable with IIS"
        )
    return {
        "available": available,
        "time_range": time_range or {},
        "correlatable": bool(usable_pairs),
        "correlation_quality": correlation_quality,
        "pairs": pairs,
        "note": "; ".join(notes) + "." if notes else None,
    }


# ---------------------------------------------------------------------------
# Missing-log gate
# ---------------------------------------------------------------------------

# Backward-compatible defaults; preferred source is registry metadata.
DEFAULT_EXTRA_KEY_TO_KIND: dict[str, str] = {
    "netlog_paths":  "netlog",
    "netlog_folder": "netlog",
    "evtx_paths":    "evtx",
    "evtx_folder":   "evtx",
    "procmon_paths": "procmon",
    "procmon_folder": "procmon",
}


def _extra_key_to_kind_map() -> dict[str, str]:
    src = registry().get("extra_keys_to_kind") or {}
    if not isinstance(src, dict) or not src:
        return DEFAULT_EXTRA_KEY_TO_KIND
    out: dict[str, str] = {}
    for key, kind in src.items():
        if key and kind:
            out[str(key)] = str(kind)
    return out or DEFAULT_EXTRA_KEY_TO_KIND


def _expected_log_kinds_for_folder(payload_problems: list[dict[str, Any]],
                                    results: dict[str, dict[str, Any]]
                                    ) -> dict[str, LogRequest]:
    """Build {log_kind -> LogRequest} from playbook (via observed problem
    types) and from every successful child skill's additional_logs_needed."""
    out: dict[str, LogRequest] = {}

    def _add(req: LogRequest) -> None:
        if req.log_kind and req.log_kind not in out:
            out[req.log_kind] = req

    for p in payload_problems or []:
        t = p.get("type") if isinstance(p, dict) else None
        if t:
            for req in playbook.logs_for(t):
                _add(req)

    for env in results.values():
        if not isinstance(env, dict):
            continue
        for l in env.get("additional_logs_needed") or []:
            if not isinstance(l, dict) or not l.get("log_kind"):
                continue
            _add(LogRequest(
                log_kind=str(l.get("log_kind")),
                why=str(l.get("why") or ""),
                how_to_collect=l.get("how_to_collect"),
                skill=l.get("skill"),
            ))

    return out


def _provided_log_kinds_for_folder(discovery: dict[str, list[str]],
                                    results: dict[str, dict[str, Any]],
                                    extra: dict[str, Any] | None = None
                                    ) -> set[str]:
    """Discovery hits + child-skill exercises + caller-supplied extras."""
    provided: set[str] = set()
    produces_map = _skill_produces_kind_map()
    extra_map = _extra_key_to_kind_map()
    for kind, paths in (discovery or {}).items():
        if not paths:
            continue
        label = KIND_LABEL.get(kind)
        if label:
            provided.add(label)
    for sid, env in (results or {}).items():
        if not isinstance(env, dict) or not env.get("ok"):
            continue
        for kind in produces_map.get(sid, ()):
            provided.add(kind)
    for key, kind in extra_map.items():
        if (extra or {}).get(key):
            provided.add(kind)
    return provided


def _compute_missing_logs_folder(payload_problems: list[dict[str, Any]],
                                  discovery: dict[str, list[str]],
                                  results: dict[str, dict[str, Any]],
                                  extra: dict[str, Any] | None = None
                                  ) -> list[dict[str, Any]]:
    expected = _expected_log_kinds_for_folder(payload_problems, results)
    provided = _provided_log_kinds_for_folder(discovery, results, extra)
    missing = sorted(k for k in expected if k not in provided)
    out: list[dict[str, Any]] = []
    for kind in missing:
        req = expected[kind]
        out.append({
            "log_kind": kind,
            "why": req.why,
            "how_to_collect": req.how_to_collect,
            "skill": req.skill,
        })
    return out


def _problem_type_set(problems: list[dict[str, Any]]) -> set[str]:
    return {
        str(problem.get("type"))
        for problem in problems
        if isinstance(problem, dict) and problem.get("type")
    }


def _has_source_roots(extra: dict[str, Any] | None) -> bool:
    extra = extra or {}
    return any(extra.get(key) for key in ("repo_roots", "config_roots", "symbols_roots"))


def _successful_root(results: dict[str, dict[str, Any]]) -> bool:
    return any(
        result.get("ok")
        and result.get("root_cause")
        and result.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND
        for result in results.values()
        if isinstance(result, dict)
    )


def _should_validate(
    problems: list[dict[str, Any]],
    *,
    requested: bool = False,
    skip: bool = False,
) -> bool:
    if skip:
        return False
    config = registry().get("cv_config") or {}
    if not config.get("enabled"):
        return False
    eligible = set(config.get("eligible_problem_types") or [])
    return requested or bool(_problem_type_set(problems) & eligible)


def _run_final_validation(
    *,
    children: dict[str, dict[str, Any]],
    problems: list[dict[str, Any]],
    time_range: dict[str, Any] | None,
    extra: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    extra = extra or {}
    config = registry().get("cv_config") or {}
    context = {
        "problem_type": next(iter(_problem_type_set(problems)), None),
        "problems": problems,
        "time_range": time_range or {},
        "extra": {
            "product": "IIS",
            "children": children,
            "model_analyses": extra.get("model_analyses") or {},
            "incident_mode": extra.get("incident_mode") or "offline_bundle",
            "minimum_independent_skills": config.get("minimum_independent_skills", 2),
        },
    }
    return _run_one("validator", context, timeout)


def _gap_from_dict(value: dict[str, Any]) -> EvidenceGap:
    return EvidenceGap(
        code=str(value.get("code") or "evidence_gap"),
        summary=str(value.get("summary") or "Evidence gap"),
        severity=str(value.get("severity") or "warning"),
        blocking=bool(value.get("blocking", True)),
        log_kind=value.get("log_kind"),
        skill=value.get("skill"),
        details=dict(value.get("details") or {}),
    )


def orchestrate(iis_payload: dict[str, Any],
                per_skill_timeout: int = DEFAULT_PER_SKILL_TIMEOUT,
                total_timeout: int = DEFAULT_TOTAL_TIMEOUT) -> SkillResult:
    requested_follow_ups: list[str] = list(iis_payload.get("skills_to_trigger", []))
    validator_requested = "validator" in requested_follow_ups
    follow_ups = [skill for skill in requested_follow_ups if skill != "validator"]
    problems: list[dict[str, Any]] = iis_payload.get("problems", [])
    extra = iis_payload.get("extra") or {}
    if (extra.get("procmon_paths") or extra.get("procmon_folder")) and "procmon" not in follow_ups:
        follow_ups.append("procmon")

    if not follow_ups and not _has_source_roots(extra) and not validator_requested:
        return SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(summary="Nothing to orchestrate", severity="info")],
            recommendations=["No follow-up skills were requested."],
            evidence_state=EvidenceState.INCONCLUSIVE_EVIDENCE,
            raw={"executed": []},
        )

    sub_results: dict[str, dict[str, Any]] = {}
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {
            pool.submit(_run_one, skill,
                        _build_orchestrator_subcontext(
                            iis_payload, skill, problems, per_skill_timeout
                        ),
                        per_skill_timeout): skill
            for skill in follow_ups
        }
        try:
            for fut in as_completed(futures, timeout=total_timeout):
                skill = futures[fut]
                try:
                    sub_results[skill] = fut.result()
                except Exception as exc:
                    sub_results[skill] = operational_failure(
                        skill,
                        f"child execution raised {type(exc).__name__}",
                        code="child_execution_failed",
                        execution={"stage": "execute"},
                    )
        except FuturesTimeoutError:
            for fut, skill in futures.items():
                if skill not in sub_results:
                    fut.cancel()
                    sub_results[skill] = operational_failure(
                        skill,
                        f"total orchestration timeout after {total_timeout}s",
                        code="orchestration_timeout",
                        execution={
                            "stage": "orchestrate",
                            "timeout_seconds": total_timeout,
                        },
                    )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    executed_skills = list(follow_ups)
    if _has_source_roots(extra) and not _successful_root(sub_results):
        source_context = {
            "problem_type": next(iter(_problem_type_set(problems)), None),
            "problems": problems,
            "time_range": (iis_payload.get("metrics") or {}).get("time_range") or {},
            "extra": {**extra, "product": "IIS"},
        }
        sub_results["source_logging_discovery"] = _run_one(
            "source_logging_discovery", source_context, per_skill_timeout
        )
        executed_skills.append("source_logging_discovery")

    validation_result: dict[str, Any] | None = None
    if _should_validate(
        problems,
        requested=validator_requested,
        skip=bool(extra.get("skip_validation")),
    ) and sub_results:
        validation_result = _run_final_validation(
            children=sub_results,
            problems=problems,
            time_range=(iis_payload.get("metrics") or {}).get("time_range") or {},
            extra=extra,
            timeout=per_skill_timeout,
        )

    successes = [s for s, r in sub_results.items() if r.get("ok")]
    failures = [s for s, r in sub_results.items() if not r.get("ok")]

    # Build a fused finding list
    findings: list[Finding] = []
    root_chain: list[dict[str, str]] = []
    for skill, res in sub_results.items():
        if not res.get("ok"):
            continue
        for f in res.get("findings", []):
            findings.append(Finding(
                summary=f"[{skill}] {f.get('summary', '')}",
                severity=f.get("severity", "info"),
                evidence=f.get("evidence", {}),
            ))
        if (res.get("root_cause") and res.get("evidence_state")
            == EvidenceState.ROOT_CAUSE_FOUND):
            root_chain.append({"skill": skill, "finding": res["root_cause"]})

    if validation_result:
        for finding in validation_result.get("findings") or []:
            findings.append(Finding(
                summary=f"[validator] {finding.get('summary', '')}",
                severity=finding.get("severity", "info"),
                evidence=finding.get("evidence", {}),
            ))
        if (validation_result.get("root_cause")
            and validation_result.get("evidence_state")
            == EvidenceState.ROOT_CAUSE_FOUND):
            root_chain.insert(0, {
                "skill": "validator",
                "finding": validation_result["root_cause"],
            })

    success_rate = len(successes) / max(1, len(executed_skills))
    confidence = "high" if success_rate >= 0.8 else "medium" if success_rate >= 0.5 else "low"
    validation_status = (
        (validation_result.get("cross_validation") or {}).get("status")
        if validation_result else None
    )
    if validation_status == "conflict":
        confidence = "low"
    elif (validation_result and validation_result.get("root_cause")
          and validation_result.get("evidence_state")
          == EvidenceState.ROOT_CAUSE_FOUND):
        confidence = validation_result.get("confidence", "low")

    advisory_results = dict(sub_results)
    if validation_result:
        advisory_results["validator"] = validation_result
    agg_sols, agg_nxts, agg_logs = _aggregate_advisories(advisory_results)
    cross_ctx = _cross_log_context(None, iis_payload.get("metrics", {}).get("time_range"), sub_results)
    missing_logs = _compute_missing_logs_folder(
        payload_problems=problems,
        discovery={},
        results=advisory_results,
        extra=extra,
    )
    evidence_gaps = gaps_from_missing(missing_logs)
    for skill in failures:
        evidence_gaps.append(EvidenceGap(
            code="skill_failure",
            summary=f"{skill} failed: {sub_results[skill].get('error') or 'unknown error'}",
            skill=skill,
        ))
    if validation_result:
        if not validation_result.get("ok"):
            evidence_gaps.append(EvidenceGap(
                code="validator_failure",
                summary=(
                    "Final validator failed: "
                    f"{validation_result.get('error') or 'unknown error'}"
                ),
                skill="validator",
                blocking=True,
            ))
        evidence_gaps.extend(
            _gap_from_dict(gap) for gap in validation_result.get("evidence_gaps") or []
            if isinstance(gap, dict)
        )
    collection_inputs: list[object] = [*agg_logs, *missing_logs]
    collection_plan = build_collection_plan(
        collection_inputs,
        mode=str(extra.get("incident_mode") or "offline_bundle"),
        time_range=(iis_payload.get("metrics") or {}).get("time_range") or {},
        products=["IIS"],
    ) if collection_inputs else None
    root_cause = " → ".join(item["finding"] for item in root_chain) or None
    if root_cause is None:
        confidence = "low"
    if not root_cause and not _has_source_roots(extra):
        source_action = "Run source-guided logging discovery with explicit repo/config roots"
        if not any(step.action == source_action for step in agg_nxts):
            agg_nxts.append(NextStep(
                action=source_action,
                why=("Existing evidence did not establish a root cause; inspect local "
                     "logging declarations without executing target code."),
                skill="source_logging_discovery",
            ))
        evidence_gaps.append(EvidenceGap(
            code="source_guidance_not_run",
            summary="No explicit repo/config roots were supplied for source-guided logging discovery.",
            blocking=False,
            skill="source_logging_discovery",
        ))
    time_not_comparable = any(
        pair.get("quality") == "unavailable"
        for pair in (cross_ctx.get("pairs") or {}).values()
        if isinstance(pair, dict)
    )
    quality_inputs = list(sub_results.values())
    if validation_result:
        quality_inputs.append(validation_result)
    root_quality = aggregate_root_quality(quality_inputs)
    root_quality["blocking_gaps"] = (
        root_quality["blocking_gaps"]
        or any(gap.blocking for gap in evidence_gaps)
    )
    evidence_state = derive_evidence_state(
        root_cause=root_cause,
        confidence=confidence,
        direct_join=root_quality["direct_join"],
        independent_sources=root_quality["independent_sources"],
        blocking_gaps=root_quality["blocking_gaps"],
        sample_sufficient=root_quality["sample_sufficient"],
        capture_complete=root_quality["capture_complete"],
        missing_required=bool(missing_logs),
        time_not_comparable=(
            time_not_comparable
            or (bool(root_cause) and not root_quality["time_comparable"])
        ),
    )
    if validation_status == "conflict":
        evidence_state = EvidenceState.INCONCLUSIVE_EVIDENCE
    elif not root_cause and collection_plan and collection_plan.requests:
        evidence_state = EvidenceState.COLLECTION_PLAN_READY
    diagnosis = _build_diagnosis(
        results=sub_results,
        findings=findings,
        root_chain=root_chain,
        problems=problems,
        discovery=None,
        error_block=None,
        executed=executed_skills,
        failures=failures,
        confidence=confidence,
        time_range=iis_payload.get("metrics", {}).get("time_range"),
        cross_log_context=cross_ctx,
        solutions=agg_sols,
        next_steps=agg_nxts,
        additional_logs_needed=agg_logs,
        missing_logs=missing_logs,
    )

    return SkillResult(
        skill=SKILL_ID,
        ok=(
            len(failures) == 0
            and (validation_result is None or bool(validation_result.get("ok")))
        ),
        findings=findings,
        root_cause=root_cause,
        confidence=confidence,
        recommendations=[
            f"Run rate: {len(successes)}/{len(executed_skills)} skills succeeded.",
            *([f"Re-run failed skills: {', '.join(failures)}"] if failures else []),
        ],
        solutions=agg_sols,
        next_steps=agg_nxts,
        additional_logs_needed=agg_logs,
        cross_validation=(validation_result or {}).get("cross_validation"),
        evidence_state=evidence_state,
        evidence_gaps=evidence_gaps,
        collection_plan=collection_plan,
        raw={
            "executed": executed_skills,
            "results": sub_results,
            "validation": validation_result,
            "quality": root_quality,
            "missing_logs": missing_logs,
            "runtimes": {
                skill: ((result.get("raw") or {}).get("execution") or {}).get("runtime")
                for skill, result in sub_results.items()
            },
            "root_cause_chain": root_chain,
            "cross_log_context": cross_ctx,
            "diagnosis": diagnosis,
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Multi-skill orchestrator (folder-first)")
    ap.add_argument("context", nargs="?", default=None,
                    help="Folder path, JSON string, @file, .json path, or omit for stdin")
    ap.add_argument("--per-skill-timeout", type=int, default=DEFAULT_PER_SKILL_TIMEOUT)
    ap.add_argument("--total-timeout", type=int, default=DEFAULT_TOTAL_TIMEOUT)
    # Folder-mode-only switches (ignored in legacy mode).
    ap.add_argument("--around", action="append", default=[], metavar="TIMESTAMP",
                    help="Anchor timestamp passed through to iis_logs/ftp_logs. Repeatable.")
    ap.add_argument("--window", default=DEFAULT_WINDOW,
                    help="Window around each anchor (e.g. 30s, 5m, 1h). Default 5m.")
    ap.add_argument("--error", default=None, metavar="REGEX",
                    help="Search every text log under the folder for REGEX. "
                         "Matching lines' timestamps become extra --around anchors.")
    ap.add_argument("--no-recursive", action="store_true",
                    help="Folder mode: do not descend into subdirectories.")
    ap.add_argument("--allow-live-host", action="store_true",
                    help=("Folder mode: allow live Event Log/perf/security/firewall "
                          "queries on this host. Off by default for incident bundles."))
    ap.add_argument("--procmon-exe", default=None, metavar="PATH",
                    help=("Trusted Procmon executable used only to export discovered PML files. "
                          "The path is inherited by the child and is never accepted from incident JSON."))
    ap.add_argument("--procmon-time-basis", choices=("unknown", "context"), default=None,
                    help=("Use 'context' only when Procmon CSV timestamps use the same clock "
                          "basis as the orchestrator's time_range; default is unknown."))
    ap.add_argument("--source-root", action="append", default=[], metavar="PATH",
                    help="Explicit source root for read-only logging discovery. Repeatable.")
    ap.add_argument("--config-root", action="append", default=[], metavar="PATH",
                    help="Explicit config root for read-only logging discovery. Repeatable.")
    ap.add_argument("--symbols-root", action="append", default=[], metavar="PATH",
                    help="Explicit symbol/text-map root for read-only logging discovery. Repeatable.")
    ap.add_argument("--report", default=None, metavar="HTML_PATH",
                    help="Folder mode: write an HTML summary to this path.")
    ap.add_argument("--agent-summary", default=None, metavar="PATH",
                    help=("Folder mode: read a Markdown or HTML file holding the "
                          "agent's (Copilot CLI) consolidated diagnosis / solution "
                          "and embed it as a featured section at the top of the "
                          "HTML report."))
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="Suppress the human-readable progress trace on stderr.")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Force the progress trace on stderr (default in folder mode).")
    args = ap.parse_args(argv)

    if args.procmon_exe:
        os.environ["SKILLS_PROCMON_EXE"] = str(Path(args.procmon_exe).resolve())
    if args.procmon_time_basis:
        os.environ["SKILLS_PROCMON_TIME_BASIS"] = args.procmon_time_basis

    global _VERBOSE
    if args.quiet:
        _VERBOSE = False
    elif args.verbose:
        _VERBOSE = True

    # --- Mode arbitration -------------------------------------------------
    arg = args.context
    if arg and not arg.startswith("{") and not arg.startswith("@") \
            and not arg.lower().endswith(".json"):
        candidate = Path(arg)
        if candidate.exists() and candidate.is_dir():
            result = orchestrate_folder(
                folder=candidate,
                around=list(args.around),
                window=args.window,
                error_pattern=args.error,
                recursive=not args.no_recursive,
                per_skill_timeout=args.per_skill_timeout,
                total_timeout=args.total_timeout,
                report_path=Path(args.report) if args.report else None,
                agent_summary_path=(Path(args.agent_summary)
                                    if args.agent_summary else None),
                allow_live_host=args.allow_live_host,
                source_roots=list(args.source_root),
                config_roots=list(args.config_root),
                symbols_roots=list(args.symbols_root),
            )
            # Lift raw.executed_summary to a top-level field so agents that
            # only display the envelope's top keys still surface which
            # sub-skills actually ran.
            envelope = json.loads(result.to_json())
            envelope["executed_summary"] = (
                result.raw.get("executed_summary") if isinstance(result.raw, dict) else None
            )
            print(json.dumps(envelope, indent=2, ensure_ascii=True))
            return 0 if result.ok else 1

    # --- Legacy pipe mode (unchanged) -------------------------------------
    if args.context is None and not sys.stdin.isatty():
        payload = json.loads(sys.stdin.read())
    else:
        payload = load_context([args.context] if args.context else [])

    # Accept both the IIS analyzer's full envelope and its `raw` sub-block
    if isinstance(payload, dict) and "raw" in payload and "skills_to_trigger" in payload.get("raw", {}):
        payload = payload["raw"]

    # Auto-promote a folder-mode JSON mistake into real folder mode.
    # Agents sometimes pass `{"extra": {"folder": "<path>"}}` thinking that is
    # how folder mode is invoked. Legacy pipe mode would silently return
    # "Nothing to orchestrate" because `skills_to_trigger` is empty.
    if (isinstance(payload, dict)
            and not payload.get("skills_to_trigger")
            and isinstance(payload.get("extra"), dict)):
        extra = payload["extra"]
        folder_hint = extra.get("folder") or extra.get("folder_path") or extra.get("path")
        if folder_hint:
            candidate = Path(str(folder_hint))
            if candidate.exists() and candidate.is_dir():
                _log(f"[orchestrator] auto-promoting JSON 'extra.folder' "
                     f"to folder mode: {candidate}")
                result = orchestrate_folder(
                    folder=candidate,
                    around=list(args.around),
                    window=args.window,
                    error_pattern=args.error,
                    recursive=not args.no_recursive,
                    per_skill_timeout=args.per_skill_timeout,
                    total_timeout=args.total_timeout,
                    report_path=Path(args.report) if args.report else None,
                    agent_summary_path=(Path(args.agent_summary)
                                        if args.agent_summary else None),
                    allow_live_host=args.allow_live_host,
                    source_roots=list(args.source_root),
                    config_roots=list(args.config_root),
                    symbols_roots=list(args.symbols_root),
                )
                envelope = json.loads(result.to_json())
                envelope["executed_summary"] = (
                    result.raw.get("executed_summary") if isinstance(result.raw, dict) else None
                )
                print(json.dumps(envelope, indent=2, ensure_ascii=True))
                return 0 if result.ok else 1

    result = orchestrate(payload, args.per_skill_timeout, args.total_timeout)
    result.emit()
    return 0 if result.ok else 1


# =========================================================================
# Folder-mode helpers
# =========================================================================

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr)?\s*$",
                          re.IGNORECASE)


def _parse_duration_seconds(text: str) -> int:
    m = _DURATION_RE.match(text or "")
    if not m:
        raise ValueError(f"invalid duration: {text!r}")
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit.startswith("s"):
        return int(n)
    if unit.startswith("m"):
        return int(n * 60)
    if unit.startswith("h"):
        return int(n * 3600)
    raise ValueError(f"invalid duration: {text!r}")


def _parse_anchor(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        s = text.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def _run_cli_skill(skill_id: str, cli_args: list[str], timeout: int) -> dict[str, Any]:
    """Invoke a skill via its CLI (positional file/folder), capture JSON."""
    try:
        entry = skill_entry(skill_id, "python")
    except KeyError as exc:
        return {"skill": skill_id, "ok": False, "error": str(exc)}
    cmd = [sys.executable, str(entry), *cli_args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"skill": skill_id, "ok": False,
                "error": f"timed out after {timeout}s", "cmd": cmd}
    return _parse_skill_process(skill_id, "python", cmd, proc)


def _run_json_skill(skill_id: str, ctx: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Invoke a skill with a JSON context as its first positional arg."""
    return _run_one(skill_id, ctx, timeout)


def _bounding_time_range(anchors: list[datetime], window_seconds: int
                          ) -> dict[str, str] | None:
    if not anchors:
        return None
    half = timedelta(seconds=window_seconds)
    lo = min(anchors) - half
    hi = max(anchors) + half
    return {"start": lo.strftime("%Y-%m-%dT%H:%M:%S"),
            "end":   hi.strftime("%Y-%m-%dT%H:%M:%S")}


def orchestrate_folder(folder: Path,
                       around: list[str],
                       window: str,
                       error_pattern: str | None,
                       recursive: bool,
                       per_skill_timeout: int,
                       total_timeout: int,
                       report_path: Path | None,
                       agent_summary_path: Path | None = None,
                       allow_live_host: bool = False,
                       source_roots: list[str] | None = None,
                       config_roots: list[str] | None = None,
                       symbols_roots: list[str] | None = None) -> SkillResult:
    """Folder-first entry point. See module docstring for the high-level flow."""
    # --- Parse anchors and window ----------------------------------------
    try:
        window_seconds = _parse_duration_seconds(window)
    except ValueError as exc:
        return SkillResult(skill=SKILL_ID, ok=False,
                           error=f"--window: {exc}", confidence="low")

    user_anchors: list[datetime] = []
    bad_anchors: list[str] = []
    for raw in around:
        ts = _parse_anchor(raw)
        if ts:
            user_anchors.append(ts)
        else:
            bad_anchors.append(raw)

    # --- Discover logs ---------------------------------------------------
    disc = discover_logs(folder, recursive=recursive)
    discovery_summary: dict[str, list[str]] = {
        kind: [str(p) for p in files]
        for kind, files in disc.by_kind.items()
    }

    iis_files     = disc.by_kind.get(IIS_KIND, [])
    ftp_files     = disc.by_kind.get(FTP_KIND, [])
    httperr_files = disc.by_kind.get(HTTPERR_KIND, [])
    evtx_files    = disc.by_kind.get(EVTX_KIND, [])
    netlog_files  = disc.by_kind.get(NETLOG_KIND, [])
    procmon_files = disc.by_kind.get(PROCMON_KIND, [])
    text_files    = [p for kind, lst in disc.by_kind.items()
                     if kind not in (EVTX_KIND, NETLOG_KIND, PROCMON_KIND) for p in lst]

    _log("=" * 72)
    _log(f"IIS Orchestrator — folder mode")
    _log(f"  folder    : {folder}")
    _log(f"  recursive : {recursive}")
    _log(f"  live host : {'enabled' if allow_live_host else 'disabled'}")
    source_roots = list(source_roots or [])
    config_roots = list(config_roots or [])
    symbols_roots = list(symbols_roots or [])
    if source_roots or config_roots or symbols_roots:
        _log(f"  source scan: {len(source_roots) + len(config_roots) + len(symbols_roots)} explicit root(s)")
    _log(f"  window    : {window}  ({window_seconds}s)")
    if user_anchors:
        _log(f"  anchors   : {len(user_anchors)} user-supplied")
    _log("")
    _log("Discovered logs:")
    if discovery_summary:
        for kind, paths in discovery_summary.items():
            label = KIND_LABEL.get(kind, kind)
            _log(f"  - {label:<10} ({len(paths)} file(s))")
            for p in paths[:5]:
                _log(f"      {p}")
            if len(paths) > 5:
                _log(f"      ... ({len(paths) - 5} more)")
    else:
        _log("  (none)")

    # --- Error locator ---------------------------------------------------
    error_block: dict[str, Any] = {}
    error_anchors: list[datetime] = []
    if error_pattern:
        try:
            hits = find_error_anchors(text_files, error_pattern,
                                      max_hits=DEFAULT_ERROR_MAX_HITS)
        except ValueError as exc:
            return SkillResult(skill=SKILL_ID, ok=False,
                               error=str(exc), confidence="low")
        error_anchors = dedup_anchors(hits, min_gap_seconds=60)
        error_block = {
            "pattern": error_pattern,
            "hits": [h.to_dict() for h in hits],
            "derived_anchors": [a.strftime("%Y-%m-%d %H:%M:%S") for a in error_anchors],
        }

    all_anchors = sorted(set(user_anchors) | set(error_anchors))
    time_range = _bounding_time_range(all_anchors, window_seconds)
    around_cli = [a.strftime("%Y-%m-%d %H:%M:%S") for a in all_anchors]

    # --- Dispatch entry skills ------------------------------------------
    executed: list[str] = []
    results: dict[str, dict[str, Any]] = {}

    _log("")
    if error_block:
        _log(f"Error locator: pattern={error_pattern!r}  "
             f"hits={len(error_block.get('hits', []))}  "
             f"derived_anchors={len(error_anchors)}")
    if time_range:
        _log(f"Effective time_range: {time_range['start']} .. {time_range['end']}")
    _log("")
    _log("Dispatching entry skills...")

    def _maybe_add_around(args_list: list[str]) -> list[str]:
        out = list(args_list)
        for a in around_cli:
            out.extend(["--around", a])
        if all_anchors:
            out.extend(["--window", window])
        if not recursive:
            out.append("--no-recursive")
        return out

    if iis_files:
        _log(f">>> running skill: iis_logs ({len(iis_files)} IIS log file(s))")
        executed.append("iis_logs")
        results["iis_logs"] = _run_cli_skill(
            "iis_logs", _maybe_add_around([str(folder)]),
            timeout=per_skill_timeout,
        )
        _print_skill_block("iis_logs", results["iis_logs"])

    if ftp_files:
        _log(f">>> running skill: ftp_logs ({len(ftp_files)} FTP log file(s))")
        executed.append("ftp_logs")
        results["ftp_logs"] = _run_cli_skill(
            "ftp_logs", _maybe_add_around([str(folder)]),
            timeout=per_skill_timeout,
        )
        _print_skill_block("ftp_logs", results["ftp_logs"])

    if httperr_files:
        ctx: dict[str, Any] = {
            "problem_type": None,
            "extra": {"folder": str(folder), "no_recursive": not recursive},
        }
        if time_range:
            ctx["time_range"] = time_range
        _log(f">>> running skill: httperror ({len(httperr_files)} HTTPERR file(s))")
        executed.append("httperror")
        results["httperror"] = _run_json_skill("httperror", ctx, per_skill_timeout)
        _print_skill_block("httperror", results["httperror"])

    # event_log: run if we have evtx files (offline) OR a time_range
    # (live mode is only meaningful when a time window is known).
    if evtx_files or (time_range and allow_live_host):
        ctx = {"problem_type": None, "extra": {}}
        if evtx_files:
            ctx["extra"]["evtx_paths"] = [str(p) for p in evtx_files]
        if time_range:
            ctx["time_range"] = time_range
        mode_desc = (f"{len(evtx_files)} evtx file(s)" if evtx_files
                     else "live mode (time_range)")
        _log(f">>> running skill: event_log ({mode_desc})")
        executed.append("event_log")
        results["event_log"] = _run_json_skill("event_log", ctx, per_skill_timeout)
        _print_skill_block("event_log", results["event_log"])

    # netlog: run if any net-export JSON files were discovered.
    if netlog_files:
        ctx = {
            "problem_type": None,
            "extra": {"netlog_paths": [str(p) for p in netlog_files]},
        }
        if time_range:
            ctx["time_range"] = time_range
        _log(f">>> running skill: netlog ({len(netlog_files)} net-export file(s))")
        executed.append("netlog")
        results["netlog"] = _run_json_skill("netlog", ctx, per_skill_timeout)
        _print_skill_block("netlog", results["netlog"])

    # procmon: analyze only when a structurally valid CSV/PML source is present.
    if procmon_files:
        ctx = {
            "problem_type": None,
            "extra": {
                "procmon_paths": [str(path) for path in procmon_files],
                "deadline_seconds": max(1, per_skill_timeout - 10),
            },
        }
        if time_range:
            ctx["time_range"] = time_range
        _log(f">>> running skill: procmon ({len(procmon_files)} Procmon file(s))")
        executed.append("procmon")
        results["procmon"] = _run_json_skill("procmon", ctx, per_skill_timeout)
        _print_skill_block("procmon", results["procmon"])

    # --- Secondary fan-out (re-use legacy orchestrate) -------------------
    fanout_calls: list[str] = []
    skipped_followups: list[dict[str, Any]] = []
    seen: set[str] = set(executed)
    available_kinds = _provided_log_kinds_for_folder(
        discovery_summary, results, extra=None
    )
    skill_registry = registry().get("skills") or {}
    for res in results.values():
        if not isinstance(res, dict) or not res.get("ok"):
            continue
        raw = res.get("raw") or {}
        for s in raw.get("skills_to_trigger", []) or []:
            if s in seen:
                continue
            seen.add(s)
            skill_meta = skill_registry.get(s) or {}
            if not allow_live_host and skill_meta.get("folder_dispatch") == "live_only":
                skipped_followups.append({
                    "skill": s,
                    "reason": "live-host access disabled for incident-folder analysis",
                    "required_kinds": [],
                })
                continue
            required_kinds = set(skill_meta.get("folder_evidence_kinds") or [])
            if (not allow_live_host and required_kinds
                    and not (required_kinds & available_kinds)):
                skipped_followups.append({
                    "skill": s,
                    "reason": "required offline evidence is not present",
                    "required_kinds": sorted(required_kinds),
                })
                continue
            fanout_calls.append(s)

    if skipped_followups:
        _log("")
        _log("Skipped follow-ups (offline-safe mode):")
        for skipped in skipped_followups:
            required = ", ".join(skipped["required_kinds"]) or "live host"
            _log(f"  - {skipped['skill']}: {skipped['reason']} ({required})")

    secondary: dict[str, Any] | None = None
    if fanout_calls:
        _log("")
        _log(f"Secondary fan-out: {', '.join(fanout_calls)}")
        synthetic_payload = {
            "metrics": {"time_range": time_range or {}},
            "problems": _aggregate_problems(results),
            "skills_to_trigger": fanout_calls,
            "extra": {
                "skip_validation": True,
                "incident_mode": "live_host" if allow_live_host else "offline_bundle",
            },
        }
        secondary_result = orchestrate(synthetic_payload,
                                        per_skill_timeout=per_skill_timeout,
                                        total_timeout=total_timeout)
        secondary = json.loads(secondary_result.to_json())
        assert secondary is not None
        _print_skill_block("secondary", secondary)

    evidence_children: dict[str, dict[str, Any]] = dict(results)
    if secondary:
        for skill, child in ((secondary.get("raw") or {}).get("results") or {}).items():
            if isinstance(child, dict):
                evidence_children.setdefault(skill, child)

    source_result: dict[str, Any] | None = None
    if (source_roots or config_roots or symbols_roots) and not _successful_root(evidence_children):
        _log("")
        _log(">>> running skill: source_logging_discovery (explicit read-only roots)")
        source_context = {
            "problem_type": None,
            "problems": _aggregate_problems(evidence_children),
            "time_range": time_range or {},
            "extra": {
                "product": "IIS",
                "incident_mode": "live_host" if allow_live_host else "offline_bundle",
                "repo_roots": source_roots,
                "config_roots": config_roots,
                "symbols_roots": symbols_roots,
            },
        }
        source_result = _run_json_skill(
            "source_logging_discovery", source_context, per_skill_timeout
        )
        results["source_logging_discovery"] = source_result
        evidence_children["source_logging_discovery"] = source_result
        executed.append("source_logging_discovery")
        _print_skill_block("source_logging_discovery", source_result)

    validation_result: dict[str, Any] | None = None
    validation_problems = _aggregate_problems(evidence_children)
    if _should_validate(validation_problems) and evidence_children:
        _log("")
        _log(">>> running final reducer: validator")
        validation_result = _run_final_validation(
            children=evidence_children,
            problems=validation_problems,
            time_range=time_range or {},
            extra={
                "incident_mode": "live_host" if allow_live_host else "offline_bundle",
            },
            timeout=per_skill_timeout,
        )
        _print_skill_block("validator", validation_result)

    # --- Fuse findings ---------------------------------------------------
    findings: list[Finding] = []
    root_chain: list[dict[str, str]] = []
    failures: list[str] = []

    for skill, res in results.items():
        if not isinstance(res, dict) or not res.get("ok"):
            failures.append(skill)
            findings.append(Finding(
                summary=f"[{skill}] skill failed: {res.get('error', 'unknown error')}"
                        if isinstance(res, dict) else f"[{skill}] invalid result",
                severity="warning",
            ))
            continue
        for f in res.get("findings", [])[:5]:
            findings.append(Finding(
                summary=f"[{skill}] {f.get('summary', '')}",
                severity=f.get("severity", "info"),
                evidence=f.get("evidence", {}),
            ))
        if (res.get("root_cause") and res.get("evidence_state")
            == EvidenceState.ROOT_CAUSE_FOUND):
            root_chain.append({"skill": skill, "finding": res["root_cause"]})

    if secondary and secondary.get("ok"):
        for f in secondary.get("findings", [])[:5]:
            findings.append(Finding(
                summary=f"[secondary:{f.get('summary', '')}]",
                severity=f.get("severity", "info"),
                evidence=f.get("evidence", {}),
            ))
        for item in (secondary.get("raw") or {}).get("root_cause_chain", []):
            root_chain.append(item)
    elif secondary:
        secondary_results = (secondary.get("raw") or {}).get("results") or {}
        failed_secondary = [
            skill for skill, result in secondary_results.items()
            if not isinstance(result, dict) or not result.get("ok")
        ]
        if not failed_secondary:
            failed_secondary = ["fanout"]
        failures.extend(f"secondary:{skill}" for skill in failed_secondary)
        findings.append(Finding(
            summary=("[secondary] follow-up analysis failed: "
                     f"{', '.join(failed_secondary)}"),
            severity="warning",
            evidence={"failed_skills": failed_secondary},
        ))

    if validation_result:
        for finding in validation_result.get("findings") or []:
            findings.append(Finding(
                summary=f"[validator] {finding.get('summary', '')}",
                severity=finding.get("severity", "info"),
                evidence=finding.get("evidence", {}),
            ))
        if (validation_result.get("root_cause")
            and validation_result.get("evidence_state")
            == EvidenceState.ROOT_CAUSE_FOUND):
            root_chain.insert(0, {
                "skill": "validator",
                "finding": validation_result["root_cause"],
            })

    if not executed:
        findings.insert(0, Finding(
            summary=f"No recognisable IIS/FTP/HTTPERR/EVTX logs found under {folder}",
            severity="info",
            evidence={"discovery": discovery_summary},
        ))

    if bad_anchors:
        findings.append(Finding(
            summary=f"Ignored {len(bad_anchors)} unparseable --around value(s)",
            severity="warning",
            evidence={"unparseable": bad_anchors},
        ))

    # --- Confidence ------------------------------------------------------
    if not executed:
        confidence = "low"
    else:
        ok_count = sum(1 for s in executed if isinstance(results.get(s), dict)
                       and results[s].get("ok"))
        rate = ok_count / len(executed)
        confidence = "high" if rate >= 0.8 else "medium" if rate >= 0.5 else "low"
    if secondary and not secondary.get("ok"):
        confidence = "low"
    validation_status = (
        (validation_result.get("cross_validation") or {}).get("status")
        if validation_result else None
    )
    if validation_status == "conflict":
        confidence = "low"
    elif (validation_result and validation_result.get("root_cause")
          and validation_result.get("evidence_state")
          == EvidenceState.ROOT_CAUSE_FOUND):
        confidence = validation_result.get("confidence", "low")

    # --- Optional HTML report -------------------------------------------
    # Aggregate advisories from every child result (entry + secondary).
    combined_results: dict[str, dict[str, Any]] = dict(results)
    if secondary:
        combined_results["_secondary"] = secondary
    if validation_result:
        combined_results["validator"] = validation_result
    agg_sols, agg_nxts, agg_logs = _aggregate_advisories(combined_results)
    cross_ctx = _cross_log_context(discovery_summary, time_range, evidence_children)

    # --- Missing-log gate ----------------------------------------------
    # Compute required log_kinds (from playbook via observed problems, plus
    # every child skill's additional_logs_needed) minus the kinds we
    # actually have (discovery + successful child runs).
    aggregated_problems = _aggregate_problems(results)
    if secondary and secondary.get("ok"):
        for p in (secondary.get("raw") or {}).get("problems", []) or []:
            if isinstance(p, dict):
                aggregated_problems.append(p)
    missing_logs = _compute_missing_logs_folder(
        payload_problems=aggregated_problems,
        discovery=discovery_summary,
        results=combined_results,
        extra=None,  # folder-mode does not yet carry caller-supplied extras
    )
    for m in missing_logs:
        findings.append(Finding(
            summary=(f"Required log '{m['log_kind']}' is missing — "
                     f"analysis may be incomplete."),
            severity="warning",
            evidence={
                "log_kind": m["log_kind"],
                "why": m.get("why"),
                "how_to_collect": m.get("how_to_collect"),
                "consumer_skill": m.get("skill"),
            },
        ))

    evidence_gaps = gaps_from_missing(missing_logs)
    for skipped in skipped_followups:
        evidence_gaps.append(EvidenceGap(
            code="live_collection_required",
            summary=f"{skipped['skill']}: {skipped['reason']}",
            skill=skipped["skill"],
            blocking=True,
            details={"required_kinds": skipped["required_kinds"]},
        ))
    unknown_files = discovery_summary.get(UNKNOWN_KIND) or []
    if unknown_files:
        evidence_gaps.append(EvidenceGap(
            code="unknown_log_type",
            summary=f"{len(unknown_files)} discovered file(s) could not be classified.",
            blocking=False,
            details={"sample_names": [Path(path).name for path in unknown_files[:10]]},
        ))
    if source_result:
        evidence_gaps.extend(
            _gap_from_dict(gap) for gap in source_result.get("evidence_gaps") or []
            if isinstance(gap, dict)
        )
    if validation_result:
        if not validation_result.get("ok"):
            if "validator" not in failures:
                failures.append("validator")
            evidence_gaps.append(EvidenceGap(
                code="validator_failure",
                summary=(
                    "Final validator failed: "
                    f"{validation_result.get('error') or 'unknown error'}"
                ),
                skill="validator",
                blocking=True,
            ))
            findings.append(Finding(
                summary=(
                    "[validator] final validation failed: "
                    f"{validation_result.get('error') or 'unknown error'}"
                ),
                severity="warning",
                evidence={"skill": "validator"},
            ))
        evidence_gaps.extend(
            _gap_from_dict(gap) for gap in validation_result.get("evidence_gaps") or []
            if isinstance(gap, dict)
        )
    collection_inputs: list[object] = [*agg_logs, *missing_logs]
    collection_plan = build_collection_plan(
        collection_inputs,
        mode="live_host" if allow_live_host else "offline_bundle",
        time_range=time_range or {},
        products=["IIS", "Edge"] if netlog_files else ["IIS"],
    ) if collection_inputs else None
    root_cause_text = " → ".join(item["finding"] for item in root_chain) or None
    if root_cause_text is None:
        confidence = "low"
    time_not_comparable = any(
        pair.get("quality") == "unavailable"
        for pair in (cross_ctx.get("pairs") or {}).values()
        if isinstance(pair, dict)
    )
    root_quality = aggregate_root_quality(combined_results.values())
    root_quality["blocking_gaps"] = (
        root_quality["blocking_gaps"]
        or any(gap.blocking for gap in evidence_gaps)
    )
    evidence_state = derive_evidence_state(
        root_cause=root_cause_text,
        confidence=confidence,
        direct_join=root_quality["direct_join"],
        independent_sources=root_quality["independent_sources"],
        blocking_gaps=root_quality["blocking_gaps"],
        sample_sufficient=root_quality["sample_sufficient"],
        capture_complete=root_quality["capture_complete"],
        missing_required=bool(missing_logs),
        time_not_comparable=(
            time_not_comparable
            or (bool(root_cause_text) and not root_quality["time_comparable"])
        ),
        unknown_log_type=bool(unknown_files),
        live_collection_required=bool(skipped_followups),
    )
    if validation_status == "conflict":
        evidence_state = EvidenceState.INCONCLUSIVE_EVIDENCE
    elif not root_cause_text and collection_plan and collection_plan.requests:
        evidence_state = EvidenceState.COLLECTION_PLAN_READY
    if not root_cause_text and not (source_roots or config_roots or symbols_roots):
        source_action = "Run source-guided logging discovery with explicit repo/config roots"
        if not any(step.action == source_action for step in agg_nxts):
            agg_nxts.append(NextStep(
                action=source_action,
                why=("Existing logs did not establish a root cause; use --source-root "
                     "or --config-root for a bounded read-only scan."),
                skill="source_logging_discovery",
            ))
        evidence_gaps.append(EvidenceGap(
            code="source_guidance_not_run",
            summary="No explicit source/config roots were supplied for source-guided logging discovery.",
            blocking=False,
            skill="source_logging_discovery",
        ))

    diagnosis = _build_diagnosis(
        results=combined_results,
        findings=findings,
        root_chain=root_chain,
        problems=aggregated_problems,
        discovery=discovery_summary,
        error_block=error_block,
        executed=executed,
        failures=failures,
        confidence=confidence,
        time_range=time_range,
        cross_log_context=cross_ctx,
        solutions=agg_sols,
        next_steps=agg_nxts,
        additional_logs_needed=agg_logs,
        missing_logs=missing_logs,
    )

    report_written: str | None = None
    if report_path:
        try:
            _write_report(report_path, folder=folder, recursive=recursive,
                          discovery=discovery_summary, anchors=all_anchors,
                          window_seconds=window_seconds,
                          error_block=error_block,
                          executed=executed, results=results,
                          secondary=secondary, root_chain=root_chain,
                          solutions=agg_sols, next_steps=agg_nxts,
                          additional_logs_needed=agg_logs,
                          cross_log_context=cross_ctx,
                          missing_logs=missing_logs,
                          diagnosis=diagnosis,
                          agent_summary_path=agent_summary_path)
            report_written = str(report_path)
        except OSError as exc:
            findings.append(Finding(
                summary=f"Failed to write HTML report: {exc}",
                severity="warning",
            ))

    # --- Human-readable consolidated diagnosis on stderr ----------------
    _log("")
    _log("=" * 72)
    _log("CONSOLIDATED DIAGNOSIS")
    _log("=" * 72)
    _log(f"  entry skills run : {len(executed)}  ({', '.join(executed) or 'none'})")
    if fanout_calls:
        _log(f"  secondary fan-out: {', '.join(fanout_calls)}")
    if failures:
        _log(f"  failed skills    : {', '.join(failures)}")
    _log(f"  confidence       : {confidence}")
    _log(f"  evidence state   : {evidence_state}")
    if validation_status:
        _log(f"  cross-validation : {validation_status}")
    if root_chain:
        _log("")
        _log("Root-cause chain:")
        for item in root_chain:
            _log(f"  * [{item.get('skill','?')}] {item.get('finding','')}")
    if diagnosis:
        _log("")
        _log("Final diagnosis sections:")
        _log(f"  direct cause : {diagnosis.get('direct_cause')}")
        _log(f"  root cause   : {diagnosis.get('root_cause')}")
        timeline = diagnosis.get("timeline") or []
        if timeline:
            _log(f"  timeline     : {len(timeline)} timestamped event(s)")
            for item in timeline[:5]:
                _log(f"    - {item.get('time')} [{item.get('source')}] {item.get('event')}")
        evidence_chain = diagnosis.get("evidence_chain") or []
        if evidence_chain:
            _log("  evidence chain:")
            for item in evidence_chain[:6]:
                _log(f"    - {item}")
    crit = [f for f in findings if f.severity == "critical"]
    warn = [f for f in findings if f.severity == "warning"]
    if crit:
        _log("")
        _log(f"Critical findings ({len(crit)}):")
        for f in crit:
            _log(f"  {_sev_tag('critical')} {f.summary}")
    if warn:
        _log("")
        _log(f"Warning findings ({len(warn)}):")
        for f in warn[:10]:
            _log(f"  {_sev_tag('warning')} {f.summary}")
        if len(warn) > 10:
            _log(f"  ... ({len(warn) - 10} more)")
    if agg_sols:
        _log("")
        _log(f"Recommended solutions ({len(agg_sols)}):")
        for s in agg_sols:
            sev = getattr(s, "severity", None) or (s.get("severity", "info") if isinstance(s, dict) else "info")
            title = getattr(s, "title", None) or (s.get("title", "") if isinstance(s, dict) else "")
            steps = getattr(s, "steps", None) or (s.get("steps", []) if isinstance(s, dict) else [])
            _log(f"  - [{sev}] {title}")
            for step in (steps or [])[:3]:
                _log(f"      . {step}")
    if agg_nxts:
        _log("")
        _log(f"Next steps ({len(agg_nxts)}):")
        for n in agg_nxts:
            action = getattr(n, "action", None) or (n.get("action", "") if isinstance(n, dict) else "")
            why = getattr(n, "why", None) or (n.get("why", "") if isinstance(n, dict) else "")
            skill_id = getattr(n, "skill", None) or (n.get("skill") if isinstance(n, dict) else None)
            tag = f" ({skill_id})" if skill_id else ""
            _log(f"  -> {action}{tag}" + (f"  :: {why}" if why else ""))
    if missing_logs:
        _log("")
        _log(f"Missing required logs ({len(missing_logs)}):")
        for m in missing_logs:
            _log(f"  ! {m.get('log_kind')}  — {m.get('why','')}")
    if evidence_gaps:
        _log("")
        _log(f"Evidence gaps ({len(evidence_gaps)}):")
        for gap in evidence_gaps[:10]:
            marker = "blocking" if gap.blocking else "non-blocking"
            _log(f"  ? {gap.code} [{marker}] — {gap.summary}")
    if collection_plan and collection_plan.requests:
        _log("")
        _log(f"Collection plan ({len(collection_plan.requests)} request(s), operator review required):")
        for request in collection_plan.requests[:10]:
            admin = "admin" if request.requires_admin else "standard user"
            _log(f"  + {request.log_kind} [{request.mode}; {admin}] — {request.reason}")
    if report_written:
        _log("")
        _log(f"HTML report: {report_written}")
    _log("=" * 72)

    # Build the one-line executed_summary so the agent can quote it back.
    # Format: "<skill> OK" / "<skill> FAIL" comma-joined; "(none run)" if empty.
    summary_parts: list[str] = []
    for sid in executed:
        env = results.get(sid)
        ok_flag = bool(isinstance(env, dict) and env.get("ok"))
        summary_parts.append(f"{sid} {'OK' if ok_flag else 'FAIL'}")
    if secondary is not None:
        summary_parts.append(
            f"secondary {'OK' if secondary.get('ok') else 'FAIL'} "
            f"({', '.join(fanout_calls) or '-'})"
        )
    if validation_result is not None:
        summary_parts.append(
            f"validator {'OK' if validation_result.get('ok') else 'FAIL'} "
            f"({validation_status or 'unknown'})"
        )
    executed_summary = ", ".join(summary_parts) or "(no entry skills run)"

    return SkillResult(
        skill=SKILL_ID,
        ok=not failures,
        findings=findings,
        root_cause=root_cause_text,
        confidence=confidence,
        recommendations=[
            f"Executed: {executed_summary}",
            *([f"Failed skills: {', '.join(failures)}"] if failures else []),
            *(["Open the HTML report for a visual summary."] if report_written else []),
        ],
        solutions=agg_sols,
        next_steps=agg_nxts,
        additional_logs_needed=agg_logs,
        cross_validation=(validation_result or {}).get("cross_validation"),
        evidence_state=evidence_state,
        evidence_gaps=evidence_gaps,
        collection_plan=collection_plan,
        raw={
            "mode": "folder",
            "executed_summary": executed_summary,
            "input": {
                "folder": str(folder),
                "recursive": recursive,
                "allow_live_host": allow_live_host,
                "source_roots": len(source_roots),
                "config_roots": len(config_roots),
                "symbols_roots": len(symbols_roots),
                "anchors": [a.strftime("%Y-%m-%d %H:%M:%S") for a in all_anchors],
                "window": window,
                "window_seconds": window_seconds,
                "error_pattern": error_pattern,
                "time_range": time_range,
            },
            "discovery": discovery_summary,
            "error_locator": error_block,
            "executed": executed,
            "results": results,
            "secondary": secondary,
            "validation": validation_result,
            "quality": root_quality,
            "skipped_followups": skipped_followups,
            "root_cause_chain": root_chain,
            "cross_log_context": cross_ctx,
            "missing_logs": missing_logs,
            "diagnosis": diagnosis,
            "report_html_path": report_written,
        },
    )


def _aggregate_problems(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect every `raw.problems` from successful entry skills (best-effort)."""
    out: list[dict[str, Any]] = []
    for res in results.values():
        if not isinstance(res, dict) or not res.get("ok"):
            continue
        for p in (res.get("raw") or {}).get("problems", []) or []:
            if isinstance(p, dict):
                out.append(p)
    return out


def _render_agent_summary(path: Path) -> str:
    """Load `path` and turn it into an HTML fragment.

    Accepts:
    * `.html` / `.htm`  — displayed as escaped source, never active HTML
      * anything else      — treated as Markdown; converted via a small
                              stdlib-only converter that supports headings,
                              ordered/unordered lists, bold/italic, inline code,
                              fenced code blocks, and paragraphs.

    Errors are not raised — the resulting fragment falls back to a `<pre>` of
    the raw text so the report always renders.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return (f"<p class=\"warn\">Could not read agent summary "
                f"<code>{html_escape(str(path))}</code>: "
                f"{html_escape(str(exc))}</p>")
    if path.suffix.lower() in (".html", ".htm"):
        return f"<pre><code>{html_escape(raw)}</code></pre>"
    raw = str(redact_default(raw, key="agent_summary"))
    try:
        return _md_to_html(raw)
    except Exception as exc:  # pragma: no cover — defensive
        return (f"<p class=\"warn\">Failed to render Markdown "
                f"({html_escape(str(exc))}); raw text below.</p>"
                f"<pre>{html_escape(raw)}</pre>")


# Tiny, deterministic Markdown → HTML converter (stdlib-only).
# Supports headings, fenced/inline code, bold, italic, list items, links.
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+?)`")
_MD_BOLD_RE        = re.compile(r"\*\*([^*]+?)\*\*")
_MD_ITALIC_RE      = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?!\w)")
_MD_LINK_RE        = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_FENCE_OPEN_RE  = re.compile(r"^```([a-zA-Z0-9_+\-]*)\s*$")
_MD_HEADING_RE     = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_OL_RE          = re.compile(r"^\s*\d+\.\s+(.*)$")
_MD_UL_RE          = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_TABLE_SEP_RE   = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _md_inline(text: str) -> str:
    """Escape & apply inline transforms (code → bold → italic → links)."""
    out: list[str] = []
    # Pull out inline `code` segments first so other transforms skip them.
    i = 0
    for m in _MD_INLINE_CODE_RE.finditer(text):
        out.append(_md_inline_after_code(text[i:m.start()]))
        out.append(f"<code>{html_escape(m.group(1))}</code>")
        i = m.end()
    out.append(_md_inline_after_code(text[i:]))
    return "".join(out)


def _md_inline_after_code(text: str) -> str:
    s = html_escape(text)
    s = _MD_BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _MD_ITALIC_RE.sub(r"<em>\1</em>", s)
    s = _MD_LINK_RE.sub(
        lambda m: (f"<a href=\"{html_escape(m.group(2), quote=True)}\" "
                   f"target=\"_blank\" rel=\"noopener\">{m.group(1)}</a>"),
        s)
    return s


def _md_to_html(text: str) -> str:
    """Convert a Markdown fragment to HTML. Best-effort; no nested lists."""
    lines = text.splitlines()
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    list_open: str | None = None        # 'ul' | 'ol' | None
    para_buf: list[str] = []
    table_buf: list[str] = []           # collected raw rows for current table

    def _flush_para() -> None:
        nonlocal para_buf
        if para_buf:
            joined = " ".join(line.strip() for line in para_buf)
            if joined:
                out.append(f"<p>{_md_inline(joined)}</p>")
            para_buf = []

    def _close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    def _flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        # Split header | separator | body
        rows = [r for r in table_buf]
        if len(rows) < 2 or not _MD_TABLE_SEP_RE.match(rows[1]):
            # not a valid table — render as paragraphs
            for r in rows:
                out.append(f"<p>{_md_inline(r)}</p>")
            table_buf = []
            return
        header_cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
        body_rows = rows[2:]
        thead = "".join(f"<th>{_md_inline(c)}</th>" for c in header_cells)
        tbody = "".join(
            "<tr>" + "".join(
                f"<td>{_md_inline(c.strip())}</td>"
                for c in row.strip().strip("|").split("|")
            ) + "</tr>"
            for row in body_rows
        )
        out.append(f"<table><thead><tr>{thead}</tr></thead>"
                   f"<tbody>{tbody}</tbody></table>")
        table_buf = []

    for raw_line in lines:
        if in_code:
            if raw_line.strip().startswith("```"):
                out.append(f"<pre><code>"
                           f"{html_escape(chr(10).join(code_buf))}"
                           f"</code></pre>")
                code_buf = []
                in_code = False
            else:
                code_buf.append(raw_line)
            continue
        # Table rows: any line starting with '|' and containing another '|'
        if raw_line.lstrip().startswith("|") and raw_line.count("|") >= 2:
            _flush_para(); _close_list()
            table_buf.append(raw_line)
            continue
        else:
            _flush_table()
        m = _MD_FENCE_OPEN_RE.match(raw_line)
        if m:
            _flush_para(); _close_list()
            in_code = True
            continue
        if not raw_line.strip():
            _flush_para(); _close_list()
            continue
        h = _MD_HEADING_RE.match(raw_line)
        if h:
            _flush_para(); _close_list()
            level = min(len(h.group(1)) + 1, 6)  # offset by +1 so the agent's
                                                   # top-level # becomes <h2>
            out.append(f"<h{level}>{_md_inline(h.group(2))}</h{level}>")
            continue
        ol = _MD_OL_RE.match(raw_line)
        ul = _MD_UL_RE.match(raw_line)
        if ol:
            _flush_para()
            if list_open != "ol":
                _close_list()
                out.append("<ol>")
                list_open = "ol"
            out.append(f"<li>{_md_inline(ol.group(1))}</li>")
            continue
        if ul:
            _flush_para()
            if list_open != "ul":
                _close_list()
                out.append("<ul>")
                list_open = "ul"
            out.append(f"<li>{_md_inline(ul.group(1))}</li>")
            continue
        para_buf.append(raw_line)
    if in_code:
        out.append(f"<pre><code>{html_escape(chr(10).join(code_buf))}</code></pre>")
    _flush_table()
    _flush_para()
    _close_list()
    return "\n".join(out)


def _write_report(path: Path, *, folder: Path, recursive: bool,
                  discovery: dict[str, list[str]], anchors: list[datetime],
                  window_seconds: int, error_block: dict[str, Any],
                  executed: list[str], results: dict[str, dict[str, Any]],
                  secondary: dict[str, Any] | None,
                  root_chain: list[dict[str, str]],
                  solutions: list[Solution] | None = None,
                  next_steps: list[NextStep] | None = None,
                  additional_logs_needed: list[LogRequest] | None = None,
                  cross_log_context: dict[str, Any] | None = None,
                  missing_logs: list[dict[str, Any]] | None = None,
                  diagnosis: dict[str, Any] | None = None,
                  agent_summary_path: Path | None = None) -> None:
    from _shared import html_report  # late import: optional dep

    def public_item(value: Any) -> Any:
        if is_dataclass(value):
            value = asdict(value)
        return redact_default(value)

    folder_display = redact_default(str(folder), key="path")
    discovery = redact_default(discovery)
    error_block = redact_default(error_block)
    results = redact_default(results)
    secondary = redact_default(secondary)
    root_chain = redact_default(root_chain)
    solutions = [public_item(item) for item in (solutions or [])]
    next_steps = [public_item(item) for item in (next_steps or [])]
    additional_logs_needed = [
        public_item(item) for item in (additional_logs_needed or [])
    ]
    cross_log_context = redact_default(cross_log_context)
    missing_logs = redact_default(missing_logs)
    diagnosis = redact_default(diagnosis)

    def esc(x: Any) -> str:
        return html_escape(str(x), quote=True)

    rows_disc = "".join(
        f"<tr><td>{esc(k)}</td><td>{len(v)}</td>"
        f"<td><ul>{''.join(f'<li><code>{esc(p)}</code></li>' for p in v)}</ul></td></tr>"
        for k, v in discovery.items()
    ) or "<tr><td colspan=3><em>no logs discovered</em></td></tr>"

    rows_anchors = "".join(
        f"<li><code>{esc(a.strftime('%Y-%m-%d %H:%M:%S'))}</code> "
        f"(\u00b1{window_seconds}s)</li>"
        for a in anchors
    ) or "<li><em>no anchors</em></li>"

    rows_err = ""
    if error_block:
        hits_html = "".join(
            f"<tr><td><code>{esc(h.get('file',''))}</code></td>"
            f"<td>{esc(h.get('lineno',''))}</td>"
            f"<td>{esc(h.get('timestamp') or '—')}</td>"
            f"<td><code>{esc((h.get('excerpt') or '')[:160])}</code></td></tr>"
            for h in error_block.get("hits", [])[:30]
        ) or "<tr><td colspan=4><em>no matches</em></td></tr>"
        rows_err = (
            f"<h2>Error locator</h2>"
            f"<p>Pattern: <code>{esc(error_block.get('pattern'))}</code> &middot; "
            f"derived anchors: {len(error_block.get('derived_anchors', []))}</p>"
            f"<table><thead><tr><th>file</th><th>line</th><th>ts</th><th>excerpt</th></tr></thead>"
            f"<tbody>{hits_html}</tbody></table>"
        )

    rows_results = ""
    for skill in executed:
        res = results.get(skill, {})
        ok = bool(res.get("ok"))
        badge = "OK" if ok else "FAIL"
        rc = esc(res.get("root_cause") or "—")
        findings = res.get("findings") or []
        f_html = "".join(
            f"<li><b>{esc(f.get('severity',''))}</b>: {esc(f.get('summary',''))}</li>"
            for f in findings[:5]
        ) or "<li><em>no findings</em></li>"
        rows_results += (
            f"<section><h3>{esc(skill)} <small>[{badge}]</small></h3>"
            f"<p><b>root_cause:</b> {rc}</p>"
            f"<ul>{f_html}</ul></section>"
        )

    chain_html = " &rarr; ".join(esc(f"{c.get('skill','?')}: {c.get('finding','')}")
                                  for c in root_chain) or "<em>none</em>"

    secondary_html = ""
    if secondary:
        sec_findings = (secondary or {}).get("findings", []) or []
        sec_items = "".join(
            f"<li><b>{esc(f.get('severity',''))}</b>: {esc(f.get('summary',''))}</li>"
            for f in sec_findings[:10]
        )
        secondary_html = (
            f"<h2>Secondary fan-out</h2>"
            f"<p>ok={esc(secondary.get('ok'))}, "
            f"confidence={esc(secondary.get('confidence'))}</p>"
            f"<ul>{sec_items}</ul>"
        )

    # --- Advisory sections (Solutions / Next steps / Additional logs needed)
    advisory_html = ""
    if solutions or next_steps or additional_logs_needed:
        sections = html_report.advisory_sections(
            solutions=solutions or [],
            next_steps=next_steps or [],
            additional_logs_needed=additional_logs_needed or [],
            level=2,
        )
        advisory_html = "".join(
            f"<h{sec.level}>{esc(sec.heading)}</h{sec.level}>{sec.html}"
            for sec in sections
        )

    cross_html = ""
    if cross_log_context:
        avail = cross_log_context.get("available") or []
        tr = cross_log_context.get("time_range") or {}
        note = cross_log_context.get("note")
        cross_html = (
            "<h2>Cross-log context</h2>"
            f"<p>Available log kinds: {', '.join(f'<code>{esc(k)}</code>' for k in avail) or '<em>none</em>'}</p>"
            f"<p>Time range: <code>{esc(tr.get('start') or '—')}</code> &rarr; "
            f"<code>{esc(tr.get('end') or '—')}</code></p>"
            f"<p>Correlatable: <b>{esc(cross_log_context.get('correlatable'))}</b></p>"
            + (f"<p><em>{esc(note)}</em></p>" if note else "")
        )

    missing_html = ""
    if missing_logs:
        rows_missing = "".join(
            f"<tr><td><code>{esc(m.get('log_kind',''))}</code></td>"
            f"<td>{esc(m.get('why') or '—')}</td>"
            f"<td>{esc(m.get('how_to_collect') or '—')}</td>"
            f"<td><code>{esc(m.get('skill') or '—')}</code></td></tr>"
            for m in missing_logs
        )
        missing_html = (
            "<h2>Missing required logs</h2>"
            "<p class=\"warn\">The investigation referenced log kinds that were not "
            "provided. Findings may be incomplete until they are collected.</p>"
            "<table><thead><tr><th>log_kind</th><th>why</th>"
            "<th>how to collect</th><th>consumer skill</th></tr></thead>"
            f"<tbody>{rows_missing}</tbody></table>"
        )

    agent_summary_html = ""
    if agent_summary_path:
        body = _render_agent_summary(agent_summary_path)
        agent_summary_html = (
            "<section class=\"agent-summary\">"
            "<h2>Consolidated diagnosis (GitHub Copilot CLI)</h2>"
            f"<p class=\"meta\">Authored by the Copilot CLI agent based on the "
            f"skill outputs below &middot; source: "
            f"<code>{esc(redact_default(str(agent_summary_path), key='path'))}</code></p>"
            f"<div class=\"agent-summary-body\">{body}</div>"
            "</section>"
        )

    diagnosis_html = ""
    if diagnosis:
        summary = diagnosis.get("summary") or {}
        timeline = diagnosis.get("timeline") or []
        key_evidence = diagnosis.get("key_evidence") or []
        evidence_chain = diagnosis.get("evidence_chain") or []
        key_logs = diagnosis.get("key_logs") or []
        possible_causes = diagnosis.get("possible_causes") or []
        repair_suggestions = diagnosis.get("repair_suggestions") or []
        diag_next_steps = diagnosis.get("next_steps") or []
        diag_logs_needed = diagnosis.get("additional_logs_needed") or []

        timeline_rows = "".join(
            f"<tr><td><code>{esc(item.get('time') or '—')}</code></td>"
            f"<td>{esc(item.get('source') or '—')}</td>"
            f"<td>{esc(item.get('severity') or '')}</td>"
            f"<td>{esc(item.get('event') or '')}</td>"
            f"<td><code>{esc(item.get('log') or '')}</code></td></tr>"
            for item in timeline[:40]
        ) or "<tr><td colspan=5><em>No timestamped events extracted.</em></td></tr>"

        evidence_rows = "".join(
            f"<tr><td>{esc(item.get('skill') or '—')}</td>"
            f"<td>{esc(item.get('severity') or '—')}</td>"
            f"<td>{esc(item.get('summary') or '')}</td>"
            f"<td><code>{esc(json.dumps(item.get('evidence') or {}, ensure_ascii=True, default=str))}</code></td></tr>"
            for item in key_evidence[:40]
        ) or "<tr><td colspan=4><em>No key evidence extracted.</em></td></tr>"

        chain_items = "".join(f"<li>{esc(item)}</li>" for item in evidence_chain) \
            or "<li><em>No evidence chain generated.</em></li>"

        log_rows = "".join(
            f"<tr><td><code>{esc(item.get('kind') or '—')}</code></td>"
            f"<td>{esc(item.get('count') if item.get('count') is not None else '—')}</td>"
            f"<td>{esc(', '.join(item.get('consumer_skills') or []))}</td>"
            f"<td><ul>{''.join(f'<li><code>{esc(p)}</code></li>' for p in (item.get('sample_files') or []))}</ul></td></tr>"
            for item in key_logs
        ) or "<tr><td colspan=4><em>No key logs available.</em></td></tr>"

        cause_items = "".join(
            f"<li><b>{esc(item.get('type') or 'candidate')}</b>: "
            f"{esc(item.get('summary') or '')}</li>"
            for item in possible_causes
        ) or "<li><em>No additional possible causes identified.</em></li>"

        repair_items = "".join(
            f"<li><b>{esc(item.get('severity') or 'info')}</b>: "
            f"{esc(item.get('title') or '')}"
            f"{('<ol>' + ''.join(f'<li>{esc(step)}</li>' for step in (item.get('steps') or [])) + '</ol>') if item.get('steps') else ''}"
            f"</li>"
            for item in repair_suggestions
        ) or "<li><em>No standard repair suggestions generated.</em></li>"

        next_items = "".join(
            f"<li>{esc(item.get('action') or '')}"
            f"{' — ' + esc(item.get('why')) if item.get('why') else ''}"
            f"{' [' + esc(item.get('skill')) + ']' if item.get('skill') else ''}</li>"
            for item in diag_next_steps
        ) or "<li><em>No next steps generated.</em></li>"

        logs_needed_items = "".join(
            f"<li><code>{esc(item.get('log_kind') or '')}</code>: "
            f"{esc(item.get('why') or '')}"
            f"{' — ' + esc(item.get('how_to_collect')) if item.get('how_to_collect') else ''}</li>"
            for item in diag_logs_needed
        ) or "<li><em>No additional logs requested.</em></li>"

        diagnosis_html = f"""
<h2>Final diagnosis</h2>
<section>
 <h3>Summary</h3>
 <p><b>confidence:</b> {esc(summary.get('confidence') or '—')} &middot;
    <b>correlatable:</b> {esc(summary.get('correlatable'))} &middot;
    <b>failed skills:</b> {esc(', '.join(summary.get('failed') or []) or 'none')}</p>
 <p><b>direct cause:</b> {esc(diagnosis.get('direct_cause') or '—')}</p>
 <p><b>root cause:</b> {esc(diagnosis.get('root_cause') or '—')}</p>
</section>
<section>
 <h3>Event timeline</h3>
 <table><thead><tr><th>time</th><th>source</th><th>severity</th><th>event</th><th>log</th></tr></thead>
 <tbody>{timeline_rows}</tbody></table>
</section>
<section>
 <h3>Evidence chain</h3>
 <ol>{chain_items}</ol>
</section>
<section>
 <h3>Key evidence</h3>
 <table><thead><tr><th>skill</th><th>severity</th><th>summary</th><th>evidence</th></tr></thead>
 <tbody>{evidence_rows}</tbody></table>
</section>
<section>
 <h3>Key logs</h3>
 <table><thead><tr><th>kind</th><th>count</th><th>consumer skills</th><th>sample files</th></tr></thead>
 <tbody>{log_rows}</tbody></table>
</section>
<section>
 <h3>Possible causes</h3>
 <ul>{cause_items}</ul>
</section>
<section>
 <h3>Repair suggestions</h3>
 <ol>{repair_items}</ol>
</section>
<section>
 <h3>Next investigation steps</h3>
 <ol>{next_items}</ol>
</section>
<section>
 <h3>Additional logs needed</h3>
 <ul>{logs_needed_items}</ul>
</section>
"""

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>orchestrator report - {esc(folder_display)}</title>
<style>
 body{{font:14px/1.4 -apple-system,Segoe UI,Arial;margin:24px;max-width:1100px}}
 h1{{margin:0 0 4px}} h2{{margin-top:24px;border-bottom:1px solid #ddd;padding-bottom:4px}}
 h3{{margin:18px 0 4px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0}}
 th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}}
 th{{background:#f5f5f5}}
 code{{background:#f4f4f4;padding:1px 4px;border-radius:3px}}
 pre{{background:#f4f4f4;padding:8px 12px;border-radius:4px;overflow:auto}}
 pre code{{background:transparent;padding:0}}
 section{{background:#fafafa;border:1px solid #eee;padding:8px 12px;margin:8px 0;border-radius:4px}}
 section.agent-summary{{background:#f1f7ff;border-color:#bcd9ff;padding:12px 18px;margin:16px 0}}
 section.agent-summary h2{{margin-top:0;border-bottom-color:#bcd9ff;color:#0a4f99}}
 .agent-summary-body p{{margin:6px 0}}
 .agent-summary-body li{{margin:2px 0}}
 .meta{{color:#666;font-size:12px}}
 small{{color:#888}}
 .warn{{color:#a00}}
</style></head><body>
<h1>orchestrator report</h1>
<p><b>folder:</b> <code>{esc(folder_display)}</code> &middot; recursive={esc(recursive)} &middot;
 entry skills run: {len(executed)}</p>

{agent_summary_html}

{diagnosis_html}

<h2>Discovery</h2>
<table><thead><tr><th>kind</th><th>count</th><th>files</th></tr></thead>
<tbody>{rows_disc}</tbody></table>

<h2>Anchors</h2>
<ul>{rows_anchors}</ul>

{rows_err}

<h2>Per-skill results</h2>
{rows_results or '<p><em>no entry skills executed</em></p>'}

<h2>Root cause chain</h2>
<p>{chain_html}</p>

{cross_html}

{missing_html}

{advisory_html}

{secondary_html}
</body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
