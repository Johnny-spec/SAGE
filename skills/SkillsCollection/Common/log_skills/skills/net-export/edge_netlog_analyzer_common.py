#!/usr/bin/env python3
"""
edge_netlog.py — Chromium net-export (edge://net-export) analyser.

Consumes the `netlog` log_kind referenced by the playbook for the
`page_load_failure`, `cert_error`, `proxy_issue`, and `dns_issue` problem
types. Uses _shared/sources/netlog.py for the low-level read; this skill
adds the diagnostic interpretation layer (ERR_* codes, slow requests,
proxy fallback, TLS handshake failures).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlsplit, urlunsplit

# Load product-local shared modules from the invoking product root.
_default_product_root = Path(__file__).resolve().parents[4] / "Edge"
_product_root = Path(os.environ.get("COMMON_SKILL_PRODUCT_ROOT", str(_default_product_root))).resolve()
sys.path.insert(0, str(_product_root))
_log_skills_root = Path(__file__).resolve().parents[2]
if str(_log_skills_root) not in sys.path:
    sys.path.insert(0, str(_log_skills_root))
from _shared.contract import (  # noqa: E402
    Finding, LogRequest, SkillResult, load_context,
)
from _shared import playbook  # noqa: E402
from _shared.sources import netlog as netlog_src  # noqa: E402
from investigation_protocol import EvidenceGap, EvidenceState  # type: ignore[reportMissingImports]  # noqa: E402
from symptom_relevance import (  # noqa: E402
    annotate_netlog_findings, assess_netlog_problem_types,
)

SKILL_ID = "edge_netlog"
SLOW_REQUEST_THRESHOLD_MS = 5000
MAX_TOP_REQUESTS = 5
MAX_FINDINGS_PER_KIND = 3

SSPI_STATUS_NAMES = {
    0x00000000: ("SEC_E_OK", "success"),
    0x00090312: ("SEC_I_CONTINUE_NEEDED", "handshake continues - normal"),
    0x00090313: ("SEC_I_COMPLETE_NEEDED", "complete the token - normal"),
    0x00090314: ("SEC_I_COMPLETE_AND_CONTINUE", "complete and continue - normal"),
    0x8009030C: ("SEC_E_LOGON_DENIED", "bad credentials or account disabled"),
    0x8009030E: ("SEC_E_NO_CREDENTIALS", "no credentials available for the target"),
    0x80090308: ("SEC_E_INVALID_TOKEN", "authentication token is invalid"),
    0x80090322: ("SEC_E_WRONG_PRINCIPAL", "SPN does not match the service account"),
    0x80090303: ("SEC_E_TARGET_UNKNOWN", "KDC could not find the target principal"),
    0x80090311: ("SEC_E_NO_AUTHENTICATING_AUTHORITY", "no domain controller is reachable"),
    0x80090346: ("SEC_E_BAD_BINDINGS", "channel binding mismatch"),
    0x80090350: (
        "SEC_E_DOWNGRADE_DETECTED",
        "authentication downgrade detected; the domain controller or target SPN may be unavailable",
    ),
}

NET_ERROR_NAMES = {
    -7: "ERR_TIMED_OUT",
    -101: "ERR_CONNECTION_RESET",
    -102: "ERR_CONNECTION_REFUSED",
    -105: "ERR_NAME_NOT_RESOLVED",
    -118: "ERR_CONNECTION_TIMED_OUT",
    -130: "ERR_PROXY_CONNECTION_FAILED",
    -200: "ERR_CERT_COMMON_NAME_INVALID",
    -201: "ERR_CERT_DATE_INVALID",
    -202: "ERR_CERT_AUTHORITY_INVALID",
    -337: "ERR_HTTP2_PROTOCOL_ERROR",
    -338: "ERR_INVALID_AUTH_CREDENTIALS",
    -339: "ERR_UNSUPPORTED_AUTH_SCHEME",
    -341: "ERR_MISSING_AUTH_CREDENTIALS",
    -342: "ERR_UNEXPECTED_SECURITY_LIBRARY_STATUS",
    -343: "ERR_MISCONFIGURED_AUTH_ENVIRONMENT",
    -344: "ERR_UNDOCUMENTED_SECURITY_LIBRARY_STATUS",
    -375: "ERR_TOO_MANY_RETRIES",
}


def _decode_sspi(value: Any) -> tuple[str | None, str | None]:
    try:
        code = int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None, None
    return SSPI_STATUS_NAMES.get(code, (f"0x{code:08X}", None))


def _decode_net_error(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("ERR_"):
        return value
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return NET_ERROR_NAMES.get(code, f"net_error={code}")


def _redact_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.split("?", 1)[0].split("#", 1)[0]
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def _resolve_sources(extra: dict[str, Any]) -> list[Path]:
    """Resolve net-export files from extra.netlog_paths and/or extra.folder."""
    out: list[Path] = []
    for raw in extra.get("netlog_paths") or []:
        p = Path(raw)
        if p.is_file():
            out.append(p)
    folder = extra.get("folder")
    if folder:
        out.extend(_discover_netlogs_in_folder(Path(folder),
                                               recursive=not extra.get("no_recursive")))
    # Dedup, keep order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _looks_like_netlog(path: Path, peek_bytes: int = 4096) -> bool:
    """Cheap structural check: top-level JSON has both 'events' and 'constants'."""
    try:
        with path.open("rb") as fh:
            head = fh.read(peek_bytes).decode("utf-8", errors="replace")
    except OSError:
        return False
    # Net-export files are large but the constants block is near the top.
    # A substring probe is enough to reject random JSON files cheaply.
    return '"constants"' in head and '"logSourceType"' in head


def _discover_netlogs_in_folder(folder: Path, recursive: bool = True) -> list[Path]:
    if not folder.is_dir():
        return []
    pattern = "**/*.json" if recursive else "*.json"
    return [p for p in sorted(folder.glob(pattern)) if _looks_like_netlog(p)]


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------

def _extract_net_error(params: Any) -> str | None:
    """Return the ERR_* string from a netlog event's params, if any."""
    if not isinstance(params, dict):
        return None
    for key in ("net_error", "error", "result"):
        v = params.get(key)
        decoded = _decode_net_error(v)
        if decoded not in (None, "net_error=0", "net_error=-1"):
            return decoded
    for v in params.values():
        if isinstance(v, dict):
            decoded = _extract_net_error(v)
            if decoded:
                return decoded
    return None


