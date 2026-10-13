"""Cross-product investigation states, evidence gaps, and collection plans.

This module is intentionally standard-library only. It creates plans; it never
executes collection commands. Product skills may serialize the dataclasses via
``dataclasses.asdict`` as part of their normal result envelope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


class EvidenceState:
    ROOT_CAUSE_FOUND = "ROOT_CAUSE_FOUND"
    INCONCLUSIVE_EVIDENCE = "INCONCLUSIVE_EVIDENCE"
    MISSING_REQUIRED_EVIDENCE = "MISSING_REQUIRED_EVIDENCE"
    TIME_NOT_COMPARABLE = "TIME_NOT_COMPARABLE"
    PERMISSION_BLOCKED = "PERMISSION_BLOCKED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    UNKNOWN_LOG_TYPE = "UNKNOWN_LOG_TYPE"
    LIVE_COLLECTION_REQUIRED = "LIVE_COLLECTION_REQUIRED"
    COLLECTION_PLAN_READY = "COLLECTION_PLAN_READY"

    ALL = {
        ROOT_CAUSE_FOUND,
        INCONCLUSIVE_EVIDENCE,
        MISSING_REQUIRED_EVIDENCE,
        TIME_NOT_COMPARABLE,
        PERMISSION_BLOCKED,
        TOOL_UNAVAILABLE,
        UNKNOWN_LOG_TYPE,
        LIVE_COLLECTION_REQUIRED,
        COLLECTION_PLAN_READY,
    }


@dataclass
class EvidenceGap:
    code: str
    summary: str
    severity: str = "warning"
    blocking: bool = True
    log_kind: str | None = None
    skill: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectionCommand:
    platform: str
    shell: str
    command: str
    output: list[str] = field(default_factory=list)
    readonly: bool = True
    notes: str | None = None


@dataclass
class CollectionRequest:
    id: str
    log_kind: str
    required: bool
    reason: str
    consumer_skill: str | None = None
    mode: str = "offline_export"
    requires_admin: bool = False
    sensitivity: list[str] = field(default_factory=list)
    commands: list[CollectionCommand] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    alternatives: list[str] = field(default_factory=list)


@dataclass
class CollectionPlan:
    schema_version: str = "1.0"
    status: str = "ready_for_operator_review"
    incident_scope: dict[str, Any] = field(default_factory=dict)
    requests: list[CollectionRequest] = field(default_factory=list)
    stop_when: list[str] = field(default_factory=list)
    safety: dict[str, Any] = field(default_factory=lambda: {
        "execute_automatically": False,
        "readonly_only": True,
        "requires_operator_review": True,
        "raw_evidence_may_be_sensitive": True,
    })


def _command(
    command: str,
    *output: str,
    platform: str = "windows",
    shell: str = "powershell",
    notes: str | None = None,
) -> CollectionCommand:
    return CollectionCommand(
        platform=platform,
        shell=shell,
        command=command,
        output=list(output),
        notes=notes,
    )


COLLECTOR_CATALOG: dict[str, dict[str, Any]] = {
    "iis_w3c": {
        "mode": "offline_copy",
        "requires_admin": False,
        "sensitivity": ["client_ip", "username", "url", "query_string"],
        "commands": [
            _command(
                "Copy-Item \"$env:SystemDrive\\inetpub\\logs\\LogFiles\\W3SVC*\\u_ex*.log\" <bundle> -Force",
                "<bundle>\\u_ex*.log",
                notes="Restrict copied files to the incident window where possible.",
            )
        ],
        "validation": {
            "file_globs": ["u_ex*.log"],
            "required_fields": ["date", "time", "sc-status", "time-taken"],
            "must_cover_time_range": True,
        },
    },
    "http_err": {
        "mode": "offline_copy",
        "requires_admin": False,
        "sensitivity": ["client_ip", "url", "app_pool"],
        "commands": [
            _command(
                "Copy-Item \"$env:SystemRoot\\System32\\LogFiles\\HTTPERR\\httperr*.log\" <bundle> -Force",
                "<bundle>\\httperr*.log",
            )
        ],
        "validation": {
            "file_globs": ["httperr*.log"],
            "required_fields": ["date", "time", "s-reason"],
            "must_cover_time_range": True,
        },
    },
    "evtx": {
        "mode": "offline_export",
        "requires_admin": True,
        "sensitivity": ["machine_name", "username", "process_path"],
        "commands": [
            _command(
                "wevtutil epl Application <bundle>\\Application.evtx; wevtutil epl System <bundle>\\System.evtx",
                "<bundle>\\Application.evtx",
                "<bundle>\\System.evtx",
            )
        ],
        "validation": {
            "file_globs": ["Application.evtx", "System.evtx"],
            "minimum_records": 1,
            "must_cover_time_range": True,
        },
    },
    "security_evtx": {
        "mode": "offline_export",
        "requires_admin": True,
        "sensitivity": ["username", "domain", "client_ip", "object_path"],
        "commands": [
            _command(
                "wevtutil epl Security <bundle>\\Security.evtx",
                "<bundle>\\Security.evtx",
                notes="Security log export requires elevation and may contain personal data.",
            )
        ],
        "validation": {
            "file_globs": ["Security.evtx"],
            "minimum_records": 1,
            "must_cover_time_range": True,
        },
    },
    "perf_counter": {
        "mode": "live_capture",
        "requires_admin": True,
        "sensitivity": ["process_name", "machine_name"],
        "commands": [
            _command(
                "logman create counter IISIncident -o <bundle>\\iis_perf.blg -f bincirc -max 512 -si 00:00:05 -c \"\\Processor(_Total)\\% Processor Time\" \"\\Memory\\Available MBytes\" \"\\Process(w3wp*)\\Working Set\" \"\\PhysicalDisk(_Total)\\Avg. Disk sec/Read\" \"\\PhysicalDisk(_Total)\\Avg. Disk sec/Write\"; logman start IISIncident",
                "<bundle>\\iis_perf.blg",
                notes="Stop with: logman stop IISIncident; logman delete IISIncident",
            )
        ],
        "validation": {
            "file_globs": ["*.blg"],
            "minimum_duration_seconds": 60,
            "must_cover_time_range": True,
        },
    },
    "firewall": {
        "mode": "offline_copy",
        "requires_admin": True,
        "sensitivity": ["source_ip", "destination_ip", "port"],
        "commands": [
            _command(
                "Copy-Item \"$env:SystemRoot\\System32\\LogFiles\\Firewall\\pfirewall.log\" <bundle> -Force",
                "<bundle>\\pfirewall.log",
                notes="If absent, review Get-NetFirewallProfile logging settings before enabling collection.",
            )
        ],
        "validation": {
            "file_globs": ["pfirewall.log"],
            "must_cover_time_range": True,
        },
    },
    "netlog": {
        "mode": "interactive_reproduction",
        "requires_admin": False,
        "sensitivity": ["url", "hostname", "proxy", "cookies_if_sensitive_mode"],
        "commands": [
            _command(
                "Open edge://net-export, start logging to disk, reproduce once, stop logging, and place the JSON in <bundle>.",
                "<bundle>\\*.json",
                shell="manual",
                notes="Use default capture mode unless sensitive capture is explicitly approved.",
            )
        ],
        "validation": {
            "file_globs": ["*.json"],
            "required_json_keys": ["constants", "events"],
            "minimum_records": 1,
        },
    },
    "procmon": {
        "mode": "interactive_reproduction",
        "requires_admin": True,
        "sensitivity": [
            "username", "process_name", "command_line", "file_path",
            "registry_path", "url", "network_endpoint",
        ],
        "commands": [
            _command(
                "Open Process Monitor as administrator, start capture immediately before one reproduction, stop immediately after, save the PML master, then export all events to CSV into <bundle>.",
                "<bundle>\\procmon.pml",
                "<bundle>\\procmon.csv",
                shell="manual",
                notes=(
                    "Operator review is required. Do not enable boot logging or Drop Filtered Events "
                    "unless the investigation specifically requires it. Preserve the unfiltered PML master."
                ),
            )
        ],
        "validation": {
            "file_globs": ["*.pml", "*.csv"],
            "required_fields": [
                "Time of Day", "Process Name", "PID", "Operation",
                "Result", "Path", "Detail",
            ],
            "minimum_records": 1,
            "must_cover_time_range": True,
        },
        "alternatives": [
            "Use WPR/ETL when stack, scheduler, disk, or CPU attribution is required.",
            "Use NetLog or a packet capture when protocol payload and network-hop evidence is required.",
        ],
    },
    "crashpad": {
        "mode": "offline_copy",
        "requires_admin": False,
        "sensitivity": ["process_memory", "paths", "user_data"],
        "commands": [
            _command(
                "Copy-Item \"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Crashpad\\reports\\*.dmp\" <bundle> -Force",
                "<bundle>\\*.dmp",
            )
        ],
        "validation": {"file_globs": ["*.dmp"], "minimum_files": 1},
    },
    "etl": {
        "mode": "live_capture",
        "requires_admin": True,
        "sensitivity": ["process_name", "paths", "network_endpoints"],
        "commands": [
            _command(
                "wpr -start GeneralProfile -filemode; <reproduce once>; wpr -stop <bundle>\\trace.etl",
                "<bundle>\\trace.etl",
                notes="Replace the generic profile with a provider-specific WPRP when source discovery identifies one.",
            )
        ],
        "validation": {"file_globs": ["*.etl"], "minimum_files": 1},
    },
    "application_log": {
        "mode": "offline_copy",
        "requires_admin": False,
        "sensitivity": ["username", "paths", "request_data", "application_secrets"],
        "commands": [
            _command(
                "Copy the application log files identified by source/config discovery into <bundle>, restricted to the incident window.",
                "<bundle>\\application-logs\\*",
                shell="manual",
            )
        ],
        "validation": {"minimum_files": 1, "must_cover_time_range": True},
    },
}


def _field(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _request_id(log_kind: str, consumer: str | None) -> str:
    raw = f"collect-{log_kind}-{consumer or 'evidence'}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")


def build_collection_plan(
    log_requests: Iterable[object],
    *,
    mode: str = "offline_bundle",
    time_range: Mapping[str, Any] | None = None,
    hosts: Iterable[str] = (),
    products: Iterable[str] = (),
    stop_when: Iterable[str] = (),
) -> CollectionPlan:
    """Convert legacy LogRequest-like values into a deterministic safe plan."""
    requests: list[CollectionRequest] = []
    seen: set[tuple[str, str | None]] = set()
    for item in log_requests:
        log_kind = str(_field(item, "log_kind", "") or "").strip()
        if not log_kind:
            continue
        consumer = _field(item, "skill") or _field(item, "consumer_skill")
        consumer = str(consumer) if consumer else None
        key = (log_kind, consumer)
        if key in seen:
            continue
        seen.add(key)
        reason = str(_field(item, "why", "") or _field(item, "reason", "") or "Additional evidence is required.")
        required = bool(_field(item, "required", True))
        catalog = COLLECTOR_CATALOG.get(log_kind, {})
        commands = [
            CollectionCommand(**vars(command))
            for command in catalog.get("commands", [])
        ]
        legacy_how = _field(item, "how_to_collect")
        if legacy_how and not commands:
            commands.append(_command(str(legacy_how), shell="manual"))
        requests.append(CollectionRequest(
            id=_request_id(log_kind, consumer),
            log_kind=log_kind,
            required=required,
            reason=reason,
            consumer_skill=consumer,
            mode=str(catalog.get("mode", "operator_guided")),
            requires_admin=bool(catalog.get("requires_admin", False)),
            sensitivity=list(catalog.get("sensitivity", [])),
            commands=commands,
            validation=dict(catalog.get("validation", {})),
            alternatives=list(catalog.get("alternatives", [])),
        ))

    return CollectionPlan(
        incident_scope={
            "mode": mode,
            "time_range": dict(time_range or {}),
            "hosts": sorted({str(host) for host in hosts if host}),
            "products": sorted({str(product) for product in products if product}),
        },
        requests=requests,
        stop_when=list(stop_when) or [
            "A direct root-cause join is established from independent evidence.",
            "All required requests are collected and analyzers remain inconclusive.",
            "Collection risk exceeds the approved incident scope.",
        ],
    )


def derive_evidence_state(
    *,
    root_cause: str | None = None,
    confidence: str = "low",
    direct_join: bool = False,
    independent_sources: int = 0,
    blocking_gaps: bool = False,
    sample_sufficient: bool = False,
    capture_complete: bool = False,
    missing_required: bool = False,
    time_not_comparable: bool = False,
    permission_blocked: bool = False,
    tool_unavailable: bool = False,
    unknown_log_type: bool = False,
    live_collection_required: bool = False,
) -> str:
    """Derive one primary investigation state from explicit quality signals."""
    if permission_blocked:
        return EvidenceState.PERMISSION_BLOCKED
    if tool_unavailable:
        return EvidenceState.TOOL_UNAVAILABLE
    if time_not_comparable:
        return EvidenceState.TIME_NOT_COMPARABLE
    if unknown_log_type:
        return EvidenceState.UNKNOWN_LOG_TYPE
    if live_collection_required:
        return EvidenceState.LIVE_COLLECTION_REQUIRED
    if missing_required:
        return EvidenceState.MISSING_REQUIRED_EVIDENCE
    evidence_supported = direct_join or independent_sources >= 2
    if (root_cause and confidence in {"high", "medium"}
            and evidence_supported and not blocking_gaps
            and sample_sufficient and capture_complete):
        return EvidenceState.ROOT_CAUSE_FOUND
    return EvidenceState.INCONCLUSIVE_EVIDENCE


def aggregate_root_quality(results: Iterable[object]) -> dict[str, Any]:
    """Reduce quality only from successful, explicitly proven root results."""
    proven: list[Mapping[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        if (result.get("ok") and result.get("root_cause")
                and result.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND):
            proven.append(result)

    qualities = [
        (result.get("raw") or {}).get("quality") or {}
        for result in proven
        if isinstance(result.get("raw"), Mapping)
    ]
    declared_sources = [
        int(quality.get("independent_sources", 0) or 0)
        for quality in qualities
        if isinstance(quality, Mapping)
        and not isinstance(quality.get("independent_sources", 0), bool)
    ]
    return {
        "proven_results": len(proven),
        "direct_join": any(
            quality.get("direct_join") is True for quality in qualities
            if isinstance(quality, Mapping)
        ),
        "independent_sources": max(
            [len(proven), *declared_sources], default=0
        ),
        "sample_sufficient": bool(proven) and all(
            quality.get("sample_sufficient") is True for quality in qualities
            if isinstance(quality, Mapping)
        ) and len(qualities) == len(proven),
        "capture_complete": bool(proven) and all(
            quality.get("capture_complete") is True for quality in qualities
            if isinstance(quality, Mapping)
        ) and len(qualities) == len(proven),
        "time_comparable": bool(proven) and all(
            quality.get("time_comparable") is True for quality in qualities
            if isinstance(quality, Mapping)
        ) and len(qualities) == len(proven),
        "blocking_gaps": any(
            quality.get("blocking_gaps") is not False for quality in qualities
            if isinstance(quality, Mapping)
        ) or len(qualities) != len(proven),
    }


def gaps_from_missing(log_requests: Iterable[object]) -> list[EvidenceGap]:
    gaps: list[EvidenceGap] = []
    for item in log_requests:
        log_kind = str(_field(item, "log_kind", "") or "").strip()
        if not log_kind:
            continue
        reason = str(_field(item, "why", "") or _field(item, "reason", "") or "Required evidence is missing.")
        skill = _field(item, "skill") or _field(item, "consumer_skill")
        gaps.append(EvidenceGap(
            code="missing_log",
            summary=f"Missing {log_kind}: {reason}",
            log_kind=log_kind,
            skill=str(skill) if skill else None,
        ))
    return gaps


__all__ = [
    "EvidenceState", "EvidenceGap", "CollectionCommand", "CollectionRequest",
    "CollectionPlan", "COLLECTOR_CATALOG", "build_collection_plan",
    "aggregate_root_quality", "derive_evidence_state", "gaps_from_missing",
]