#!/usr/bin/env python3
"""Common bounded Process Monitor CSV/PML analyzer."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

_script_root = Path(__file__).resolve().parent
if str(_script_root) not in sys.path:
    sys.path.insert(0, str(_script_root))
_log_skills_root = Path(__file__).resolve().parents[2]
if str(_log_skills_root) not in sys.path:
    sys.path.insert(0, str(_log_skills_root))

from procmon_analysis import AnalysisFilter, ProcmonAnalyzer, finding_summary, parse_range
from procmon_io import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_ROWS,
    HARD_MAX_BYTES,
    HARD_MAX_FILES,
    HARD_MAX_ROWS,
    classify_source,
    discover_sources,
    export_pml,
    stream_csv,
)
from procmon_report import render_report
from symptom_relevance import assess_procmon_patterns, extract_context


_default_product_root = Path(__file__).resolve().parents[4] / "IIS"
_product_root = Path(os.environ.get("COMMON_SKILL_PRODUCT_ROOT", str(_default_product_root))).resolve()
sys.path.insert(0, str(_product_root))
from _shared.contract import Finding, LogRequest, SkillResult, load_context  # noqa: E402
from investigation_protocol import EvidenceGap, EvidenceState  # type: ignore[reportMissingImports]  # noqa: E402
from redaction import redact_default  # type: ignore[reportMissingImports]  # noqa: E402


SKILL_ID = os.environ.get("COMMON_PROCMON_SKILL_ID", "procmon")
TRUSTED_EXE_ENV = "SKILLS_PROCMON_EXE"
TIME_BASIS_ENV = "SKILLS_PROCMON_TIME_BASIS"


def _as_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike)):
        return [str(value)]
    if isinstance(value, Iterable):
        return [str(item) for item in value if item]
    return []


def _bounded_int(value: Any, default: int, hard_max: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, hard_max))


def _no_input_result(rejected: list[dict[str, Any]] | None = None) -> SkillResult:
    rejected = rejected or []
    unsupported = bool(rejected)
    return SkillResult(
        skill=SKILL_ID,
        ok=not unsupported,
        findings=[Finding(
            summary=(
                "No supported Procmon capture was supplied."
                if not unsupported else
                "The supplied files are not supported Procmon CSV/PML captures."
            ),
            severity="info" if not unsupported else "warning",
        )],
        confidence="low",
        additional_logs_needed=[LogRequest(
            log_kind="procmon",
            why="A bounded Process Monitor reproduction is needed for file, registry, process, or network activity evidence.",
            how_to_collect=(
                "Start Process Monitor immediately before one reproduction, stop immediately after, "
                "save the PML master, and export all events to CSV with Time of Day, Process Name, "
                "PID, Operation, Result, Path, and Detail columns."
            ),
            skill=SKILL_ID,
        )],
        evidence_state=(
            EvidenceState.UNKNOWN_LOG_TYPE if unsupported
            else EvidenceState.MISSING_REQUIRED_EVIDENCE
        ),
        evidence_gaps=[EvidenceGap(
            code="procmon_input_missing" if not unsupported else "procmon_input_unsupported",
            summary=(
                "No Procmon capture was provided."
                if not unsupported else "Explicit inputs did not match the Procmon export schema."
            ),
            log_kind="procmon",
            skill=SKILL_ID,
            details={"rejected": rejected[:20]},
        )],
        raw={
            "acquisitions": [],
            "rejected_inputs": rejected,
            "quality": {
                "capture_complete": False,
                "sample_sufficient": False,
                "time_comparable": False,
                "direct_join": False,
                "independent_sources": 0,
                "blocking_gaps": True,
                "records_seen": 0,
            },
        },
        error="No supported Procmon capture was available." if unsupported else None,
    )


def analyse(
    ctx: dict[str, Any],
    *,
    cli_paths: Iterable[str] = (),
    cli_folder: str | None = None,
    trusted_procmon_exe: str | None = None,
    report_path: str | None = None,
    max_bytes_override: int | None = None,
    max_rows_override: int | None = None,
    max_files_override: int | None = None,
    deadline_seconds: float | None = None,
    time_basis_override: str | None = None,
) -> SkillResult:
    extra = ctx.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    rejected_control_fields = sorted(
        key for key in ("procmon_exe", "procmon_exe_path", "procmon_command")
        if extra.get(key)
    )

    explicit_paths = list(cli_paths) + _as_paths(extra.get("procmon_paths"))
    folder = cli_folder or extra.get("procmon_folder")
    max_files = _bounded_int(
        max_files_override if max_files_override is not None else extra.get("max_files"),
        DEFAULT_MAX_FILES,
        HARD_MAX_FILES,
    )
    discovered = discover_sources(
        explicit_paths,
        folder,
        recursive=not bool(extra.get("no_recursive")),
        max_files=max_files,
    )
    rejected_inputs: list[dict[str, Any]] = []
    discovered_set = {path.resolve() for path in discovered}
    for raw_path in explicit_paths:
        path = Path(raw_path)
        if not path.is_file():
            rejected_inputs.append({"source": str(path), "status": "missing_input"})
        elif path.resolve() not in discovered_set:
            rejected_inputs.append({"source": str(path), "status": "unsupported_schema"})
    if not discovered:
        result = _no_input_result(rejected_inputs)
        if rejected_control_fields:
            result.raw["rejected_control_fields"] = rejected_control_fields
        return result

    max_bytes = _bounded_int(
        max_bytes_override if max_bytes_override is not None else extra.get("max_input_bytes"),
        DEFAULT_MAX_BYTES,
        HARD_MAX_BYTES,
    )
    max_rows = _bounded_int(
        max_rows_override if max_rows_override is not None else extra.get("max_rows"),
        DEFAULT_MAX_ROWS,
        HARD_MAX_ROWS,
    )
    deadline_value = deadline_seconds if deadline_seconds is not None else extra.get("deadline_seconds")
    try:
        deadline_budget = float(deadline_value) if deadline_value is not None else None
    except (TypeError, ValueError):
        deadline_budget = None
    deadline = time.monotonic() + max(0.05, deadline_budget) if deadline_budget else None

    time_basis = str(
        time_basis_override
        or os.environ.get(TIME_BASIS_ENV)
        or extra.get("procmon_time_basis")
        or "unknown"
    ).strip().casefold()
    if time_basis not in {"unknown", "context"}:
        time_basis = "unknown"
    requested_incident_range = parse_range(ctx.get("time_range"))
    requested_baseline_range = parse_range(extra.get("baseline_time_range"))
    incident_range = requested_incident_range if time_basis == "context" else None
    baseline_range = requested_baseline_range if time_basis == "context" else None
    filter_spec = AnalysisFilter.from_extra(extra)
    explicit_filter = bool(
        extra.get("procmon_filter")
        or extra.get("process_names")
        or extra.get("pids")
    )
    inferred_processes: list[str] = []
    scope_mode = "explicit" if explicit_filter else "unscoped"
    if not explicit_filter:
        inferred_processes = list(extract_context(ctx).get("target_processes") or [])
        if inferred_processes:
            filter_spec = AnalysisFilter(
                process_names=frozenset(inferred_processes),
                pids=filter_spec.pids,
                operation_families=filter_spec.operation_families,
                result_contains=filter_spec.result_contains,
                path_contains=filter_spec.path_contains,
                contains=filter_spec.contains,
            )
            scope_mode = "symptom_inferred"
    analyzer = ProcmonAnalyzer(
        incident_range=incident_range,
        baseline_range=baseline_range,
        filter_spec=filter_spec,
    )
    remaining_bytes = max_bytes
    remaining_rows = max_rows
    acquisitions: list[dict[str, Any]] = []
    usable_sources = 0
    tool_unavailable = False

    def _consume_csv(csv_path: Path, source: Path, kind: str, conversion: dict[str, Any] | None = None) -> None:
        nonlocal remaining_bytes, remaining_rows, usable_sources
        analyzer.begin_source()
        read = stream_csv(
            csv_path,
            analyzer.consume,
            max_bytes=max(1, remaining_bytes),
            max_rows=max(1, remaining_rows),
            deadline_monotonic=deadline,
        )
        remaining_bytes = max(0, remaining_bytes - read.bytes_read)
        remaining_rows = max(0, remaining_rows - read.rows_seen)
        acquisition = read.to_dict()
        acquisition.update({"source": str(source), "kind": kind})
        if conversion:
            acquisition["conversion"] = conversion
        acquisitions.append(acquisition)
        if read.status in {"success", "budget_exhausted"}:
            usable_sources += 1

    for source in discovered:
        if remaining_bytes <= 0 or remaining_rows <= 0 or (deadline and time.monotonic() >= deadline):
            acquisitions.append({
                "source": str(source), "kind": classify_source(source),
                "status": "not_scanned", "stop_reason": "aggregate_budget_exhausted",
                "rows_seen": 0, "bytes_read": 0,
            })
            continue
        kind = classify_source(source)
        if kind == "csv":
            _consume_csv(source, source, kind)
            continue
        with tempfile.TemporaryDirectory(prefix="procmon-export-") as temp_dir:
            exported = Path(temp_dir) / f"{source.stem}.csv"
            conversion = export_pml(
                source,
                exported,
                trusted_procmon_exe,
                timeout_seconds=max(1, int(deadline_budget or 300)),
            )
            conversion_dict = conversion.to_dict()
            if conversion.status != "success":
                tool_unavailable = tool_unavailable or conversion.status == "tool_unavailable"
                acquisitions.append({
                    "source": str(source), "kind": "pml", "status": conversion.status,
                    "rows_seen": 0, "bytes_read": 0, "conversion": conversion_dict,
                    "error": conversion.error,
                })
                continue
            _consume_csv(exported, source, "pml", conversion_dict)

    analysis = analyzer.summary()
    patterns, context_assessment = assess_procmon_patterns(
        analysis.get("patterns") or [], ctx,
    )
    supportive_patterns = [
        pattern for pattern in patterns
        if pattern.get("signal_classification") == "supportive"
    ]
    contextual_patterns = [
        pattern for pattern in patterns
        if pattern.get("signal_classification") == "contextual"
    ]
    candidate_pattern = context_assessment.get("candidate_pattern")
    analysis["patterns"] = patterns[:50]
    analysis["context_assessment"] = context_assessment
    analysis["candidate_cause"] = (
        finding_summary(candidate_pattern) if candidate_pattern else None
    )
    findings = [
        Finding(
            summary=finding_summary(pattern),
            severity=pattern.get("severity", "warning"),
            evidence={
                "type": pattern.get("type"),
                "process_name": pattern.get("process_name"),
                "pid": pattern.get("pid"),
                "operation": pattern.get("operation"),
                "result": pattern.get("result"),
                "path": pattern.get("path"),
                "count": pattern.get("count"),
                "send_count": pattern.get("send_count"),
                "receive_count": pattern.get("receive_count"),
                "receive_ratio": pattern.get("receive_ratio"),
                "distinct_remote_endpoints": pattern.get("distinct_remote_endpoints"),
                "service_port": pattern.get("service_port"),
                "service_name": pattern.get("service_name"),
                "baseline_count": pattern.get("baseline_count"),
                "baseline_ratio": pattern.get("baseline_ratio"),
                "falsifier": pattern.get("falsifier"),
                "next_source": pattern.get("next_source"),
                "symptom_relevance": pattern.get("symptom_relevance"),
                "signal_classification": pattern.get("signal_classification"),
                "noise_risk": pattern.get("noise_risk"),
                "relevance_score": pattern.get("relevance_score"),
                "relevance_reasons": pattern.get("relevance_reasons"),
                **(pattern.get("sample") or {}),
            },
        )
        for pattern in supportive_patterns[:10]
    ]
    acquisition_failures = [item for item in acquisitions if item.get("status") != "success"]
    if not findings:
        findings.append(Finding(
            summary=(
                "No high-signal Procmon pattern met the configured thresholds."
                if usable_sources else "No Procmon source could be analyzed."
            ),
            severity="info" if usable_sources else "warning",
        ))
    if context_assessment.get("background_count"):
        findings.append(Finding(
            summary=(
                f"Suppressed {context_assessment['background_count']} Procmon pattern(s) "
                "as background observations because they did not align with the reported symptom."
            ),
            severity="info",
            evidence={
                "type": "procmon_background_noise_summary",
                "background_count": context_assessment["background_count"],
                "signal_classification": "background",
                "noise_risk": "high",
                "relevance_reasons": context_assessment.get("noise_controls"),
            },
        ))
    if contextual_patterns:
        findings.append(Finding(
            summary=(
                f"Classified {len(contextual_patterns)} Procmon pattern(s) as contextual; "
                "bounded samples remain in raw analysis and are not used as causal evidence."
            ),
            severity="info",
            evidence={
                "type": "procmon_contextual_summary",
                "contextual_count": len(contextual_patterns),
                "signal_classification": "contextual",
                "noise_risk": "medium",
                "relevance_reasons": context_assessment.get("noise_controls"),
            },
        ))
    for item in acquisition_failures[:5]:
        findings.append(Finding(
            summary=f"Procmon source was not fully analyzed: {item.get('status')}",
            severity="warning",
            evidence={
                "source_file": item.get("source"),
                "status": item.get("status"),
                "stop_reason": item.get("stop_reason"),
            },
        ))

    time_comparable = bool(
        time_basis == "context"
        and (analysis.get("timestamp_model") or {}).get("wall_clock_comparable")
    )
    acquisition_complete = bool(acquisitions) and not acquisition_failures
    incident = analysis.get("incident") or {}
    capture_covers_window = False
    if incident_range and incident.get("first_time") and incident.get("last_time"):
        first = parse_range({"start": incident["first_time"], "end": incident["last_time"]})
        capture_covers_window = bool(
            first and first[0] <= incident_range[0] and first[1] >= incident_range[1]
        )
    capture_complete = acquisition_complete and (
        capture_covers_window if incident_range else False
    )
    sample_sufficient = bool(analysis.get("sample_sufficient"))
    blocking_gaps = bool(acquisition_failures) or not capture_complete
    evidence_gaps: list[EvidenceGap] = []
    if acquisition_failures:
        evidence_gaps.append(EvidenceGap(
            code="procmon_acquisition_incomplete",
            summary="One or more Procmon sources were not analyzed completely.",
            log_kind="procmon",
            skill=SKILL_ID,
            details={"statuses": sorted({str(item.get("status")) for item in acquisition_failures})},
        ))
    if usable_sources and not time_comparable:
        evidence_gaps.append(EvidenceGap(
            code="procmon_time_not_comparable",
            summary="The capture has time-of-day values without a trustworthy incident date anchor.",
            log_kind="procmon",
            skill=SKILL_ID,
        ))
    if usable_sources and not capture_complete:
        evidence_gaps.append(EvidenceGap(
            code="procmon_capture_scope_unproven",
            summary="Reading the file does not prove that Procmon captured the full incident window.",
            log_kind="procmon",
            skill=SKILL_ID,
        ))

    next_log_kind = {
        "access_denied": "security_evtx",
        "lock_contention": "etl",
        "network_failure": "netlog",
        "image_load_failure": "evtx",
        "missing_dependency": "evtx",
        "repeated_path_probe": "procmon",
        "process_restart_loop": "evtx",
        "slow_operation": "perf_counter",
        "dc_udp_response_gap": "packet_capture",
    }
    additional_logs: list[LogRequest] = []
    if supportive_patterns:
        strongest = supportive_patterns[0]
        additional_logs.append(LogRequest(
            log_kind=next_log_kind.get(strongest.get("type"), "evtx"),
            why=(
                f"Needed to falsify or corroborate the Procmon candidate: "
                f"{strongest.get('type')}."
            ),
            how_to_collect=str(strongest.get("next_source") or "Collect an independent source for the same window."),
            skill=None,
        ))

    if not usable_sources:
        state = EvidenceState.TOOL_UNAVAILABLE if tool_unavailable else EvidenceState.UNKNOWN_LOG_TYPE
    elif not time_comparable:
        state = EvidenceState.TIME_NOT_COMPARABLE
    else:
        state = EvidenceState.INCONCLUSIVE_EVIDENCE
    raw = {
        "acquisitions": acquisitions,
        "rejected_inputs": rejected_inputs,
        "rejected_control_fields": rejected_control_fields,
        "analysis": analysis,
        "analysis_scope": {
            "mode": scope_mode,
            "process_names": sorted(filter_spec.process_names),
            "pids": sorted(filter_spec.pids),
            "rows_filtered": int(analysis.get("rows_filtered") or 0),
        },
        "candidate_cause": analysis.get("candidate_cause"),
        "timestamp_basis": {
            "mode": time_basis,
            "context_comparison_enabled": time_basis == "context",
            "note": (
                "Operator confirmed Procmon timestamps use the same basis as context.time_range."
                if time_basis == "context" else
                "Procmon CSV timestamps are local and unzoned; cross-source comparison is disabled."
            ),
        },
        "budgets": {
            "max_files": max_files,
            "max_input_bytes": max_bytes,
            "max_rows": max_rows,
            "deadline_seconds": deadline_budget,
            "remaining_bytes": remaining_bytes,
            "remaining_rows": remaining_rows,
        },
        "quality": {
            "capture_complete": capture_complete,
            "file_read_complete": acquisition_complete,
            "capture_covers_requested_window": capture_covers_window,
            "sample_sufficient": sample_sufficient,
            "time_comparable": time_comparable,
            "direct_join": False,
            "independent_sources": 1 if usable_sources else 0,
            "blocking_gaps": blocking_gaps,
            "records_seen": int(analysis.get("rows_received") or 0),
            "threshold_model": "noise_aware_static_with_optional_baseline_delta",
        },
    }
    result = SkillResult(
        skill=SKILL_ID,
        ok=bool(usable_sources),
        findings=findings,
        root_cause=None,
        confidence=(
            "medium"
            if candidate_pattern
            and candidate_pattern.get("signal_classification") == "supportive"
            and sample_sufficient
            else "low"
        ),
        recommendations=(
            [
                "Correlate the strongest Procmon pattern with an independent source before remediation.",
                "Use the finding's falsifier to test whether the observed operation is causal or expected control flow.",
            ]
            if supportive_patterns else
            ["Narrow the capture to the affected process and reproduction window, then compare with a healthy baseline."]
        ),
        additional_logs_needed=additional_logs,
        evidence_state=state,
        evidence_gaps=evidence_gaps,
        raw=raw,
        error=None if usable_sources else "No usable Procmon capture was available.",
    )

    selected_report = report_path
    if selected_report:
        try:
            safe_payload = redact_default({
                "analysis": analysis,
                "acquisitions": acquisitions,
                "quality": raw["quality"],
            })
            report = Path(str(selected_report))
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(render_report(safe_payload), encoding="utf-8")
            result.raw["report_html_path"] = str(report.resolve())
        except OSError as exc:
            result.raw["report_error"] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Process Monitor CSV/PML analyzer")
    parser.add_argument("context", nargs="?", default=None, help="JSON context, @file, or stdin")
    parser.add_argument("--input", action="append", default=[], help="Procmon CSV/PML path; repeatable")
    parser.add_argument("--folder", default=None, help="Folder containing Procmon CSV/PML evidence")
    parser.add_argument("--procmon-exe", default=None, help="Trusted Procmon executable for PML export")
    parser.add_argument("--report", default=None, help="Write a redacted self-contained HTML report")
    parser.add_argument("--max-input-bytes", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--deadline-seconds", type=float, default=None)
    parser.add_argument(
        "--time-basis", choices=("unknown", "context"), default=None,
        help="Use 'context' only when Procmon timestamps share context.time_range's clock basis.",
    )
    args = parser.parse_args(argv)

    if args.context is None and not sys.stdin.isatty():
        ctx = json.loads(sys.stdin.read())
    else:
        ctx = load_context([args.context] if args.context else [])
    trusted_exe = args.procmon_exe or os.environ.get(TRUSTED_EXE_ENV)
    result = analyse(
        ctx,
        cli_paths=args.input,
        cli_folder=args.folder,
        trusted_procmon_exe=trusted_exe,
        report_path=args.report,
        max_bytes_override=args.max_input_bytes,
        max_rows_override=args.max_rows,
        max_files_override=args.max_files,
        deadline_seconds=args.deadline_seconds,
        time_basis_override=args.time_basis,
    )
    result.emit()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())