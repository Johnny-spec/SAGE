#!/usr/bin/env python3
"""Read-only source/config scan that proposes the smallest useful log capture.

The scanner never imports, builds, or executes target code. It reads only
allowlisted text files within explicit budgets and emits short redacted facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

COMMON_ROOT = Path(__file__).resolve().parents[3]
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))

from investigation_protocol import (  # noqa: E402
    CollectionCommand,
    EvidenceGap,
    EvidenceState,
    build_collection_plan,
)

SKILL_ID = "source_logging_discovery"

DEFAULT_EXTENSIONS = {
    ".cs", ".csproj", ".fs", ".vb", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".config", ".xml", ".json", ".jsonc", ".yaml", ".yml", ".ini",
    ".toml", ".properties", ".ps1", ".cmd", ".bat", ".md", ".txt",
    ".map", ".sym",
}
EXCLUDED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "bin", "obj", "dist", "build",
    ".venv", "venv", "packages", ".pytest_cache", "__pycache__",
}
SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(password|passwd|pwd|client[_-]?secret|api[_-]?key|access[_-]?token|connectionstring)\b"
        r"(\s*[:=]\s*)([^\s,;\"']+|\"[^\"]*\"|'[^']*')"
    ),
    re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[^\s,;]+"),
    re.compile(r"(?i)([?&](?:sig|token|code|key)=)[^&#\s]+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL),
]

PATTERNS: list[dict[str, Any]] = [
    {
        "match_type": "iis_windows_auth",
        "capability": "iis_windows_auth",
        "regex": re.compile(r"(?i)(<authentication\s+mode=[\"']Windows|<windowsAuthentication\b|useAppPoolCredentials|\bNegotiate\b|SEC_E_[A-Z_]+)"),
        "logs": ["iis_w3c", "security_evtx", "evtx", "netlog"],
        "doc_query": "IIS Windows Authentication failed request tracing Kerberos event logging",
    },
    {
        "match_type": "iis_request_pipeline",
        "capability": "iis_request_pipeline",
        "regex": re.compile(r"(?i)(<system\.webServer\b|<handlers\b|<modules\b|<httpLogging\b|failedRequestTracing|FREB)"),
        "logs": ["iis_w3c", "http_err", "evtx"],
        "doc_query": "IIS failed request tracing configure FREB request pipeline",
    },
    {
        "match_type": "dotnet_eventsource",
        "capability": "dotnet_eventsource",
        "regex": re.compile(r"(?i)(EventSource\s*\(|EventSource\s*\[|\[EventSource\s*\(|EventSource\.Write|EventListener\b)"),
        "logs": ["etl", "evtx"],
        "doc_query": ".NET EventSource collect ETW trace provider",
    },
    {
        "match_type": "native_etw",
        "capability": "native_etw",
        "regex": re.compile(r"(?i)(TraceLoggingWrite|TRACELOGGING_DEFINE_PROVIDER|EventRegister|EventWrite|TraceEvent\()"),
        "logs": ["etl"],
        "doc_query": "Windows TraceLogging ETW collect provider WPR",
    },
    {
        "match_type": "application_logging",
        "capability": "application_logging",
        "regex": re.compile(r"(?i)(ILogger\b|LogInformation\(|LogError\(|Serilog|NLog|log4net|stdoutLogEnabled|AddEventLog\()"),
        "logs": ["application_log", "evtx"],
        "doc_query": "ASP.NET Core IIS stdout logging EventLog provider troubleshooting",
    },
    {
        "match_type": "edge_netlog_event",
        "capability": "edge_netlog",
        "regex": re.compile(r"\b(?:URL_REQUEST|HTTP_STREAM_JOB|SSL_CONNECT|AUTH_LIBRARY_INIT_SEC_CTX|HOST_RESOLVER_[A-Z_]+|PROXY_RESOLUTION_[A-Z_]+)\b"),
        "logs": ["netlog"],
        "doc_query": "Chromium NetLog event source dependency edge net-export",
    },
    {
        "match_type": "tls_or_certificate",
        "capability": "tls_certificate",
        "regex": re.compile(r"(?i)(SslStream|SCHANNEL|certificateHash|https?Binding|X509Certificate|SSL_HANDSHAKE_ERROR)"),
        "logs": ["netlog", "evtx"],
        "doc_query": "IIS TLS Schannel event logging Edge net-export certificate failure",
    },
]

EVENTSOURCE_NAME_RE = re.compile(
    r"(?i)EventSource\s*\(\s*Name\s*=\s*[\"']([^\"']+)[\"']"
)
TRACELOGGING_PROVIDER_RE = re.compile(
    r"(?i)TRACELOGGING_DEFINE_PROVIDER\s*\([^,]+,\s*[\"']([^\"']+)[\"']"
)
DOTNET_EVENT_RE = re.compile(
    r"(?i)\[Event\s*\(\s*(\d+)(?:\s*,[^\]]*?Level\s*=\s*EventLevel\.([A-Za-z]+))?"
)
EVENTLOG_SOURCE_RE = re.compile(
    r"(?i)EventLog\.WriteEntry\s*\(\s*[\"']([^\"']+)[\"']"
)
TRACE_CATEGORY_RE = re.compile(
    r"(?i)TRACE_EVENT(?:_BEGIN|_END|_INSTANT)?\s*\(\s*[\"']([^\"']+)[\"']"
)
NETLOG_EVENT_RE = re.compile(
    r"\b(?:AUTH|URL_REQUEST|HTTP_STREAM|SSL|TLS|HOST_RESOLVER|PROXY|CONNECT_JOB|SOCKET)_[A-Z0-9_]+\b"
)
SAFE_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _load_context(argument: str | None) -> dict[str, Any]:
    if not argument:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            return json.loads(raw) if raw.strip() else {}
        return {}
    if argument.startswith("@"):
        return json.loads(Path(argument[1:]).read_text(encoding="utf-8"))
    try:
        return json.loads(argument)
    except json.JSONDecodeError:
        return json.loads(Path(argument).read_text(encoding="utf-8"))


def _redact(text: str) -> tuple[str, int]:
    count = 0
    value = text
    for pattern in SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            if match.lastindex and match.lastindex >= 2:
                return f"{match.group(1)}{match.group(2)}<redacted>"
            if match.lastindex:
                return f"{match.group(1)}<redacted>"
            return "<redacted-sensitive-value>"
        value = pattern.sub(replace, value)
    return value, count


def _iter_files(
    roots: Iterable[Path],
    allowed_extensions: set[str],
    max_files: int,
) -> Iterable[tuple[str, Path, Path]]:
    yielded = 0
    for index, root in enumerate(roots, start=1):
        if not root.is_dir():
            continue
        label = f"root_{index}"
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                directory for directory in dirs
                if directory not in EXCLUDED_DIRS
                and not Path(current, directory).is_symlink()
            )
            for name in sorted(files):
                path = Path(current, name)
                if path.is_symlink() or path.suffix.lower() not in allowed_extensions:
                    continue
                yield label, root, path
                yielded += 1
                if yielded >= max_files:
                    return


def scan_context(ctx: dict[str, Any]) -> dict[str, Any]:
    extra = ctx.get("extra") or {}
    raw_roots = [
        *list(extra.get("repo_roots") or []),
        *list(extra.get("config_roots") or []),
        *list(extra.get("symbols_roots") or []),
    ]
    roots = [Path(str(root)).resolve() for root in raw_roots if Path(str(root)).is_dir()]
    allowed = {
        str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in (extra.get("allowed_extensions") or DEFAULT_EXTENSIONS)
    }
    max_file_bytes = max(1, int(extra.get("max_file_bytes", 1_048_576)))
    max_total_bytes = max(1, int(extra.get("max_total_bytes", 50 * 1_048_576)))
    max_files = max(1, int(extra.get("max_files", 5000)))
    max_hits = max(1, int(extra.get("max_hits", 200)))
    symptom_terms = [
        str(term).strip() for term in extra.get("symptom_terms") or [] if str(term).strip()
    ]

    hits: list[dict[str, Any]] = []
    capabilities: set[str] = set()
    log_requests: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    event_candidates: list[dict[str, Any]] = []
    netlog_candidates: dict[str, dict[str, Any]] = {}
    doc_queries: set[str] = set()
    files_seen = 0
    files_scanned = 0
    files_skipped_size = 0
    bytes_scanned = 0
    redactions = 0
    budget_exhausted = False

    for root_label, root, path in _iter_files(roots, allowed, max_files):
        files_seen += 1
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            files_skipped_size += 1
            continue
        if bytes_scanned + size > max_total_bytes:
            budget_exhausted = True
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        bytes_scanned += size
        files_scanned += 1
        relative = path.relative_to(root).as_posix()

        matched_lines: set[tuple[int, str]] = set()
        for pattern in PATTERNS:
            for match in pattern["regex"].finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                matched_lines.add((line, pattern["match_type"]))
                capabilities.add(pattern["capability"])
                doc_queries.add(pattern["doc_query"])
                for log_kind in pattern["logs"]:
                    log_requests.setdefault(log_kind, {
                        "log_kind": log_kind,
                        "why": (
                            f"Source/config capability {pattern['capability']} requires "
                            f"{log_kind} evidence to test the incident hypothesis."
                        ),
                        "skill": None,
                    })

        lowered = text.lower()
        for term in symptom_terms:
            position = lowered.find(term.lower())
            if position >= 0:
                line = text.count("\n", 0, position) + 1
                matched_lines.add((line, "symptom_term"))

        lines = text.splitlines()
        for line_number, match_type in sorted(matched_lines):
            if len(hits) >= max_hits:
                budget_exhausted = True
                break
            source = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
            snippet, count = _redact(source.strip()[:500])
            redactions += count
            hits.append({
                "root": root_label,
                "path": relative,
                "kind": "config" if path.suffix.lower() in {".config", ".xml", ".json", ".yaml", ".yml", ".ini"} else "source",
                "match_type": match_type,
                "line": line_number,
                "snippet_redacted": snippet[:240],
                "snippet_sha256": hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest(),
                "secret_redactions": count,
            })

        for regex, source_type in (
            (EVENTSOURCE_NAME_RE, "dotnet_eventsource"),
            (TRACELOGGING_PROVIDER_RE, "native_tracelogging"),
        ):
            for match in regex.finditer(text):
                name, count = _redact(match.group(1))
                redactions += count
                providers.setdefault(name, {
                    "provider_name": name,
                    "source": "local_source",
                    "source_type": source_type,
                    "confidence": "high",
                    "collection_cost": "medium",
                    "evidence": {"root": root_label, "path": relative},
                })
                log_requests.setdefault("etl", {
                    "log_kind": "etl",
                    "why": f"Provider {name} is declared in local source and may expose the failing path.",
                    "skill": None,
                })

        for match in DOTNET_EVENT_RE.finditer(text):
            event_candidates.append({
                "kind": "dotnet_eventsource_event",
                "event_id": int(match.group(1)),
                "level": match.group(2),
                "root": root_label,
                "path": relative,
                "line": text.count("\n", 0, match.start()) + 1,
                "confidence": "high",
            })
        for match in EVENTLOG_SOURCE_RE.finditer(text):
            name, count = _redact(match.group(1))
            redactions += count
            event_candidates.append({
                "kind": "windows_event_log_source",
                "provider_name": name,
                "root": root_label,
                "path": relative,
                "line": text.count("\n", 0, match.start()) + 1,
                "confidence": "high",
            })
            log_requests.setdefault("evtx", {
                "log_kind": "evtx",
                "why": f"Application source {name} is written by local code.",
                "skill": "event_log",
            })
        for match in TRACE_CATEGORY_RE.finditer(text):
            category, count = _redact(match.group(1))
            redactions += count
            event_candidates.append({
                "kind": "trace_category",
                "category": category,
                "root": root_label,
                "path": relative,
                "line": text.count("\n", 0, match.start()) + 1,
                "confidence": "medium",
            })
        for match in NETLOG_EVENT_RE.finditer(text):
            name = match.group(0)
            netlog_candidates.setdefault(name, {
                "event_name": name,
                "source": "local_source",
                "root": root_label,
                "path": relative,
                "line": text.count("\n", 0, match.start()) + 1,
                "confidence": "high",
            })

    plan = build_collection_plan(
        log_requests.values(),
        mode=str(extra.get("incident_mode") or "offline_bundle"),
        time_range=ctx.get("time_range") or {},
        products=[str(extra.get("product") or "unknown")],
        stop_when=[
            "A source-identified provider or log proves the failing component.",
            "The minimal required log set is collected and analyzers remain inconclusive.",
            "Collection would require executing target code or exceed the approved scope.",
        ],
    )
    provider_names = sorted(
        name for name in providers if SAFE_PROVIDER_NAME_RE.fullmatch(name)
    )
    if provider_names:
        for request in plan.requests:
            if request.log_kind != "etl":
                continue
            provider_args = " ".join(
                f'-p "{name}" 0xFFFFFFFF 5' for name in provider_names[:8]
            )
            request.commands.insert(0, CollectionCommand(
                platform="windows",
                shell="powershell",
                command=(
                    "logman create trace SourceGuided "
                    f"-o <bundle>\\source.etl -ets {provider_args}; "
                    "<reproduce once>; logman stop SourceGuided -ets"
                ),
                output=["<bundle>\\source.etl"],
                readonly=True,
                notes=(
                    "Provider names came from local source. Verify level and "
                    "keywords against authoritative documentation before capture."
                ),
            ))
    return {
        "roots_supplied": len(raw_roots),
        "roots_scanned": len(roots),
        "files_seen": files_seen,
        "files_scanned": files_scanned,
        "files_skipped_size": files_skipped_size,
        "bytes_scanned": bytes_scanned,
        "budget_exhausted": budget_exhausted,
        "allowed_extensions": sorted(allowed),
        "source_hits": hits,
        "matched_capabilities": sorted(capabilities),
        "provider_candidates": sorted(providers.values(), key=lambda item: item["provider_name"]),
        "event_candidates": event_candidates[:max_hits],
        "netlog_candidates": sorted(
            netlog_candidates.values(), key=lambda item: item["event_name"]
        )[:max_hits],
        "official_doc_lookup": {
            "mode": "suggestions_only",
            "suggested_queries": sorted(doc_queries),
            "source_content_sent": False,
        },
        "redaction": {"secret_values_removed": redactions},
        "collection_plan": asdict(plan),
        "log_requests": list(log_requests.values()),
        "safety": {
            "target_code_executed": False,
            "target_modules_imported": False,
            "network_access_used": False,
            "symlinks_followed": False,
            "full_source_in_output": False,
        },
    }


def analyze(ctx: dict[str, Any]) -> dict[str, Any]:
    result = scan_context(ctx)
    roots_missing = result["roots_scanned"] == 0
    hits = result["source_hits"]
    gaps = []
    if roots_missing:
        gaps.append(asdict(EvidenceGap(
            code="source_roots_missing",
            summary="No readable repo/config roots were supplied for source-guided discovery.",
            blocking=True,
        )))
    elif not hits:
        gaps.append(asdict(EvidenceGap(
            code="logging_capability_not_found",
            summary="No known logging capability was identified within the scan budget.",
            blocking=False,
            details={"budget_exhausted": result["budget_exhausted"]},
        )))

    evidence_state = (
        EvidenceState.COLLECTION_PLAN_READY
        if result["collection_plan"]["requests"]
        else EvidenceState.MISSING_REQUIRED_EVIDENCE
        if roots_missing
        else EvidenceState.INCONCLUSIVE_EVIDENCE
    )
    finding_summary = (
        f"Identified {len(result['matched_capabilities'])} logging capabilities and "
        f"prepared {len(result['collection_plan']['requests'])} collection requests."
        if hits else
        "Source-guided discovery did not identify a usable logging capability."
    )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "skill": SKILL_ID,
        "ok": True,
        "findings": [{
            "summary": finding_summary,
            "severity": "warning" if result["collection_plan"]["requests"] else "info",
            "evidence": {
                "matched_capabilities": result["matched_capabilities"],
                "provider_candidates": result["provider_candidates"],
                "event_candidate_count": len(result["event_candidates"]),
                "netlog_candidate_count": len(result["netlog_candidates"]),
                "source_hit_count": len(hits),
            },
        }],
        "root_cause": None,
        "confidence": "medium" if hits else "low",
        "recommendations": [
            "Review the collection plan; do not execute target source or build scripts.",
            "Use the suggested official-document queries to verify provider-specific collection syntax.",
        ],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": result["log_requests"],
        "cross_validation": None,
        "evidence_state": evidence_state,
        "evidence_gaps": gaps,
        "collection_plan": result["collection_plan"],
        "raw": {"logging_discovery": result},
        "error": None,
        "generated_at": now,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only source-guided log discovery")
    parser.add_argument("context", nargs="?", default=None)
    args = parser.parse_args(argv)
    try:
        result = analyze(_load_context(args.context))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "skill": SKILL_ID,
            "ok": False,
            "findings": [],
            "root_cause": None,
            "confidence": "low",
            "recommendations": [],
            "solutions": [],
            "next_steps": [],
            "additional_logs_needed": [],
            "cross_validation": None,
            "evidence_state": EvidenceState.TOOL_UNAVAILABLE,
            "evidence_gaps": [],
            "collection_plan": None,
            "raw": {},
            "error": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())