def _classify_request_span(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Given all events for one URL_REQUEST source_id, return a request row."""
    url = None
    method = None
    net_error: str | None = None
    t_begin = None
    t_end = None
    response_codes: list[int] = []
    auth_restarts = 0
    for ev in events:
        params = ev.get("params") or {}
        if url is None and isinstance(params, dict) and params.get("url"):
            url = _redact_url(params.get("url"))
            method = params.get("method") or method
        if isinstance(params, dict):
            response_code = params.get("http_status_code") or params.get("response_code")
            if isinstance(response_code, int):
                response_codes.append(response_code)
        if ev.get("event_type") == "HTTP_TRANSACTION_DRAIN_BODY_FOR_AUTH_RESTART":
            auth_restarts += 1
        err = _extract_net_error(params)
        if err and err != "ERR_IO_PENDING":
            net_error = err
        try:
            t = int(ev.get("time")) if ev.get("time") is not None else None
        except (TypeError, ValueError):
            t = None
        if t is not None:
            if t_begin is None or t < t_begin:
                t_begin = t
            if t_end is None or t > t_end:
                t_end = t
    duration_ms = (t_end - t_begin) if (t_begin is not None and t_end is not None) else None
    return {
        "url": url,
        "method": method,
        "net_error": net_error,
        "duration_ms": duration_ms,
        "event_count": len(events),
        "auth_restarts": auth_restarts,
        "response_codes": response_codes,
    }


def _analyse_auth(entries: list[dict[str, Any]]) -> dict[str, Any]:
    spn_attempts: list[dict[str, Any]] = []
    handler_results: list[dict[str, Any]] = []
    schemes_seen: Counter[str] = Counter()
    origins_seen: Counter[str] = Counter()
    sspi_statuses: Counter[str] = Counter()
    net_errors: Counter[str] = Counter()
    pending_init: dict[Any, deque[dict[str, Any]]] = defaultdict(deque)
    unmatched_end = 0

    for entry in entries:
        event_type = entry.get("event_type") or ""
        if not event_type.startswith(("AUTH_", "HTTP_TRANSACTION_DRAIN")):
            continue
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            continue
        status = params.get("status") if isinstance(params.get("status"), dict) else {}
        net_value = status.get("net_error", params.get("net_error"))
        security_value = status.get("security_status", params.get("security_status"))
        decoded_net = _decode_net_error(net_value)
        if decoded_net:
            net_errors[decoded_net] += 1
        security_name, security_description = _decode_sspi(security_value)
        if security_name:
            sspi_statuses[security_name] += 1

        source_id = entry.get("source_id")
        if event_type == "AUTH_LIBRARY_INIT_SEC_CTX":
            if entry.get("phase") == 1:
                pending_init[source_id].append({
                    "spn": params.get("spn"),
                    "flags": params.get("flags"),
                    "time": entry.get("time_unix_ms") or entry.get("time"),
                })
                continue
            begin = pending_init[source_id].popleft() if pending_init[source_id] else {}
            if not begin:
                unmatched_end += 1
            context = params.get("context") if isinstance(params.get("context"), dict) else {}
            spn_attempts.append({
                "source_id": source_id,
                "spn": params.get("spn") or begin.get("spn"),
                "mechanism": context.get("mechanism"),
                "net_error": decoded_net,
                "security_status": security_name,
                "security_status_desc": security_description,
                "flags": params.get("flags") or context.get("flags") or begin.get("flags"),
                "paired": bool(begin),
                "begin_time": begin.get("time"),
                "end_time": entry.get("time_unix_ms") or entry.get("time"),
            })
        elif event_type == "AUTH_HANDLER_CREATE_RESULT":
            scheme = params.get("scheme")
            origin = _redact_url(params.get("origin"))
            if scheme:
                schemes_seen[str(scheme).lower()] += 1
            if origin:
                origins_seen[origin] += 1
            handler_results.append({
                "source_id": source_id,
                "scheme": scheme,
                "challenge_scheme": str(params.get("challenge") or "").split(" ", 1)[0] or None,
                "origin": origin,
                "allows_default_credentials": params.get("allows_default_credentials"),
                "net_error": decoded_net,
            })

    return {
        "spn_attempts": spn_attempts,
        "handler_results": handler_results,
        "schemes_seen": dict(schemes_seen),
        "origins_seen": dict(origins_seen),
        "sspi_status_counts": dict(sspi_statuses),
        "net_error_counts": dict(net_errors),
        "pairing": {
            "unmatched_begin": sum(len(queue) for queue in pending_init.values()),
            "unmatched_end": unmatched_end,
            "paired_attempts": sum(1 for attempt in spn_attempts if attempt["paired"]),
        },
    }


def _detect_auth_problems(
    auth: dict[str, Any], requests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    wrong_principal = [
        attempt for attempt in auth["spn_attempts"]
        if attempt.get("security_status") == "SEC_E_WRONG_PRINCIPAL"
    ]
    if wrong_principal:
        spns = Counter(attempt.get("spn") for attempt in wrong_principal if attempt.get("spn"))
        failed_spn, failed_count = spns.most_common(1)[0] if spns else (None, 0)
        origins = Counter(
            result.get("origin") for result in auth["handler_results"]
            if result.get("origin")
            and result.get("source_id") in {item.get("source_id") for item in wrong_principal}
        )
        origin = origins.most_common(1)[0][0] if origins else None
        url_host = urlparse(origin).hostname if origin else None
        spn_host = (
            failed_spn.split("/", 1)[1].split(":", 1)[0].lower()
            if failed_spn and "/" in failed_spn else None
        )
        problems.append({
            "type": "kerberos_spn_mismatch",
            "severity": "critical",
            "description": (
                f"Kerberos returned SEC_E_WRONG_PRINCIPAL {len(wrong_principal)} time(s)"
                f" for SPN {failed_spn!r}."
            ),
            "evidence": {
                "failed_spn": failed_spn,
                "failed_spn_count": failed_count,
                "origin": origin,
                "url_host": url_host,
                "spn_host": spn_host,
                "host_mismatch": bool(
                    url_host and spn_host and url_host.lower() != spn_host
                ),
                "sample_attempts": wrong_principal[:3],
            },
        })

    downgrade = [
        attempt for attempt in auth["spn_attempts"]
        if attempt.get("security_status") == "SEC_E_DOWNGRADE_DETECTED"
    ]
    if downgrade:
        spns = Counter(attempt.get("spn") for attempt in downgrade if attempt.get("spn"))
        failed_spn = spns.most_common(1)[0][0] if spns else None
        origins = Counter(
            result.get("origin") for result in auth["handler_results"]
            if result.get("origin")
            and result.get("source_id") in {item.get("source_id") for item in downgrade}
        )
        origin = origins.most_common(1)[0][0] if origins else None
        problems.append({
            "type": "auth_error",
            "severity": "warning",
            "description": (
                f"Negotiate failed with SEC_E_DOWNGRADE_DETECTED {len(downgrade)} time(s)"
                + (f" for SPN {failed_spn!r}" if failed_spn else "")
                + (f" on origin {origin}" if origin else "")
                + "; the domain controller or target SPN could not be safely validated."
            ),
            "evidence": {
                "failed_spn": failed_spn,
                "origin": origin,
                "failure_count": len(downgrade),
                "sample_attempts": downgrade[:3],
            },
        })

    missing_credentials = auth["net_error_counts"].get("ERR_MISSING_AUTH_CREDENTIALS", 0)
    loops = [
        request for request in requests
        if request.get("auth_restarts", 0) >= 3
        and (
            (request.get("response_codes") or [None])[-1] == 401
            or request.get("net_error") in (
                "ERR_MISSING_AUTH_CREDENTIALS", "ERR_INVALID_AUTH_CREDENTIALS"
            )
        )
    ]
    if loops:
        problems.append({
            "type": "auth_handshake_loop",
            "severity": "warning",
            "description": (
                f"{len(loops)} URL request(s) repeated the 401 authentication handshake; "
                f"ERR_MISSING_AUTH_CREDENTIALS appeared {missing_credentials} time(s)."
            ),
            "evidence": {
                "looping_requests_sample": loops[:3],
                "missing_credentials_count": missing_credentials,
            },
        })
    elif missing_credentials:
        problems.append({
            "type": "auth_error",
            "severity": "warning",
            "description": (
                f"ERR_MISSING_AUTH_CREDENTIALS appeared {missing_credentials} time(s), "
                "but a repeated 401 loop was not proven."
            ),
            "evidence": {
                "missing_credentials_count": missing_credentials,
                "handshake_loop_proven": False,
            },
        })

    windows_schemes = {
        scheme for scheme in auth["schemes_seen"] if scheme in {"negotiate", "ntlm"}
    }
    if windows_schemes and not problems:
        problems.append({
            "type": "auth_error",
            "severity": "info",
            "description": (
                f"Browser saw {sum(auth['schemes_seen'][scheme] for scheme in windows_schemes)} "
                f"Windows-auth challenges (schemes: {sorted(windows_schemes)}); "
                "no SSPI failure was recorded."
            ),
            "evidence": {"schemes_seen": auth["schemes_seen"]},
        })
    return problems


def _analyse_one(
    path: Path, *, max_input_bytes: int, max_events: int
) -> dict[str, Any]:
    read = netlog_src.read_file(
        path,
        max_input_bytes=max_input_bytes,
        max_events=max_events,
    )
    if read.status != "success":
        return {
            "file": path.name,
            "status": read.status,
            "error": read.error,
            "quality": read.quality,
            "time_model": read.time_model,
            "dependency_graph": read.dependency_graph,
        }
    entries = read.entries
    summary = netlog_src.summarise(entries)

    # Per-source-id grouping for URL_REQUEST spans.
    by_source: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if e.get("source_type") == "URL_REQUEST":
            by_source[e.get("source_id")].append(e)
    requests = [_classify_request_span(evs) for evs in by_source.values()]

    failed = [r for r in requests if r["net_error"]]
    slow = [r for r in requests if (r["duration_ms"] or 0) >= SLOW_REQUEST_THRESHOLD_MS]
    slow.sort(key=lambda r: r["duration_ms"] or 0, reverse=True)
    error_counts = Counter(r["net_error"] for r in failed)

    # DNS failures: HOST_RESOLVER_IMPL events whose params include net_error.
    dns_failures = [
        e for e in entries
        if e.get("source_type") in ("HOST_RESOLVER_IMPL", "HOST_RESOLVER_IMPL_JOB",
                                    "HOST_RESOLVER_IMPL_REQUEST")
        and _extract_net_error(e.get("params"))
    ]

    # TLS / cert failures: ERR_CERT_* anywhere
    cert_failures = [r for r in failed if (r["net_error"] or "").startswith("ERR_CERT")]

    # Proxy fallback events: PROXY_RESOLVER or anything mentioning "proxy"
    # with a phase==1 (end) and a net_error.
    proxy_issues = [
        e for e in entries
        if (e.get("source_type") or "").startswith("PROXY")
        and _extract_net_error(e.get("params"))
    ]
    auth = _analyse_auth(entries)
    auth_problems = _detect_auth_problems(auth, requests)

    return {
        "file": path.name,
        "status": "success",
        "summary": summary,
        "requests_total": len(requests),
        "requests_failed": len(failed),
        "requests_slow": len(slow),
        "error_breakdown": dict(error_counts.most_common(10)),
        "top_slow_requests": slow[:MAX_TOP_REQUESTS],
        "dns_failures": len(dns_failures),
        "cert_failures": len(cert_failures),
        "proxy_issues": len(proxy_issues),
        "sample_failures": failed[:MAX_TOP_REQUESTS],
        "auth": auth,
        "auth_problems": auth_problems,
        "quality": read.quality,
        "time_model": read.time_model,
        "dependency_graph": read.dependency_graph,
    }


# ---------------------------------------------------------------------------
# Build envelope
# ---------------------------------------------------------------------------

def _findings_from_files(per_file: list[dict[str, Any]]) -> tuple[list[Finding], list[str]]:
    """Return (findings, observed_problem_types)."""
    findings: list[Finding] = []
    problem_types: list[str] = []

    total_req = sum(f["requests_total"] for f in per_file)
    total_fail = sum(f["requests_failed"] for f in per_file)
    total_slow = sum(f["requests_slow"] for f in per_file)
    total_dns = sum(f["dns_failures"] for f in per_file)
    total_cert = sum(f["cert_failures"] for f in per_file)
    total_proxy = sum(f["proxy_issues"] for f in per_file)
    auth_schemes: Counter[str] = Counter()
    auth_statuses: Counter[str] = Counter()
    auth_net_errors: Counter[str] = Counter()
    auth_problems: list[dict[str, Any]] = []
    for file_result in per_file:
        auth = file_result.get("auth") or {}
        auth_schemes.update(auth.get("schemes_seen") or {})
        auth_statuses.update(auth.get("sspi_status_counts") or {})
        auth_net_errors.update(auth.get("net_error_counts") or {})
        auth_problems.extend({
            **problem,
            "source_file": file_result.get("file"),
        } for problem in file_result.get("auth_problems") or [])

    findings.append(Finding(
        summary=(f"Parsed {len(per_file)} net-export file(s): "
                 f"{total_req} URL requests, {total_fail} failed, {total_slow} slow."),
        severity="info",
        evidence={
            "files": [f["file"] for f in per_file],
            "total_requests": total_req,
            "total_failed": total_fail,
            "total_slow": total_slow,
            "auth_schemes_seen": dict(auth_schemes),
            "sspi_status_counts": dict(auth_statuses),
            "auth_net_error_counts": dict(auth_net_errors),
        },
    ))

    seen_auth_types: set[str] = set()
    for problem in auth_problems:
        problem_type = problem.get("type")
        if not problem_type or problem_type in seen_auth_types:
            continue
        seen_auth_types.add(problem_type)
        problem_types.append(problem_type)
        problem_evidence = dict(problem.get("evidence") or {})
        samples = (
            problem_evidence.get("sample_attempts")
            or problem_evidence.get("looping_requests_sample")
            or []
        )
        first_sample = samples[0] if samples and isinstance(samples[0], dict) else {}
        findings.append(Finding(
            summary=problem.get("description") or problem_type,
            severity=problem.get("severity", "warning"),
            evidence={
                "observed_problem_type": problem_type,
                "source_file": problem.get("source_file"),
                "source_id": first_sample.get("source_id"),
                **problem_evidence,
            },
        ))

    if total_cert:
        problem_types.append("cert_error")
        findings.append(Finding(
            summary=f"TLS/cert errors observed in {total_cert} URL request(s).",
            severity="critical",
            evidence={"observed_problem_type": "cert_error",
                      "sample": [r for f in per_file for r in f["sample_failures"]
                                 if (r["net_error"] or "").startswith("ERR_CERT")
                                 ][:MAX_FINDINGS_PER_KIND]},
        ))
    if total_dns:
        problem_types.append("dns_issue")
        findings.append(Finding(
            summary=f"DNS resolution failures observed: {total_dns} event(s).",
            severity="warning",
            evidence={"observed_problem_type": "dns_issue",
                      "by_file": [{"file": f["file"], "dns_failures": f["dns_failures"]}
                                  for f in per_file if f["dns_failures"]]},
        ))
    if total_proxy:
        problem_types.append("proxy_issue")
        findings.append(Finding(
            summary=f"Proxy-related failures observed: {total_proxy} event(s).",
            severity="warning",
            evidence={"observed_problem_type": "proxy_issue",
                      "by_file": [{"file": f["file"], "proxy_issues": f["proxy_issues"]}
                                  for f in per_file if f["proxy_issues"]]},
        ))
    if total_fail and not (total_cert or total_dns or total_proxy):
        problem_types.append("page_load_failure")
        findings.append(Finding(
            summary=f"{total_fail} URL request(s) failed with non-cert/dns errors.",
            severity="warning",
            evidence={"observed_problem_type": "page_load_failure",
                      "error_breakdown": _merge_counts(per_file)},
        ))
    if total_slow:
        problem_types.append("page_slow")
        findings.append(Finding(
            summary=f"{total_slow} URL request(s) slower than {SLOW_REQUEST_THRESHOLD_MS} ms.",
            severity="warning",
            evidence={"observed_problem_type": "page_slow",
                      "top_slow": _merge_top_slow(per_file)},
        ))

    return findings, problem_types


def _merge_counts(per_file: list[dict[str, Any]]) -> dict[str, int]:
    out: Counter[str] = Counter()
    for f in per_file:
        for k, v in (f.get("error_breakdown") or {}).items():
            out[k] += int(v)
    return dict(out.most_common(10))


def _merge_top_slow(per_file: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat = []
    for f in per_file:
        for r in f.get("top_slow_requests") or []:
            flat.append({**r, "_file": f["file"]})
    flat.sort(key=lambda r: r.get("duration_ms") or 0, reverse=True)
    return flat[:MAX_TOP_REQUESTS]


def _root_cause(
    problem_types: Iterable[str], findings: Iterable[Finding] = (),
) -> str | None:
    pts = list(problem_types)
    for problem_type in (
        "kerberos_spn_mismatch", "auth_handshake_loop", "auth_error",
    ):
        if problem_type not in pts:
            continue
        for finding in findings:
            evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
            if (
                evidence.get("observed_problem_type") == problem_type
                and evidence.get("signal_classification") == "supportive"
            ):
                return str(finding.summary)
    if "kerberos_spn_mismatch" in pts:
        return "Kerberos SPN mismatch (SEC_E_WRONG_PRINCIPAL) in net-export"
    if "auth_handshake_loop" in pts:
        return "Windows-auth handshake loop in net-export"
    if "auth_error" in pts:
        return "Windows-auth failure in net-export"
    if "cert_error" in pts:
        return "TLS/cert error chain in net-export"
    if "dns_issue" in pts:
        return "DNS resolution failures in net-export"
    if "proxy_issue" in pts:
        return "Proxy resolution / fallback failures in net-export"
    if "page_load_failure" in pts:
        return "URL request failures in net-export"
    if "page_slow" in pts:
        return "Slow URL requests in net-export"
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyse(ctx: dict[str, Any]) -> SkillResult:
    extra = ctx.get("extra") or {}
    sources = _resolve_sources(extra)

    if not sources:
        # Soft success: tell the operator how to collect it.
        result = SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(
                summary="No net-export files provided. Use edge://net-export "
                        "to capture a session, then pass extra.netlog_paths or extra.folder.",
                severity="info",
            )],
            confidence="low",
            additional_logs_needed=[LogRequest(
                log_kind="netlog",
                why="Required to diagnose proxy, cert, and DNS failures from the browser side.",
                how_to_collect=("Open edge://net-export, click Start Logging To Disk, "
                                "reproduce the issue, then Stop. Provide the resulting .json file."),
                skill=SKILL_ID,
            )],
            evidence_state=EvidenceState.MISSING_REQUIRED_EVIDENCE,
            raw={
                "sources": [],
                "quality": {
                    "capture_complete": False,
                    "sample_sufficient": False,
                    "time_comparable": False,
                    "blocking_gaps": True,
                },
            },
        )
        return result

    max_input_bytes = max(1, min(
        int(extra.get("max_input_bytes", netlog_src.DEFAULT_MAX_INPUT_BYTES)),
        256 * 1024 * 1024,
    ))
    max_events = max(1, min(
        int(extra.get("max_events", netlog_src.DEFAULT_MAX_EVENTS)),
        1_000_000,
    ))
    per_file: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for src in sources:
        file_result = _analyse_one(
            src,
            max_input_bytes=max_input_bytes,
            max_events=max_events,
        )
        if file_result.get("status") == "success":
            per_file.append(file_result)
        else:
            parse_errors.append(file_result)

    findings, problem_types = _findings_from_files(per_file)
    assessed_types, context_assessment = assess_netlog_problem_types(
        problem_types, ctx,
    )
    annotate_netlog_findings(findings, assessed_types)
    relevant_problem_types = list(
        context_assessment.get("relevant_problem_types") or []
    )
    supportive_problem_types = list(
        context_assessment.get("supportive_problem_types") or []
    )
    for pe in parse_errors:
        findings.append(Finding(
            summary=f"NetLog input {pe['file']} is unusable: {pe['status']}.",
            severity="warning",
            evidence={
                "file": pe["file"],
                "status": pe["status"],
                "error": pe.get("error"),
            },
        ))

    candidate_cause = _root_cause(supportive_problem_types, findings)
    supportive = any(
        item.get("signal_classification") == "supportive"
        for item in assessed_types
    )
    confidence = "medium" if candidate_cause and per_file and supportive else "low"
    capture_complete = bool(per_file) and not parse_errors and all(
        bool((item.get("quality") or {}).get("capture_complete"))
        for item in per_file
    )
    time_comparable = bool(per_file) and all(
        bool((item.get("time_model") or {}).get("wall_clock_available"))
        for item in per_file
    )
    graph_closed = bool(per_file) and all(
        bool((item.get("dependency_graph") or {}).get("closed"))
        for item in per_file
    )
    sample_sufficient = sum(item.get("requests_total", 0) for item in per_file) > 0
    blocking_gaps = not capture_complete or not time_comparable
    evidence_gaps: list[EvidenceGap] = []
    if parse_errors:
        evidence_gaps.append(EvidenceGap(
            code="netlog_input_unusable",
            summary="One or more NetLog files could not be analyzed within the configured budget/schema.",
            blocking=True,
            log_kind="netlog",
            skill=SKILL_ID,
            details={"statuses": sorted({item["status"] for item in parse_errors})},
        ))
    if per_file and not capture_complete:
        evidence_gaps.append(EvidenceGap(
            code="netlog_capture_incomplete",
            summary="NetLog contains truncation, invalid events, or unmatched BEGIN/END frames.",
            blocking=True,
            log_kind="netlog",
            skill=SKILL_ID,
        ))
    if per_file and not time_comparable:
        evidence_gaps.append(EvidenceGap(
            code="netlog_time_not_comparable",
            summary="NetLog has no timeTickOffset and cannot be placed on a wall-clock timeline.",
            blocking=True,
            log_kind="netlog",
            skill=SKILL_ID,
        ))

    result = SkillResult(
        skill=SKILL_ID, ok=bool(per_file),
        findings=findings,
        root_cause=None,
        confidence=confidence,
        evidence_state=(
            EvidenceState.INCONCLUSIVE_EVIDENCE if per_file
            else EvidenceState.MISSING_REQUIRED_EVIDENCE
        ),
        evidence_gaps=evidence_gaps,
        raw={
            "sources": [str(p) for p in sources],
            "per_file": per_file,
            "parse_errors": parse_errors,
            "observed_problem_types": problem_types,
            "relevant_problem_types": relevant_problem_types,
            "supportive_problem_types": supportive_problem_types,
            "background_problem_types": context_assessment.get(
                "background_problem_types", []
            ),
            "context_assessment": {
                **context_assessment,
                "assessed_problem_types": assessed_types,
            },
            "candidate_cause": candidate_cause,
            "budgets": {
                "max_input_bytes": max_input_bytes,
                "max_events": max_events,
            },
            "quality": {
                "capture_complete": capture_complete,
                "sample_sufficient": sample_sufficient,
                "time_comparable": time_comparable,
                "dependency_graph_closed": graph_closed,
                "direct_join": False,
                "independent_sources": 1 if per_file else 0,
                "blocking_gaps": blocking_gaps,
                "records_seen": sum(
                    int((item.get("quality") or {}).get("records_seen", 0))
                    for item in per_file
                ),
            },
        },
        error=None if per_file else "No usable NetLog capture was available.",
    )
    # Seed playbook entries for every observed problem type. Always include
    # the caller-supplied problem_type if one was given.
    pb_types = list(supportive_problem_types)
    ctx_pt = ctx.get("problem_type")
    if ctx_pt and ctx_pt not in pb_types:
        pb_types.append(ctx_pt)
    playbook.merge_into_result(result, pb_types)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Edge net-export (netlog) analyser")
    ap.add_argument("context", nargs="?", default=None,
                    help="JSON context string, @file, or path to JSON file. Use extra.netlog_paths or extra.folder.")
    args = ap.parse_args(argv)
    ctx = load_context([args.context] if args.context else [])
    result = analyse(ctx)
    result.emit()
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
