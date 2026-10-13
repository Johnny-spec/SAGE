"""Symptom-grounded relevance grading for noisy diagnostic observations."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


PROBLEM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "5xx_error": ("500", "502", "503", "server error", "http 5", "服务端错误"),
    "high_latency": ("slow", "latency", "timeout", "hang", "慢", "延迟", "超时", "卡顿"),
    "page_slow": ("slow", "latency", "timeout", "慢", "延迟", "超时"),
    "slow_startup": ("startup", "launch", "start slowly", "启动", "打开慢"),
    "hang": ("hang", "freeze", "not responding", "卡死", "无响应"),
    "crash": ("crash", "terminated", "exception", "崩溃", "闪退", "异常退出"),
    "render_process_gone": ("renderer", "render process", "gpu process", "渲染进程"),
    "auth_error": ("auth", "401", "403", "login", "credential", "认证", "登录", "凭据"),
    "auth_handshake_loop": ("401", "auth loop", "negotiate", "ntlm", "认证循环"),
    "kerberos_spn_mismatch": ("kerberos", "spn", "wrong principal", "主体名称"),
    "cert_error": ("certificate", "cert", "tls", "ssl", "证书"),
    "tls_handshake_failure": ("tls", "ssl", "handshake", "握手"),
    "dns_issue": ("dns", "name resolution", "resolve host", "域名", "解析"),
    "proxy_issue": ("proxy", "pac", "代理"),
    "page_load_failure": ("page load", "cannot open", "request failed", "网页", "无法访问", "加载失败"),
    "client_request_failure": ("request failed", "cannot connect", "连接失败", "请求失败"),
}

NETLOG_FALSIFIERS: dict[str, str] = {
    "cert_error": "A repeat capture for the same origin completes TLS successfully with the expected certificate chain.",
    "tls_handshake_failure": "Packet/SChannel evidence shows the TLS handshake completed and the failure occurred after TLS.",
    "dns_issue": "The same hostname resolves successfully through the same resolver and the request still fails.",
    "proxy_issue": "A direct and policy-equivalent request bypassing the suspected proxy decision fails identically.",
    "page_load_failure": "The same URL request succeeds in NetLog while the reported page failure still occurs.",
    "client_request_failure": "The matching request completes successfully while the IIS/client symptom remains.",
    "page_slow": "A healthy baseline shows the same request duration distribution without the reported slowness.",
    "high_latency": "Server/perf evidence attributes latency outside the client request path.",
    "auth_error": "The same principal and origin complete authentication while the symptom persists.",
    "kerberos_spn_mismatch": "SPN ownership is unique/correct and a fresh ticket succeeds for the same origin.",
    "auth_handshake_loop": "A repeat capture sends credentials and completes the challenge while the symptom persists.",
}

NETLOG_NEXT_SOURCE: dict[str, str] = {
    "cert_error": "Certificate chain and SChannel/packet evidence for the same origin",
    "tls_handshake_failure": "SChannel EVTX or packet capture",
    "dns_issue": "DNS client logs and packet capture",
    "proxy_issue": "Effective proxy/PAC policy and packet capture",
    "page_load_failure": "Server logs or packet capture joined to the URL/time",
    "client_request_failure": "IIS/HTTPERR logs joined to the request window",
    "page_slow": "Healthy NetLog baseline and server/perf timing",
    "high_latency": "IIS time-taken, WPR, and perf counters",
    "auth_error": "Security EVTX and server authentication logs",
    "kerberos_spn_mismatch": "setspn output, Kerberos EVTX, and Security 4769",
    "auth_handshake_loop": "Security EVTX and server authentication-provider logs",
}

PROCMON_COMPATIBILITY: dict[str, set[str]] = {
    "5xx_error": {"access_denied", "missing_dependency", "image_load_failure", "lock_contention", "process_restart_loop"},
    "high_latency": {"slow_operation", "lock_contention", "network_failure", "repeated_path_probe"},
    "page_slow": {"slow_operation", "lock_contention", "network_failure"},
    "slow_startup": {"slow_operation", "missing_dependency", "image_load_failure", "repeated_path_probe", "process_restart_loop"},
    "hang": {"slow_operation", "lock_contention", "network_failure", "repeated_path_probe"},
    "crash": {"image_load_failure", "missing_dependency", "process_restart_loop", "access_denied"},
    "render_process_gone": {"image_load_failure", "missing_dependency", "process_restart_loop"},
    "auth_error": {"network_failure", "dc_udp_response_gap"},
    "auth_handshake_loop": {"network_failure", "dc_udp_response_gap"},
    "kerberos_spn_mismatch": {"network_failure", "dc_udp_response_gap"},
    "cert_error": {"network_failure"},
    "tls_handshake_failure": {"network_failure"},
    "dns_issue": {"network_failure"},
    "proxy_issue": {"network_failure", "access_denied"},
    "page_load_failure": {"network_failure", "access_denied", "missing_dependency"},
    "client_request_failure": {"network_failure", "access_denied"},
}

PATTERN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "access_denied": ("access denied", "permission", "unauthorized", "拒绝访问", "权限"),
    "lock_contention": ("lock", "sharing violation", "locked", "锁", "占用"),
    "network_failure": ("network", "connect", "socket", "timeout", "网络", "连接", "超时"),
    "image_load_failure": ("dll", "module", "image load", "模块", "镜像加载"),
    "missing_dependency": ("missing", "not found", "dll", "config", "依赖", "找不到", "缺失"),
    "repeated_path_probe": ("not found", "lookup", "registry", "路径探测", "注册表"),
    "process_restart_loop": ("restart", "relaunch", "exit", "重启", "反复启动", "退出"),
    "slow_operation": ("slow", "latency", "timeout", "io", "慢", "延迟", "超时"),
    "dc_udp_response_gap": ("domain controller", "dc", "ldap", "kerberos", "auth", "认证", "域控"),
}

TARGET_PROCESS_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("iis", "w3wp", "app pool", "asp.net", "500", "503", "5xx"), ("w3wp.exe", "iisexpress.exe", "was.exe")),
    (("edge", "browser", "renderer", "chromium", "chrome", "网页", "浏览器"), ("msedge.exe", "chrome.exe")),
    (("auth", "kerberos", "ntlm", "spn", "401", "认证"), ("lsass.exe", "w3wp.exe", "msedge.exe")),
)


def extract_context(ctx: Mapping[str, Any]) -> dict[str, Any]:
    expected: set[str] = set()
    if ctx.get("problem_type"):
        expected.add(str(ctx["problem_type"]))
    for item in ctx.get("problems") or []:
        if isinstance(item, Mapping) and item.get("type"):
            expected.add(str(item["type"]))
    problem = ctx.get("problem")
    if isinstance(problem, Mapping) and problem.get("type"):
        expected.add(str(problem["type"]))

    text_parts: list[str] = []
    for key in ("question", "summary", "symptom", "title"):
        value = ctx.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    raw_extra = ctx.get("extra")
    extra: Mapping[str, Any] = raw_extra if isinstance(raw_extra, Mapping) else {}
    if isinstance(extra.get("symptoms"), str):
        text_parts.append(str(extra["symptoms"]))
    for item in ctx.get("problems") or []:
        if isinstance(item, Mapping):
            text_parts.extend(str(item.get(key) or "") for key in ("summary", "description"))
    text = " ".join(text_parts).casefold()

    target_processes = {
        str(value).casefold()
        for key in ("process_names", "target_processes")
        for value in (extra.get(key) or [])
    }
    scope_text = " ".join((text, *sorted(expected))).casefold()
    for terms, processes in TARGET_PROCESS_HINTS:
        if any(term in scope_text for term in terms):
            target_processes.update(process.casefold() for process in processes)
    return {
        "expected_problem_types": sorted(expected),
        "symptom_text": text[:4_000],
        "target_processes": sorted(target_processes),
        "scoped": bool(expected or text or target_processes),
    }


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term.casefold() in text for term in terms)


def assess_procmon_patterns(
    patterns: Iterable[Mapping[str, Any]],
    ctx: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = extract_context(ctx)
    expected = set(context["expected_problem_types"])
    symptom_text = str(context["symptom_text"])
    targets = set(context["target_processes"])
    scoped = bool(context["scoped"])
    assessed: list[dict[str, Any]] = []

    for original in patterns:
        item = dict(original)
        pattern_type = str(item.get("type") or "")
        process = str(item.get("process_name") or "").casefold()
        operation_text = " ".join(str(item.get(key) or "") for key in (
            "operation", "result", "path", "operation_family",
        )).casefold()
        score = 0
        reasons: list[str] = []
        target_mismatch = False
        if expected:
            matched_problem_types = sorted(
                problem_type for problem_type in expected
                if pattern_type in PROCMON_COMPATIBILITY.get(problem_type, set())
            )
            if matched_problem_types:
                score += 4
                reasons.append(
                    "Pattern is compatible with reported problem type(s): "
                    + ", ".join(matched_problem_types)
                )
            else:
                reasons.append("Pattern is not a known causal fit for the reported problem type.")
        if symptom_text and _contains_any(
            symptom_text, PATTERN_KEYWORDS.get(pattern_type, ()),
        ):
            score += 3
            reasons.append("Reported symptom text directly matches this pattern class.")
        if targets:
            if process in targets:
                score += 3
                reasons.append(f"Process {process} matches the scoped target process.")
            else:
                target_mismatch = True
                reasons.append(f"Process {process or '-'} is outside the scoped target process set.")
        if symptom_text and operation_text and any(
            token in operation_text for token in re.findall(r"[a-z0-9_.-]{4,}", symptom_text)
        ):
            score += 1
            reasons.append("Operation/path text overlaps the reported symptom context.")
        baseline_ratio = item.get("baseline_ratio")
        if baseline_ratio == "new_in_incident" or (
            isinstance(baseline_ratio, (int, float)) and baseline_ratio >= 3
        ):
            score += 2
            reasons.append("The incident rate is materially above the healthy baseline.")
        if not scoped:
            relevance = "unscoped"
            classification = "contextual"
            noise_risk = "medium"
            reasons.append("No explicit symptom scope was supplied; keep as a contextual observation only.")
        elif target_mismatch:
            relevance = "low"
            classification = "background"
            noise_risk = "high"
        elif score >= 5:
            relevance = "high"
            classification = "supportive"
            noise_risk = "low"
        elif score >= 2:
            relevance = "medium"
            classification = "contextual"
            noise_risk = "medium"
        else:
            relevance = "low"
            classification = "background"
            noise_risk = "high"
        item.update({
            "relevance_score": score,
            "symptom_relevance": relevance,
            "signal_classification": classification,
            "noise_risk": noise_risk,
            "relevance_reasons": reasons,
            "context_problem_types": sorted(expected),
            "context_target_processes": sorted(targets),
        })
        assessed.append(item)

    classification_rank = {"supportive": 3, "contextual": 2, "background": 1}
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    assessed.sort(key=lambda item: (
        classification_rank.get(str(item.get("signal_classification")), 0),
        int(item.get("relevance_score", 0) or 0),
        severity_rank.get(str(item.get("severity")), 0),
        int(item.get("count", 0) or 0),
    ), reverse=True)
    relevant = [
        item for item in assessed
        if item.get("signal_classification") in {"supportive", "contextual"}
    ]
    supportive = [
        item for item in assessed
        if item.get("signal_classification") == "supportive"
    ]
    background = [
        item for item in assessed if item.get("signal_classification") == "background"
    ]
    return assessed, {
        "context": context,
        "relevant_count": len(relevant),
        "background_count": len(background),
        "candidate_pattern": supportive[0] if supportive else None,
        "background_patterns": background[:20],
        "noise_controls": [
            "Static pattern thresholds must be met before relevance grading.",
            "Reported problem type, symptom terms, and target process are scored independently.",
            "Healthy-baseline deltas increase relevance; raw event volume alone does not.",
            "Background patterns remain auditable but are excluded from causal candidates.",
        ],
    }


NETLOG_COMPATIBILITY: dict[str, set[str]] = {
    "page_load_failure": {"page_load_failure", "dns_issue", "proxy_issue", "cert_error"},
    "client_request_failure": {"client_request_failure", "dns_issue", "proxy_issue", "cert_error", "tls_handshake_failure"},
    "page_slow": {"page_slow", "dns_issue", "proxy_issue"},
    "high_latency": {"high_latency", "dns_issue", "proxy_issue"},
    "cert_error": {"cert_error", "tls_handshake_failure"},
    "tls_handshake_failure": {"tls_handshake_failure", "cert_error"},
    "dns_issue": {"dns_issue"},
    "proxy_issue": {"proxy_issue"},
    "auth_error": {"auth_error", "kerberos_spn_mismatch", "auth_handshake_loop"},
    "kerberos_spn_mismatch": {"kerberos_spn_mismatch", "auth_error"},
    "auth_handshake_loop": {"auth_handshake_loop", "auth_error"},
}


def assess_netlog_problem_types(
    observed_problem_types: Iterable[str],
    ctx: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = extract_context(ctx)
    expected = set(context["expected_problem_types"])
    symptom_text = str(context["symptom_text"])
    scoped = bool(context["scoped"])
    assessed: list[dict[str, Any]] = []
    for observed in dict.fromkeys(str(value) for value in observed_problem_types):
        score = 0
        reasons: list[str] = []
        if expected:
            exact = observed in expected
            compatible = sorted(
                expected_type for expected_type in expected
                if observed in NETLOG_COMPATIBILITY.get(expected_type, set())
            )
            if exact:
                score += 5
                reasons.append("Observed NetLog problem exactly matches the reported problem type.")
            elif compatible:
                score += 3
                reasons.append(
                    "Observed NetLog problem is a compatible mechanism for: "
                    + ", ".join(compatible)
                )
            else:
                reasons.append("Observed NetLog problem is unrelated to the reported problem type.")
        if symptom_text and _contains_any(
            symptom_text, PROBLEM_KEYWORDS.get(observed, ()),
        ):
            score += 3
            reasons.append("Reported symptom text matches the observed NetLog problem.")
        if not scoped:
            relevance = "unscoped"
            classification = "contextual"
            noise_risk = "medium"
            reasons.append("No symptom scope was supplied; do not treat the observation as causal proof.")
        elif score >= 5:
            relevance = "high"
            classification = "supportive"
            noise_risk = "low"
        elif score >= 2:
            relevance = "medium"
            classification = "contextual"
            noise_risk = "medium"
        else:
            relevance = "low"
            classification = "background"
            noise_risk = "high"
        assessed.append({
            "problem_type": observed,
            "relevance_score": score,
            "symptom_relevance": relevance,
            "signal_classification": classification,
            "noise_risk": noise_risk,
            "relevance_reasons": reasons,
        })
    assessed.sort(key=lambda item: (
        {"supportive": 3, "contextual": 2, "background": 1}.get(
            str(item.get("signal_classification")), 0
        ),
        int(item.get("relevance_score", 0) or 0),
    ), reverse=True)
    relevant = [
        item for item in assessed
        if item["signal_classification"] in {"supportive", "contextual"}
    ]
    supportive = [
        item for item in assessed
        if item["signal_classification"] == "supportive"
    ]
    background = [item for item in assessed if item["signal_classification"] == "background"]
    return assessed, {
        "context": context,
        "relevant_problem_types": [item["problem_type"] for item in relevant],
        "supportive_problem_types": [item["problem_type"] for item in supportive],
        "background_problem_types": [item["problem_type"] for item in background],
        "noise_controls": [
            "Observed event types are retained even when unrelated to the incident symptom.",
            "Only exact/compatible problem mechanisms or symptom-text matches feed the candidate cause.",
            "Capture completeness and wall-clock comparability remain independent blocking checks.",
            "NetLog correlation demonstrates client-side behavior, not server-side root cause by itself.",
        ],
    }


def annotate_netlog_findings(
    findings: Iterable[Any],
    assessed_problem_types: Iterable[Mapping[str, Any]],
) -> None:
    by_type = {
        str(item.get("problem_type")): item
        for item in assessed_problem_types if item.get("problem_type")
    }
    for finding in findings:
        evidence = getattr(finding, "evidence", None)
        if not isinstance(evidence, dict):
            continue
        problem_type = str(evidence.get("observed_problem_type") or "")
        assessment = by_type.get(problem_type)
        if not assessment:
            continue
        evidence.update({
            "symptom_relevance": assessment.get("symptom_relevance"),
            "signal_classification": assessment.get("signal_classification"),
            "noise_risk": assessment.get("noise_risk"),
            "relevance_score": assessment.get("relevance_score"),
            "relevance_reasons": assessment.get("relevance_reasons"),
            "falsifier": NETLOG_FALSIFIERS.get(problem_type),
            "next_source": NETLOG_NEXT_SOURCE.get(problem_type),
        })
        if assessment.get("signal_classification") == "background":
            finding.severity = "info"
            if not str(finding.summary).startswith("Background NetLog observation"):
                finding.summary = (
                    "Background NetLog observation (not used as causal evidence): "
                    + str(finding.summary)
                )


__all__ = [
    "annotate_netlog_findings", "assess_netlog_problem_types",
    "assess_procmon_patterns", "extract_context",
]