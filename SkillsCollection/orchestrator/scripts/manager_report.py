"""Bounded evidence index and self-contained HTML for the root AI manager."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping

COMMON_ROOT = Path(__file__).resolve().parents[2] / "Common"
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))
from redaction import redact_default  # type: ignore[reportMissingImports]


MAX_ENVELOPES = 80
MAX_EVIDENCE = 120
MAX_CAUSES = 30
MAX_SOLUTIONS = 50

EVIDENCE_FACT_KEYS = (
    "problem_type", "observed_problem_type", "type", "signature",
    "failed_spn", "origin", "failure_count", "security_status",
    "process_name", "pid", "operation", "result", "count",
    "send_count", "receive_count", "receive_ratio",
    "distinct_remote_endpoints", "service_port", "service_name",
    "baseline_count", "baseline_ratio", "status", "event_id",
    "provider", "correlation", "delta_min", "duration_ms",
    "total_requests", "total_failed", "total_slow",
    "symptom_relevance", "signal_classification", "noise_risk",
    "relevance_score", "next_source",
)

CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

SKILL_WORK = {
    "iis_logs": (
        "Parsed IIS W3C requests, measured status/latency patterns, and classified IIS problems.",
        "解析 IIS W3C 请求，统计状态码与延迟模式，并识别 IIS 问题类型。",
    ),
    "ftp_logs": (
        "Parsed FTP W3C activity and reconstructed authentication and transfer sessions.",
        "解析 FTP W3C 活动，并还原认证与文件传输会话。",
    ),
    "httperror": (
        "Analyzed HTTP.SYS HTTPERR failures that are not visible in IIS access logs.",
        "分析 IIS 访问日志中不可见的 HTTP.SYS HTTPERR 失败。",
    ),
    "event_log": (
        "Correlated Windows event records with the incident window and product symptoms.",
        "将 Windows 事件记录与故障时间窗口和产品症状进行关联。",
    ),
    "app_crash": (
        "Analyzed application and .NET crash evidence and classified failure signatures.",
        "分析应用程序与 .NET 崩溃证据，并识别失败特征。",
    ),
    "security_audit": (
        "Examined authentication, authorization, and security-audit evidence.",
        "检查认证、授权和安全审计证据。",
    ),
    "resource_monitor": (
        "Examined CPU, memory, disk, and process resource evidence.",
        "检查 CPU、内存、磁盘和进程资源证据。",
    ),
    "firewall": (
        "Analyzed firewall activity and suspicious network traffic patterns.",
        "分析防火墙活动和可疑网络流量模式。",
    ),
    "netlog": (
        "Analyzed Chromium NetLog for DNS, proxy, TLS, request, and authentication behavior.",
        "分析 Chromium NetLog 中的 DNS、代理、TLS、请求和认证行为。",
    ),
    "procmon": (
        "Analyzed Process Monitor file, Registry, process, image-load, and network operations.",
        "分析 Process Monitor 中的文件、注册表、进程、镜像加载和网络操作。",
    ),
    "edge_diagnostics": (
        "Inspected the Edge environment and classified the reported browser scenario.",
        "检查 Edge 环境并识别所报告的浏览器场景。",
    ),
    "edge_crash": (
        "Analyzed Edge Crashpad/dump metadata and grouped crash signatures.",
        "分析 Edge Crashpad/转储元数据并归类崩溃特征。",
    ),
    "edge_performance": (
        "Examined Edge process CPU, memory, handles, and process roles.",
        "检查 Edge 进程的 CPU、内存、句柄和进程角色。",
    ),
    "edge_network": (
        "Examined Edge-related proxy, DNS, certificate, and connectivity settings.",
        "检查与 Edge 相关的代理、DNS、证书和连接设置。",
    ),
    "edge_policy": (
        "Inspected effective Edge policies and identified settings that could affect the scenario.",
        "检查生效的 Edge 策略，并识别可能影响该场景的设置。",
    ),
    "edge_extensions": (
        "Inspected Edge extensions, installation state, permissions, and policy controls.",
        "检查 Edge 扩展、安装状态、权限和策略控制。",
    ),
    "edge_netlog": (
        "Analyzed Edge NetLog request, DNS, proxy, and TLS event chains.",
        "分析 Edge NetLog 中的请求、DNS、代理和 TLS 事件链。",
    ),
    "edge_procmon": (
        "Analyzed Process Monitor evidence associated with Edge processes.",
        "分析与 Edge 进程相关的 Process Monitor 证据。",
    ),
    "edge_qa": (
        "Applied the Edge knowledge base to the supplied product question.",
        "使用 Edge 知识库处理所提供的产品问题。",
    ),
    "source_logging_discovery": (
        "Inspected source/configuration roots to identify additional diagnostic providers.",
        "检查源代码和配置目录，以识别额外的诊断日志提供程序。",
    ),
    "validator": (
        "Compared independent analyses, reconciled claims, and checked shared evidence references.",
        "比较独立分析、协调结论，并检查共同证据引用。",
    ),
    "manager_report": (
        "Built the bounded, redacted Chinese and English manager reports.",
        "生成有界且脱敏的中英文管理报告。",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: Any, length: int = 16) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return f"{prefix}-{hashlib.sha256(body.encode('ascii')).hexdigest()[:length]}"


def _confidence_level(value: Any, default: str = "low") -> str:
    if isinstance(value, str) and value.casefold() in CONFIDENCE_RANK:
        return value.casefold()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        score = float(value)
        return "high" if score >= 0.75 else "medium" if score >= 0.4 else "low"
    return default


def _max_confidence(values: Iterable[str], default: str = "low") -> str:
    return max(
        values,
        key=lambda item: CONFIDENCE_RANK.get(item, 0),
        default=default,
    )


def _min_confidence(values: Iterable[str], default: str = "low") -> str:
    return min(
        values,
        key=lambda item: CONFIDENCE_RANK.get(item, 0),
        default=default,
    )


def _problem_statement(
    input_summary: Mapping[str, Any],
    products: Iterable[str],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    description = str(input_summary.get("description") or "").strip()
    problems = [
        dict(item) for item in input_summary.get("reported_problems") or []
        if isinstance(item, Mapping)
    ]
    problem_types = [str(item) for item in input_summary.get("problem_types") or []]
    if description:
        confidence = "high"
        source = "explicit_description"
        description_en = (
            description if description.isascii()
            else f"Reported issue (original text): {description}"
        )
        description_zh = (
            description if not description.isascii()
            else f"用户报告的问题（原文）：{description}"
        )
    elif problems or problem_types:
        description = "; ".join(
            str(item.get("summary") or item.get("type") or "") for item in problems
        ) or ", ".join(problem_types)
        confidence = "medium"
        source = "problem_classification"
        description_en = f"Classified issue (technical text): {description}"
        description_zh = f"已识别的问题（技术原文）：{description}"
    else:
        description = (
            f"Analyze the supplied {input_summary.get('mode') or 'incident'} evidence "
            f"for {', '.join(products) or 'an unspecified product'}."
        )
        description_en = description
        description_zh = (
            f"分析已提供的{input_summary.get('mode') or '故障'}证据，"
            f"产品范围为 {', '.join(products) or '尚未指定的产品'}。"
        )
        confidence = "low"
        source = "evidence_scope_inference"
    return {
        "description": description,
        "description_en": description_en,
        "description_zh": description_zh,
        "description_source": source,
        "products": list(products),
        "problem_types": problem_types,
        "reported_problems": problems,
        "time_range": dict(input_summary.get("time_range") or {}),
        "input_mode": input_summary.get("mode"),
        "input_label": input_summary.get("label"),
        "current_evidence_state": status.get("evidence_state"),
        "confidence": confidence,
    }


def _skill_work(node: Mapping[str, Any]) -> dict[str, Any]:
    skill = str(node.get("skill") or "unknown")
    role = str(node.get("role") or "follow_up")
    product = str(node.get("product") or "Manager")
    status = str(node.get("status") or "unknown")
    if role == "ai_manager":
        base = (
            "Routed product scope, coordinated delegates, aggregated evidence, and enforced proof gates.",
            "确定产品范围、协调各产品 delegate、汇总证据并执行根因质量门控。",
        )
    elif role in {"product_orchestrator", "product_router"}:
        base = (
            f"Coordinated {product} evidence discovery, skill dispatch, and product-local result fusion.",
            f"协调 {product} 的证据发现、skill 调度和产品本地结果融合。",
        )
    else:
        base = SKILL_WORK.get(skill, (
            "Executed its registered diagnostic responsibility and returned structured evidence.",
            "执行已注册的诊断职责并返回结构化证据。",
        ))
    output = _as_mapping(node.get("output_summary"))
    evidence_state = str(output.get("evidence_state") or "")
    finding_count = int(output.get("finding_count", 0) or 0)
    reason = str(output.get("reason") or "")
    if status == "skipped":
        detail_en = f"Skipped: {reason or 'required evidence or permission was unavailable.'}"
        detail_zh = f"已跳过：{reason or '缺少必要证据或权限。'}"
        confidence = "low"
    elif status in {"failed", "timeout"}:
        detail_en = f"Execution ended with status {status}; its absence must not be treated as negative evidence."
        detail_zh = f"执行状态为 {status}；不能将缺少结果视为否定证据。"
        confidence = "low"
    else:
        detail_en = (
            f"Returned {finding_count} finding(s)"
            + (f" with evidence state {evidence_state}." if evidence_state else ".")
        )
        detail_zh = (
            f"返回 {finding_count} 条发现"
            + (f"，证据状态为 {evidence_state}。" if evidence_state else "。")
        )
        confidence = (
            "high" if evidence_state == "ROOT_CAUSE_FOUND"
            else "medium" if finding_count > 0 else "low"
        )
    return {
        **dict(node),
        "work_summary_en": f"{base[0]} {detail_en}",
        "work_summary_zh": f"{base[1]} {detail_zh}",
        "confidence": confidence,
    }


def _cross_validation_summary(
    manager_validation: Mapping[str, Any] | None,
    validation_rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    cross = (
        manager_validation.get("cross_validation")
        if isinstance(manager_validation, Mapping) else None
    )
    cross = _as_mapping(cross)
    consensus = _as_mapping(cross.get("consensus"))
    score = float(consensus.get("consensus_confidence", 0.0) or 0.0)
    similarities = []
    for item in consensus.get("agreed_claims") or []:
        if not isinstance(item, Mapping):
            continue
        similarities.append({
            "claim_key": item.get("claim_key"),
            "conclusion": item.get("conclusion"),
            "analyzers": list(item.get("analyzers") or []),
            "evidence_refs": list(item.get("evidence_refs") or []),
            "common_evidence_refs": list(item.get("common_evidence_refs") or []),
            "confidence_score": float(item.get("confidence", 0.0) or 0.0),
            "confidence": _confidence_level(item.get("confidence")),
        })
    differences = []
    for item in consensus.get("conflicting_findings") or []:
        if not isinstance(item, Mapping):
            continue
        claims = [
            {
                "analyzer": claim.get("analyzer"),
                "conclusion": claim.get("conclusion"),
                "claim_key": claim.get("claim_key"),
                "evidence_refs": list(claim.get("evidence_refs") or []),
                "confidence_score": float(claim.get("confidence", 0.0) or 0.0),
                "confidence": _confidence_level(claim.get("confidence")),
            }
            for claim in item.get("claims") or [] if isinstance(claim, Mapping)
        ]
        differences.append({
            "problem_type": item.get("problem_type"),
            "reason": item.get("reason"),
            "claims": claims,
            "minimum_conclusion_similarity": item.get("minimum_conclusion_similarity"),
            "confidence": "medium" if claims else "low",
        })
    partial = []
    for item in consensus.get("partial_agreement") or []:
        if not isinstance(item, Mapping):
            continue
        partial.append({
            "claim_key": item.get("claim_key"),
            "conclusion": item.get("conclusion"),
            "reason": item.get("reason"),
            "analyzers": list(item.get("analyzers") or []),
            "evidence_refs": list(item.get("evidence_refs") or []),
            "confidence_score": float(item.get("confidence", 0.0) or 0.0),
            "confidence": _confidence_level(item.get("confidence")),
        })
    rounds = []
    for item in validation_rounds:
        round_number = int(item.get("round", 0) or 0)
        status = str(item.get("status") or "unknown")
        confidence = (
            _confidence_level(score) if round_number == 2
            else "high" if round_number == 3 and status == "completed"
            else "medium" if status == "completed" else "low"
        )
        rounds.append({**dict(item), "confidence": confidence})
    round_two = next(
        (item for item in validation_rounds if item.get("round") == 2), {}
    )
    status = str(
        cross.get("status")
        or consensus.get("status")
        or ("skipped" if round_two.get("status") == "skipped" else "not_configured")
    )
    return {
        "status": status,
        "confidence_score": score,
        "confidence": _confidence_level(score),
        "agreement_ratio": float(consensus.get("agreement_ratio", 0.0) or 0.0),
        "provider_count": int(consensus.get("provider_count", 0) or 0),
        "completed_provider_count": int(consensus.get("completed_provider_count", 0) or 0),
        "independent_analysis_count": int(consensus.get("independent_analysis_count", 0) or 0),
        "diversity": dict(consensus.get("diversity") or {}),
        "similarities": similarities,
        "differences": differences,
        "partial_agreements": partial,
        "rounds": rounds,
        "round_count": len(rounds),
    }


def _investigation_process(
    stages: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    invocation_nodes: list[dict[str, Any]],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    stage_text = {
        "route": (
            "Determine whether the incident belongs to IIS, Edge, or both.",
            "判断故障属于 IIS、Edge，还是同时涉及两个产品。",
        ),
        "inventory": (
            "Inventory supplied evidence using bounded, non-destructive discovery.",
            "通过有界且非破坏性的方式盘点已提供证据。",
        ),
        "delegate": (
            "Run product-owned analyzers and retain their findings, provenance, and gaps.",
            "运行产品负责的分析器，并保留其发现、来源信息和证据缺口。",
        ),
        "validate": (
            "Compare independent analyses and reconcile agreements and conflicts.",
            "比较独立分析，并协调一致点和冲突点。",
        ),
        "gap": (
            "Evaluate blocking evidence gaps and construct the collection plan.",
            "评估阻塞性证据缺口并生成日志收集计划。",
        ),
        "report": (
            "Apply evidence-reference and root-quality checks, then generate both reports.",
            "执行证据引用和根因质量检查，然后生成中英文报告。",
        ),
    }
    all_refs = [item["id"] for item in evidence if _is_claimable_evidence(item)]
    skill_ids = [str(item.get("id")) for item in invocation_nodes if item.get("id")]
    validation_confidence = str(validation.get("confidence") or "low")
    steps = []
    for sequence, item in enumerate(stages, start=1):
        stage_id = str(item.get("id") or item.get("name") or f"stage-{sequence}")
        text = stage_text.get(stage_id, (
            str(item.get("name") or stage_id), str(item.get("name") or stage_id),
        ))
        if stage_id == "validate":
            confidence = validation_confidence
        elif stage_id == "delegate":
            confidence = "medium" if all_refs else "low"
        elif stage_id == "report":
            confidence = "high" if item.get("status") == "completed" else "low"
        else:
            confidence = "medium" if item.get("status") == "completed" else "low"
        steps.append({
            "sequence": sequence,
            "id": stage_id,
            "status": item.get("status"),
            "title_en": str(item.get("name") or stage_id).replace("_", " ").title(),
            "title_zh": {
                "route": "产品路由", "inventory": "证据盘点", "delegate": "产品分析",
                "validate": "交叉验证", "gap": "证据缺口检查", "report": "报告生成",
            }.get(stage_id, str(item.get("name") or stage_id)),
            "description_en": text[0],
            "description_zh": text[1],
            "notes": list(item.get("notes") or []),
            "evidence_refs": all_refs[:20] if stage_id in {"delegate", "validate"} else [],
            "skill_refs": skill_ids[:40] if stage_id in {"delegate", "validate", "report"} else [],
            "confidence": confidence,
        })
    return {
        "summary_en": "The manager routed the incident, inventoried evidence, delegated product analysis, validated claims, checked gaps, and generated the final reports.",
        "summary_zh": "管理器完成产品路由、证据盘点、产品分析调度、结论验证、证据缺口检查和最终报告生成。",
        "confidence": _min_confidence([step["confidence"] for step in steps]),
        "steps": steps,
    }


def _next_actions(
    next_steps: Iterable[Mapping[str, Any]],
    missing_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in next_steps:
        if not isinstance(item, Mapping):
            continue
        action = str(item.get("action") or "").strip()
        product = str(item.get("_source_product") or "Manager")
        skill = str(item.get("skill") or "")
        key = ("action", product, action.casefold())
        if not action or key in seen:
            continue
        seen.add(key)
        output.append({
            "type": "investigation",
            "product": product,
            "action": action,
            "action_en": action,
            "action_zh": action,
            "why": str(item.get("why") or "Continue the evidence-based investigation."),
            "skill": skill or None,
            "priority": "normal",
            "confidence": "medium" if skill else "low",
        })
    for item in missing_logs:
        log_kind = str(item.get("log_kind") or "")
        product = str(item.get("product") or "Manager")
        key = ("log", product, log_kind.casefold())
        if not log_kind or key in seen:
            continue
        seen.add(key)
        output.append({
            "type": "log_collection",
            "product": product,
            "action": f"Collect {log_kind}",
            "action_en": f"Collect {log_kind}",
            "action_zh": f"收集 {log_kind}",
            "why": str(item.get("why") or "Required to close an evidence gap."),
            "skill": item.get("consumer_skill"),
            "log_kind": log_kind,
            "how_to_collect": item.get("how_to_collect"),
            "priority": "high" if item.get("blocking") else "normal",
            "confidence": "high" if item.get("blocking") else "medium",
        })
    output.sort(key=lambda item: (
        item["priority"] != "high", item["type"], item["product"],
    ))
    for index, item in enumerate(output, start=1):
        item["sequence"] = index
    return output[:80]


def _evidence_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    display = _as_mapping(item.get("display"))
    return {
        "id": item.get("id"),
        "headline": display.get("headline") or item.get("summary"),
        "product": item.get("product"),
        "skill": item.get("skill"),
        "source": display.get("source"),
        "record": display.get("record"),
        "time": display.get("time"),
        "facts": list(display.get("facts") or [])[:8],
        "symptom_relevance": display.get("symptom_relevance"),
        "signal_classification": display.get("signal_classification"),
        "noise_risk": display.get("noise_risk"),
    }


def _is_claimable_evidence(item: Mapping[str, Any]) -> bool:
    display = _as_mapping(item.get("display"))
    classification = str(display.get("signal_classification") or "").casefold()
    if classification in {"background", "contextual"}:
        return False
    if classification == "supportive":
        return True
    evidence = _as_mapping(item.get("evidence"))
    source_text = " ".join((
        str(item.get("skill") or ""),
        str(item.get("source_ref") or ""),
        str(evidence.get("_from") or ""),
    )).casefold()
    return "procmon" not in source_text and "netlog" not in source_text


def _engineering_assessment(
    *,
    status: Mapping[str, Any],
    evidence: list[dict[str, Any]],
    causes: list[dict[str, Any]],
    validation: Mapping[str, Any],
    evidence_gaps: list[dict[str, Any]],
    next_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    relevant_evidence = [item for item in evidence if _is_claimable_evidence(item)]
    background_evidence = [
        item for item in evidence
        if (item.get("display") or {}).get("signal_classification") == "background"
    ]
    complete_causes = [
        item for item in causes if item.get("status") != "unsupported"
    ]
    leading = complete_causes[0] if complete_causes else None
    leading_refs = set((leading or {}).get("evidence_refs") or [])
    alternatives = [
        {
            "cause": item.get("summary"),
            "status": item.get("status"),
            "confidence": item.get("confidence"),
            "evidence_refs": list(item.get("evidence_refs") or []),
        }
        for item in complete_causes[1:6]
    ]
    for difference in validation.get("differences") or []:
        for claim in difference.get("claims") or []:
            conclusion = str(claim.get("conclusion") or "").strip()
            if conclusion and all(item.get("cause") != conclusion for item in alternatives):
                alternatives.append({
                    "cause": conclusion,
                    "status": "validation_difference",
                    "confidence": claim.get("confidence") or "low",
                    "evidence_refs": list(claim.get("evidence_refs") or []),
                })
    counterpoints: list[dict[str, Any]] = []
    if leading:
        counterpoints.extend({
            "type": "falsifier",
            "summary": str(value),
            "confidence": "medium",
        } for value in leading.get("falsifiers") or [])
    counterpoints.extend({
        "type": "validation_difference",
        "summary": str(item.get("reason") or "Independent analyses disagree."),
        "claims": list(item.get("claims") or []),
        "confidence": item.get("confidence") or "medium",
    } for item in validation.get("differences") or [])
    blocking_gaps = [
        gap for gap in evidence_gaps
        if isinstance(gap, Mapping) and gap.get("blocking")
    ]
    counterpoints.extend({
        "type": "evidence_gap",
        "summary": str(gap.get("summary") or gap.get("code")),
        "confidence": "high",
    } for gap in blocking_gaps[:10])
    noise_sources: dict[str, dict[str, int]] = {}
    for item in evidence:
        skill = str(item.get("skill") or "")
        family = (
            "procmon" if "procmon" in skill
            else "netlog" if "netlog" in skill
            else "other"
        )
        counts = noise_sources.setdefault(
            family,
            {"supportive": 0, "contextual": 0, "background": 0, "ungraded": 0},
        )
        classification = str(
            (item.get("display") or {}).get("signal_classification") or "ungraded"
        )
        counts[classification if classification in counts else "ungraded"] += 1
    root_cause = str(status.get("root_cause") or "").strip()
    if root_cause:
        position = "proven"
        summary_en = f"The evidence-quality gate supports the root cause: {root_cause}"
        summary_zh = f"证据质量门已支持该根本原因：{root_cause}"
    elif leading:
        position = "supported_hypothesis"
        leading_en = str(leading.get("summary_en") or leading.get("summary") or "").rstrip(
            ".!?。！？"
        )
        leading_zh = str(leading.get("summary_zh") or leading.get("summary") or "").rstrip(
            ".!?。！？"
        )
        summary_en = (
            f"The leading evidence-supported hypothesis is: {leading_en}. "
            "It remains a hypothesis until the listed falsifiers and evidence gaps are resolved."
        )
        summary_zh = (
            f"当前证据最支持的假设是：{leading_zh}。"
            "在完成反证检查并补齐证据缺口前，该结论仍不能视为根本原因。"
        )
    else:
        position = "inconclusive"
        summary_en = (
            "No cause currently has complete, symptom-aligned evidence. "
            "Background observations must not be promoted by event volume alone."
        )
        summary_zh = (
            "当前没有任何原因具备完整且与问题现象一致的证据。"
            "不能仅因为事件数量较多就把背景观察提升为原因。"
        )
    confidence = _min_confidence([
        str(status.get("confidence") or "low"),
        str(validation.get("confidence") or "low"),
        str((leading or {}).get("confidence") or "low"),
    ])
    recommended = next_actions[0] if next_actions else None
    return {
        "position": position,
        "summary_en": summary_en,
        "summary_zh": summary_zh,
        "leading_hypothesis": leading,
        "supporting_evidence": [
            _evidence_projection(item)
            for item in relevant_evidence
            if not leading_refs or item.get("id") in leading_refs
        ],
        "counter_evidence_and_uncertainty": counterpoints[:20],
        "alternative_explanations": alternatives[:10],
        "noise_assessment": {
            "by_source_family": noise_sources,
            "background_evidence_count": len(background_evidence),
            "background_examples": [
                _evidence_projection(item) for item in background_evidence[:6]
            ],
            "principles_en": [
                "Event volume is not causality.",
                "Temporal co-occurrence is weaker than a request/process/source join.",
                "Procmon and NetLog background observations remain visible but cannot drive the candidate cause.",
                "A missing event is meaningful only when acquisition completeness is proven.",
            ],
            "principles_zh": [
                "事件数量不等于因果关系。",
                "时间上的同时发生弱于请求、进程或来源的精确关联。",
                "Procmon 和 NetLog 的背景观察继续保留，但不能驱动候选原因。",
                "只有在确认采集完整时，事件缺失才具有证明意义。",
            ],
        },
        "recommended_next_move": (
            dict(recommended) if isinstance(recommended, Mapping) else None
        ),
        "confidence": confidence,
    }


def _quality(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
    quality = raw.get("quality") if isinstance(raw, Mapping) else {}
    quality = quality if isinstance(quality, Mapping) else {}
    return {
        "direct_join": quality.get("direct_join") is True,
        "independent_sources": int(quality.get("independent_sources", 0) or 0),
        "sample_sufficient": quality.get("sample_sufficient") is True,
        "capture_complete": quality.get("capture_complete") is True,
        "time_comparable": quality.get("time_comparable") is True,
        "blocking_gaps": quality.get("blocking_gaps") is not False,
    }


def _bounded_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item) for item in list(value)[:5]]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(child)
            for key, child in list(value.items())[:6]
        }
    return str(value)[:240]


def _evidence_display(
    *,
    evidence_id: str,
    product: str,
    skill: str,
    summary: str,
    severity: str,
    evidence: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    source_info = provenance.get("source") if isinstance(provenance, Mapping) else None
    source = source_info.get("value") if isinstance(source_info, Mapping) else None
    if source in (None, "", [], {}):
        for key in ("source_file", "file", "source", "path"):
            if evidence.get(key) not in (None, "", [], {}):
                source = evidence.get(key)
                break
    record = provenance.get("record") if isinstance(provenance, Mapping) else None
    record = dict(record) if isinstance(record, Mapping) else {
        key: evidence[key]
        for key in ("line_number", "record_id", "event_id", "source_id", "pid")
        if evidence.get(key) not in (None, "", [], {})
    }
    time_info = provenance.get("time") if isinstance(provenance, Mapping) else None
    observed_time = time_info.get("value") if isinstance(time_info, Mapping) else None
    if observed_time in (None, ""):
        observed_time = next((
            evidence.get(key)
            for key in ("normalized_time_utc", "timestamp", "time")
            if evidence.get(key) not in (None, "")
        ), None)
    facts = [
        {"name": key, "value": _bounded_value(evidence.get(key))}
        for key in EVIDENCE_FACT_KEYS
        if evidence.get(key) not in (None, "", [], {})
    ][:12]
    return {
        "id": evidence_id,
        "headline": summary,
        "product": product,
        "skill": skill,
        "severity": severity,
        "source": _bounded_value(source),
        "record": _bounded_value(record),
        "time": _bounded_value(observed_time),
        "facts": facts,
        "symptom_relevance": evidence.get("symptom_relevance"),
        "signal_classification": evidence.get("signal_classification"),
        "noise_risk": evidence.get("noise_risk"),
        "relevance_reasons": [
            str(value)[:300] for value in evidence.get("relevance_reasons") or []
        ][:6],
    }


def _iter_envelopes(
    products: Mapping[str, Mapping[str, Any]],
    manager_validation: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(product: str, ref: str, result: Any) -> None:
        if not isinstance(result, Mapping) or id(result) in seen or len(output) >= MAX_ENVELOPES:
            return
        seen.add(id(result))
        output.append({"product": product, "ref": ref, "skill": str(result.get("skill") or ref), "result": result})

    for product, result in products.items():
        raw = result.get("raw") if isinstance(result, Mapping) else None
        if isinstance(raw, Mapping):
            for field in ("results", "children"):
                children = raw.get(field)
                if isinstance(children, Mapping):
                    for skill, child in children.items():
                        add(product, f"{product}/{skill}", child)
            secondary = raw.get("secondary")
            secondary_raw = secondary.get("raw") if isinstance(secondary, Mapping) else None
            secondary_results = secondary_raw.get("results") if isinstance(secondary_raw, Mapping) else None
            if isinstance(secondary_results, Mapping):
                for skill, child in secondary_results.items():
                    add(product, f"{product}/secondary/{skill}", child)
            if isinstance(raw.get("validation"), Mapping):
                add(product, f"{product}/validator", raw["validation"])
        add(product, f"{product}/orchestrator", result)
    if isinstance(manager_validation, Mapping):
        add("Manager", "Manager/validator", manager_validation)
    return output


def _evidence_index(envelopes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_semantic: set[str] = set()
    for envelope in envelopes:
        result = envelope["result"]
        for finding in result.get("findings") or []:
            if not isinstance(finding, Mapping) or len(items) >= MAX_EVIDENCE:
                continue
            evidence = _as_mapping(finding.get("evidence"))
            summary = str(finding.get("summary") or "")[:700]
            normalized_summary = summary.strip()
            if normalized_summary.startswith("[") and "]" in normalized_summary[:100]:
                normalized_summary = normalized_summary.split("]", 1)[1].strip()
            semantic_key = _stable_id("SEM", {
                "product": envelope["product"],
                "summary": normalized_summary,
                "evidence": {
                    key: value for key, value in evidence.items() if key != "_from"
                },
            }, 32)
            evidence_id = str(finding.get("evidence_id") or _stable_id("EV", {
                "ref": envelope["ref"], "summary": finding.get("summary"), "evidence": evidence,
            }, 20))
            if evidence_id in seen_ids or semantic_key in seen_semantic:
                continue
            seen_ids.add(evidence_id)
            seen_semantic.add(semantic_key)
            provenance = dict(finding.get("provenance") or {})
            severity = str(finding.get("severity") or "info")
            items.append({
                "id": evidence_id,
                "product": envelope["product"],
                "source_ref": envelope["ref"],
                "skill": envelope["skill"],
                "summary": summary,
                "severity": severity,
                "evidence": dict(evidence),
                "provenance": provenance,
                "quality": _quality(result),
                "falsifiers": [str(evidence["falsifier"])] if evidence.get("falsifier") else [],
                "display": _evidence_display(
                    evidence_id=evidence_id,
                    product=envelope["product"],
                    skill=envelope["skill"],
                    summary=summary,
                    severity=severity,
                    evidence=evidence,
                    provenance=provenance,
                ),
            })
    return items


def _refs(
    evidence: list[dict[str, Any]],
    *,
    ref: str | None = None,
    product: str | None = None,
    include_background: bool = False,
) -> list[str]:
    return [
        item["id"] for item in evidence
        if (ref is None or item["source_ref"] == ref)
        and (product is None or item["product"] == product)
        and (
            include_background
            or _is_claimable_evidence(item)
        )
    ][:10]


def _cross_source_auth_cause(
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    claimable = [item for item in evidence if _is_claimable_evidence(item)]
    auth_item = next((
        item for item in claimable
        if str((item.get("evidence") or {}).get("observed_problem_type") or "")
        in {"auth_error", "auth_handshake_loop", "kerberos_spn_mismatch"}
        and "SEC_E_" in str(item.get("summary") or "")
    ), None)
    dc_item = next((
        item for item in claimable
        if str((item.get("evidence") or {}).get("type") or "")
        == "dc_udp_response_gap"
    ), None)
    if auth_item is None or dc_item is None:
        return None

    auth = auth_item.get("evidence") or {}
    dc = dc_item.get("evidence") or {}
    failed_spn = str(auth.get("failed_spn") or "the target HTTP SPN")
    origin = str(auth.get("origin") or "the affected origin")
    failure_count = int(auth.get("failure_count") or auth.get("failed_spn_count") or 0)
    send_count = int(dc.get("send_count") or dc.get("count") or 0)
    receive_count = int(dc.get("receive_count") or 0)
    endpoint_count = int(dc.get("distinct_remote_endpoints") or 0)
    refs = [str(auth_item["id"]), str(dc_item["id"])]
    falsifiers = sorted({
        str(value)
        for item in (auth_item, dc_item)
        for value in item.get("falsifiers") or []
        if value
    })
    summary = (
        "Windows authentication encountered an apparent domain-controller "
        f"response-path failure: NetLog recorded {failure_count} SSPI failure(s) "
        f"for {failed_spn} at {origin}, while Procmon observed lsass.exe sending "
        f"{send_count} request(s) to UDP/{dc.get('service_port') or 389} across "
        f"{endpoint_count} LDAP/DC endpoint(s) but only {receive_count} receive "
        "event(s). Packet capture and Security/Netlogon evidence are still "
        "required to distinguish network/DC response failure from SPN or capture-filter effects."
    )
    summary_zh = (
        "Windows 身份验证过程中出现疑似域控制器响应路径异常："
        f"NetLog 记录了针对 {failed_spn}（源站 {origin}）的 {failure_count} 次 SSPI 失败；"
        f"同时，Procmon 显示 lsass.exe 向 {endpoint_count} 个 LDAP/DC 端点发送了 "
        f"{send_count} 个 UDP/{dc.get('service_port') or 389} 请求，但仅观察到 "
        f"{receive_count} 个接收事件。仍需通过网络数据包、Security 和 Netlogon 证据，"
        "区分网络或域控制器响应异常、SPN 配置问题以及采集过滤造成的表象。"
    )
    return {
        "id": _stable_id("CAUSE", {"kind": "auth_dc_response_path", "refs": refs}),
        "label": "Domain-controller response-path failure during Windows authentication",
        "label_en": "Domain-controller response-path failure during Windows authentication",
        "label_zh": "Windows 身份验证期间的域控制器响应路径异常",
        "summary": summary,
        "summary_en": summary,
        "summary_zh": summary_zh,
        "status": "supported",
        "claimed_status": "supported",
        "confidence": "medium",
        "evidence_refs": refs,
        "falsifiers": falsifiers,
        "evidence_complete": bool(refs and falsifiers),
        "supporting_products": sorted({
            str(auth_item.get("product") or ""), str(dc_item.get("product") or ""),
        } - {""}),
        "supporting_skills": sorted({
            str(auth_item.get("skill") or ""), str(dc_item.get("skill") or ""),
        } - {""}),
    }


def _causes(envelopes: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    synthesized = _cross_source_auth_cause(evidence)
    synthesized_refs = set(synthesized.get("evidence_refs") or []) if synthesized else set()
    if synthesized:
        output.append(synthesized)
        seen.add(str(synthesized["summary"]).casefold())
    for envelope in envelopes:
        result = envelope["result"]
        raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
        candidates: list[tuple[Any, str]] = []
        if result.get("root_cause"):
            candidates.append((result["root_cause"], "proven" if result.get("evidence_state") == "ROOT_CAUSE_FOUND" else "candidate"))
        if isinstance(raw, Mapping) and raw.get("candidate_cause"):
            candidates.append((raw["candidate_cause"], "candidate"))
        diagnosis = raw.get("diagnosis") if isinstance(raw, Mapping) else None
        for item in (diagnosis.get("possible_causes") or []) if isinstance(diagnosis, Mapping) else []:
            candidates.append(((item.get("cause") or item.get("summary")) if isinstance(item, Mapping) else item, "supported"))
        for value, status in candidates:
            summary = str(value or "").strip()
            if not summary or summary.casefold() in seen or len(output) >= MAX_CAUSES:
                continue
            seen.add(summary.casefold())
            refs = _refs(evidence, ref=envelope["ref"])
            if synthesized_refs and refs and set(refs) <= synthesized_refs:
                continue
            falsifiers = sorted({
                f for item in evidence if item["id"] in refs
                for f in item.get("falsifiers") or []
            })
            effective_status = status if refs and falsifiers else "unsupported"
            output.append({
                "id": _stable_id("CAUSE", {"product": envelope["product"], "summary": summary}),
                "label": summary[:160], "summary": summary[:1000], "status": effective_status,
                "claimed_status": status,
                "confidence": str(result.get("confidence") or "low"), "evidence_refs": refs,
                "falsifiers": falsifiers,
                "evidence_complete": bool(refs and falsifiers),
                "supporting_products": [envelope["product"]], "supporting_skills": [envelope["skill"]],
            })
    return output


def _verification(problem_ref: str | None) -> dict[str, str]:
    metrics = {
        "5xx_error": "5xx rate and matching error signature no longer recur",
        "high_latency": "p95/p99 latency returns to the healthy baseline",
        "auth_error": "authentication succeeds without repeated challenge/failure",
        "crash": "no new matching crash signature appears",
        "hang": "the target workflow completes without a hung process",
        "slow_startup": "startup duration returns to the healthy baseline",
    }
    return {
        "metric": metrics.get(problem_ref or "", "the original symptom no longer reproduces"),
        "observation_window": "Repeat the same workload for an equivalent incident window.",
        "rollback": "Reverse the change if the metric regresses or new errors appear.",
    }


def _solutions(
    products: Mapping[str, Mapping[str, Any]],
    evidence: list[dict[str, Any]],
    manager_solutions: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    sources: list[tuple[str, Mapping[str, Any]]] = [
        (product, solution)
        for product, result in products.items()
        for solution in result.get("solutions") or []
        if isinstance(solution, Mapping)
    ]
    sources.extend(
        (str(solution.get("_source_product") or "Manager"), solution)
        for solution in manager_solutions if isinstance(solution, Mapping)
    )
    for product, solution in sources:
        product_evidence = [
            item for item in evidence
            if item["product"] == product
            and _is_claimable_evidence(item)
        ]
        if len(output) >= MAX_SOLUTIONS:
            break
        title = str(solution.get("title") or "").strip()
        problem_ref = str(solution.get("problem_ref") or "") or None
        key = (problem_ref or "", title.casefold())
        if not title or key in seen:
            continue
        seen.add(key)
        direct = [item["id"] for item in product_evidence if str(
            (item.get("evidence") or {}).get("problem_type") or
            (item.get("evidence") or {}).get("observed_problem_type") or
            (item.get("evidence") or {}).get("type") or ""
        ) == (problem_ref or "")][:8]
        refs = direct or [item["id"] for item in product_evidence[:5]]
        output.append({
            "id": _stable_id("SOLUTION", {"product": product, "title": title}),
            "product": product, "title": title, "problem_ref": problem_ref,
            "severity": str(solution.get("severity") or "info"),
            "steps": [str(step) for step in solution.get("steps") or []][:20],
            "references": [str(ref) for ref in solution.get("references") or []][:20],
            "evidence_refs": refs,
            "evidence_relation": "direct_problem_match" if direct else "contextual" if refs else "unlinked",
            "confidence": (
                "high" if direct and len(refs) >= 2
                else "medium" if refs else "low"
            ),
            "verification": _verification(problem_ref),
        })
    return output


def _missing_logs(
    products: Mapping[str, Mapping[str, Any]],
    manager_requests: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for product, result in products.items():
        raw = _as_mapping(result.get("raw"))
        declared = raw.get("missing_logs") if isinstance(raw, Mapping) else []
        missing_keys = {(product, str(item.get("log_kind") or ""), str(item.get("skill") or "")) for item in declared or [] if isinstance(item, Mapping)}
        for request in list(result.get("additional_logs_needed") or []) + list(declared or []):
            if not isinstance(request, Mapping):
                continue
            key = (product, str(request.get("log_kind") or ""), str(request.get("skill") or ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            output.append({
                "product": product, "log_kind": key[1], "why": str(request.get("why") or ""),
                "how_to_collect": request.get("how_to_collect"), "consumer_skill": request.get("skill"),
                "blocking": key in missing_keys, "status": "missing" if key in missing_keys else "requested",
                "confidence": "high" if key in missing_keys else "medium",
                "gap_refs": [f"GAP-{product}-{key[1]}"],
            })
    for request in manager_requests:
        if not isinstance(request, Mapping):
            continue
        product = str(request.get("_source_product") or "Manager")
        key = (product, str(request.get("log_kind") or ""), str(request.get("skill") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        output.append({
            "product": product, "log_kind": key[1],
            "why": str(request.get("why") or ""),
            "how_to_collect": request.get("how_to_collect"),
            "consumer_skill": request.get("skill"), "blocking": True,
            "status": "missing", "confidence": "high",
            "gap_refs": [f"GAP-{product}-{key[1]}"],
        })
    return output[:50]


def _inventory(
    products: Mapping[str, Mapping[str, Any]],
    routing_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for product, result in products.items():
        raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
        discovery = raw.get("discovery") if isinstance(raw, Mapping) else None
        if isinstance(discovery, Mapping):
            for kind, paths in discovery.items():
                values = list(paths) if isinstance(paths, list) else []
                sources.append({
                    "id": _stable_id("SRC", {"product": product, "kind": kind}),
                    "product": product, "kind": str(kind), "count": len(values),
                    "sample_files": [str(value) for value in values[:5]], "time_model": None,
                })
        cross = raw.get("cross_source_context") if isinstance(raw, Mapping) else None
        for kind in (cross.get("sources_used") or []) if isinstance(cross, Mapping) else []:
            if not any(item["product"] == product and item["kind"] == kind for item in sources):
                sources.append({"id": _stable_id("SRC", {"product": product, "kind": kind}), "product": product, "kind": str(kind), "count": 1, "sample_files": [], "time_model": None})
    kinds = routing_inventory.get("kinds") if isinstance(routing_inventory, Mapping) else None
    if isinstance(kinds, Mapping):
        for kind, paths in kinds.items():
            values = list(paths) if isinstance(paths, list) else []
            if any(item["kind"] == str(kind) for item in sources):
                continue
            product = (
                "IIS" if str(kind).startswith("iis_") or str(kind) == "procmon_iis"
                else "Edge" if str(kind).startswith("edge_") or str(kind) == "procmon_edge"
                else "Manager"
            )
            sources.append({
                "id": _stable_id("SRC", {"product": product, "kind": kind}),
                "product": product, "kind": str(kind), "count": len(values),
                "sample_files": [str(value) for value in values[:5]],
                "time_model": None,
            })
    return {"sources": sources[:100], "bounded": True}


def build_manager_report(*, report_id: str, products: Mapping[str, Mapping[str, Any]], status: Mapping[str, Any], routing: Mapping[str, Any], input_summary: Mapping[str, Any], invocation_nodes: list[dict[str, Any]], validation_rounds: list[dict[str, Any]], root_quality: Mapping[str, Any], evidence_gaps: list[dict[str, Any]], collection_plan: Mapping[str, Any] | None, next_steps: list[dict[str, Any]], manager_solutions: Iterable[Mapping[str, Any]] = (), manager_log_requests: Iterable[Mapping[str, Any]] = (), manager_validation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    envelopes = _iter_envelopes(products, manager_validation)
    evidence = _evidence_index(envelopes)
    evidence_ids = {item["id"] for item in evidence}
    causes = _causes(envelopes, evidence)
    solutions = _solutions(products, evidence, manager_solutions)
    missing_logs = _missing_logs(products, manager_log_requests)
    validation = _cross_validation_summary(manager_validation, validation_rounds)
    causal_refs = {
        str(ref)
        for item in causes
        for ref in item.get("evidence_refs") or []
    }
    claimable_evidence = [
        item for item in evidence
        if (
            str(item.get("id")) in causal_refs
            if causal_refs else _is_claimable_evidence(item)
        )
    ]
    chain = [{
        "id": _stable_id("EC", {"evidence": item["id"], "step": index}), "step": index,
        "type": "analyzed", "actor": item["skill"], "product": item["product"],
        "claim": item["summary"], "evidence_refs": [item["id"]], "source_refs": [item["source_ref"]],
        "quality": item["quality"], "falsifiers": item["falsifiers"],
        "confidence": "medium",
    } for index, item in enumerate(claimable_evidence[:60], start=1)]
    chain.extend({
        "id": _stable_id("EC", {"cause": item["id"], "step": len(chain) + index}),
        "step": len(chain) + index,
        "type": "synthesized_hypothesis",
        "actor": "orchestrator",
        "product": "Manager",
        "claim": item["summary"],
        "evidence_refs": list(item.get("evidence_refs") or []),
        "source_refs": [],
        "quality": {},
        "falsifiers": list(item.get("falsifiers") or []),
        "confidence": item.get("confidence", "low"),
    } for index, item in enumerate(causes[:10], start=1))
    unresolved = sorted({ref for item in [*causes, *solutions] for ref in item.get("evidence_refs") or [] if ref not in evidence_ids})
    causes_without_evidence = [item["id"] for item in causes if not item.get("evidence_refs")]
    causes_without_falsifiers = [item["id"] for item in causes if not item.get("falsifiers")]
    report_status = dict(status)
    root_cause = str(report_status.get("root_cause") or "").strip()
    if root_cause:
        matched = next(
            (item for item in causes if item.get("summary") == root_cause), None
        )
        report_status["root_cause_refs"] = list(
            (matched or {}).get("evidence_refs") or []
        )
    stages = [
        {"id": "route", "name": "routing", "status": "completed", "notes": []},
        {"id": "inventory", "name": "source_inventory", "status": "completed", "notes": []},
        {"id": "delegate", "name": "product_analysis", "status": "completed" if products else "skipped", "notes": []},
        {"id": "validate", "name": "cross_validation", "status": "completed" if validation_rounds else "skipped", "notes": []},
        {"id": "gap", "name": "missing_log_gate", "status": "partial" if evidence_gaps else "completed", "notes": []},
        {"id": "report", "name": "manager_report", "status": "completed", "notes": []},
    ]
    enriched_invocations = [_skill_work(item) for item in invocation_nodes]
    problem_statement = _problem_statement(input_summary, products, report_status)
    primary_refs = {
        str(ref)
        for item in causes
        for ref in item.get("evidence_refs") or []
    }
    process_evidence = (
        [item for item in evidence if str(item.get("id")) in primary_refs]
        if primary_refs else evidence
    )
    investigation = _investigation_process(
        stages, process_evidence, enriched_invocations, validation,
    )
    next_actions = _next_actions(next_steps, missing_logs)
    evidence_lookup = {item["id"]: item for item in evidence}
    for collection in (causes, solutions):
        for item in collection:
            item["evidence_details"] = [
                _evidence_projection(evidence_lookup[ref])
                for ref in item.get("evidence_refs") or []
                if ref in evidence_lookup
            ]
    for key in ("similarities", "partial_agreements"):
        for item in validation.get(key) or []:
            refs = item.get("common_evidence_refs") or item.get("evidence_refs") or []
            item["evidence_details"] = [
                _evidence_projection(evidence_lookup[ref])
                for ref in refs if ref in evidence_lookup
            ]
    for item in validation.get("differences") or []:
        for claim in item.get("claims") or []:
            claim["evidence_details"] = [
                _evidence_projection(evidence_lookup[ref])
                for ref in claim.get("evidence_refs") or []
                if ref in evidence_lookup
            ]
    engineering = _engineering_assessment(
        status=report_status,
        evidence=evidence,
        causes=causes,
        validation=validation,
        evidence_gaps=evidence_gaps,
        next_actions=next_actions,
    )
    cause_confidence = _max_confidence(
        [str(item.get("confidence") or "low") for item in causes]
    )
    solution_confidence = _max_confidence(
        [str(item.get("confidence") or "low") for item in solutions]
    )
    next_confidence = _max_confidence(
        [str(item.get("confidence") or "low") for item in next_actions]
    )
    skill_confidence = _min_confidence(
        [str(item.get("confidence") or "low") for item in enriched_invocations]
    )
    return {
        "schema_version": "1.0", "report_id": report_id, "generated_at": _now(),
        "products": list(products), "status": report_status,
        "problem_statement": problem_statement,
        "engineering_assessment": engineering,
        "analysis_process": {"input": dict(input_summary), "routing": dict(routing), "stages": stages},
        "investigation_process": investigation,
        "source_inventory": _inventory(
            products,
            routing.get("inventory") if isinstance(routing, Mapping) else None,
        ),
        "evidence_index": evidence, "evidence_chain": chain,
        "possible_causes": causes, "solutions": solutions,
        "next_actions": next_actions,
        "missing_logs": missing_logs,
        "cross_validation": validation,
        "invocation_chain": {
            "status": "populated" if enriched_invocations else "reserved",
            "nodes": enriched_invocations,
            "confidence": skill_confidence,
        },
        "per_product": [{"product": product, "ok": result.get("ok"), "evidence_state": result.get("evidence_state"), "confidence": result.get("confidence"), "root_cause": result.get("root_cause"), "finding_count": len(result.get("findings") or [])} for product, result in products.items()],
        "collection_plan": dict(collection_plan) if isinstance(collection_plan, Mapping) else None,
        "next_steps": next_steps[:50],
        "quality": {
            "root_quality": dict(root_quality), "evidence_gaps": evidence_gaps[:100],
            "reference_integrity": {
                "valid": not unresolved, "unresolved_refs": unresolved,
                "evidence_count": len(evidence),
            },
            "cause_integrity": {
                "valid": not causes_without_evidence and not causes_without_falsifiers,
                "without_evidence": causes_without_evidence,
                "without_falsifiers": causes_without_falsifiers,
            },
            "bounded": True,
        },
        "confidence_summary": {
            "problem_statement": problem_statement["confidence"],
            "engineering_assessment": engineering["confidence"],
            "investigation_process": investigation["confidence"],
            "possible_causes": cause_confidence,
            "solutions": solution_confidence,
            "next_actions": next_confidence,
            "cross_validation": validation["confidence"],
            "skill_invocation_chain": skill_confidence,
        },
        "privacy": {"default_redaction_required": True, "raw_logs_embedded": False, "bounded_indexes": True},
    }


_THEME_SCRIPT = """<script>
    (() => {
        const param = new URLSearchParams(window.location.search).get("clawpilotTheme");
        const theme =
            param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
        document.documentElement.setAttribute("data-theme", theme);
    })();
</script>"""

_CSS = """
:root {
    color-scheme: light;
    --cp-bg: #f7f4ef;
    --cp-bg-elevated: #fcfbf8;
    --cp-surface: #ffffff;
    --cp-surface-soft: #f5f5f5;
    --cp-border: #dedede;
    --cp-border-strong: #919191;
    --cp-text: #242424;
    --cp-text-muted: #5c5c5c;
    --cp-text-soft: #6f6f6f;
    --cp-accent: #b11f4b;
    --cp-accent-hover: #9a1a41;
    --cp-accent-soft: rgba(177, 31, 75, 0.08);
    --cp-accent-fg: #ffffff;
    --cp-success: #16a34a;
    --cp-danger: #dc2626;
    --cp-warning: #f59e0b;
    --cp-link: #0078d4;
    --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
    --cp-overlay: rgba(255, 255, 255, 0.8);
    --cp-panel: rgba(255, 255, 255, 0.86);
    --cp-panel-strong: rgba(255, 255, 255, 0.96);
    --cp-sheen: rgba(255, 255, 255, 0.55);
    --cp-highlight: rgba(177, 31, 75, 0.12);
}
html[data-theme="dark"] {
    color-scheme: dark;
    --cp-bg: #3d3b3a;
    --cp-bg-elevated: #343231;
    --cp-surface: #292929;
    --cp-surface-soft: #2e2e2e;
    --cp-border: #474747;
    --cp-border-strong: #5f5f5f;
    --cp-text: #dedede;
    --cp-text-muted: #919191;
    --cp-text-soft: #b0b0b0;
    --cp-accent: #fd8ea1;
    --cp-accent-hover: #fb7b91;
    --cp-accent-soft: rgba(253, 142, 161, 0.14);
    --cp-accent-fg: #1a1a1a;
    --cp-success: #4ade80;
    --cp-danger: #f87171;
    --cp-warning: #fbbf24;
    --cp-link: #4da6ff;
    --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
    --cp-overlay: rgba(41, 41, 41, 0.88);
    --cp-panel: rgba(41, 41, 41, 0.72);
    --cp-panel-strong: rgba(41, 41, 41, 0.96);
    --cp-sheen: rgba(255, 255, 255, 0.04);
    --cp-highlight: rgba(253, 142, 161, 0.12);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
    margin: 0;
    max-width: 100%;
    overflow-x: hidden;
    background: var(--cp-bg);
    color: var(--cp-text);
    font: 0.925rem/1.6 "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
    letter-spacing: 0;
}
a { color: var(--cp-link); }
header {
    background: var(--cp-bg-elevated);
    border-bottom: 1px solid var(--cp-border);
    padding: 2rem max(1.25rem, calc((100% - 72rem) / 2));
}
header h1 { margin: 0; max-width: 100%; overflow-wrap: anywhere; font-size: 1.75rem; letter-spacing: 0; }
header p { max-width: 100%; overflow-wrap: anywhere; color: var(--cp-text-muted); margin: 0.4rem 0 0; }
.report-meta { display: flex; flex-wrap: wrap; gap: 0.25rem 0.8rem; }
.report-meta span { max-width: 100%; overflow-wrap: anywhere; }
main { width: 100%; max-width: 72rem; margin: auto; padding: 1.5rem 1.25rem 4rem; overflow-x: hidden; }
section { padding: 1.5rem 0; border-bottom: 1px solid var(--cp-border); }
section:last-child { border-bottom: 0; }
h2 { margin: 0 0 1rem; font-size: 1.25rem; letter-spacing: 0; }
h3 { margin: 0; font-size: 1rem; letter-spacing: 0; }
p { margin: 0.4rem 0 0.8rem; }
.meta, .muted, .empty { color: var(--cp-text-muted); font-size: 0.82rem; }
.empty { font-style: italic; padding: 0.75rem 0; }
.toc { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; padding: 1rem 0; }
.toc a { max-width: 100%; overflow-wrap: anywhere; text-decoration: none; font-weight: 600; }
.summary-grid, .confidence-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
    gap: 0.75rem;
}
.metric {
    min-height: 5.25rem;
    padding: 0.85rem 1rem;
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
}
.metric strong { display: block; margin-top: 0.25rem; font-size: 1.05rem; overflow-wrap: anywhere; }
.callout {
    padding: 1rem 1.1rem;
    background: var(--cp-accent-soft);
    border-left: 0.25rem solid var(--cp-accent);
    border-radius: 0.625rem;
}
.chips { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.6rem 0; }
.chip, .badge {
    display: inline-flex;
    align-items: center;
    min-height: 1.6rem;
    padding: 0.12rem 0.5rem;
    background: var(--cp-surface-soft);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    font-size: 0.76rem;
    font-weight: 600;
}
.confidence-high { color: var(--cp-success); border-color: var(--cp-success); }
.confidence-medium { color: var(--cp-warning); border-color: var(--cp-warning); }
.confidence-low { color: var(--cp-danger); border-color: var(--cp-danger); }
.status-failed, .status-timeout { color: var(--cp-danger); }
.status-completed { color: var(--cp-success); }
.status-skipped, .status-pending { color: var(--cp-warning); }
.timeline { margin: 0; padding: 0; list-style: none; }
.timeline > li {
    position: relative;
    margin-left: 0.6rem;
    padding: 0 0 1.25rem 1.4rem;
    border-left: 2px solid var(--cp-border-strong);
}
.timeline > li:last-child { padding-bottom: 0; }
.timeline > li::before {
    content: "";
    position: absolute;
    width: 0.65rem;
    height: 0.65rem;
    left: -0.4rem;
    top: 0.35rem;
    background: var(--cp-accent);
    border-radius: 50%;
}
.timeline-head, .card-head, .action-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
}
.card-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 21rem), 1fr));
    gap: 0.75rem;
}
.card {
    padding: 1rem;
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
    overflow-wrap: anywhere;
}
.card ul, .card ol { margin: 0.6rem 0 0; padding-left: 1.2rem; }
.card li { margin: 0.25rem 0; }
.evidence-refs { font-family: Consolas, "Courier New", Courier, monospace; font-size: 0.76rem; }
.evidence-list { display: grid; gap: 0.45rem; margin-top: 0.65rem; }
.evidence-item {
    padding: 0.65rem 0.75rem;
    background: var(--cp-surface-soft);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
}
.evidence-item strong { display: block; margin: 0.2rem 0; }
.evidence-facts { display: flex; flex-wrap: wrap; gap: 0.25rem 0.6rem; margin-top: 0.35rem; }
.evidence-fact { font-size: 0.76rem; color: var(--cp-text-muted); }
.reasoning-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
    gap: 0.8rem;
    margin-top: 0.8rem;
}
.reasoning-column {
    padding: 0.9rem;
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
}
.reasoning-column.support { border-top: 0.2rem solid var(--cp-success); }
.reasoning-column.counter { border-top: 0.2rem solid var(--cp-danger); }
.reasoning-column.alternative { border-top: 0.2rem solid var(--cp-warning); }
.validation-columns {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
    gap: 0.9rem;
}
.validation-column { padding-top: 0.5rem; border-top: 0.2rem solid var(--cp-border-strong); }
.validation-column.agree { border-top-color: var(--cp-success); }
.validation-column.differ { border-top-color: var(--cp-danger); }
.validation-column.partial { border-top-color: var(--cp-warning); }
.skill-chain { margin: 0; padding: 0; list-style: none; }
.skill-chain > li {
    display: grid;
    grid-template-columns: 2.25rem minmax(0, 1fr);
    gap: 0.8rem;
    padding: 0.9rem 0;
    border-bottom: 1px solid var(--cp-border);
}
.skill-chain > li:last-child { border-bottom: 0; }
.step-number {
    display: grid;
    place-items: center;
    width: 2rem;
    height: 2rem;
    background: var(--cp-accent);
    color: var(--cp-accent-fg);
    border-radius: 50%;
    font-weight: 700;
}
.action-list { display: grid; gap: 0.65rem; }
.action {
    padding: 0.9rem 1rem;
    background: var(--cp-surface);
    border: 1px solid var(--cp-border);
    border-radius: 0.625rem;
}
.priority-high { border-left: 0.25rem solid var(--cp-danger); }
details { margin: 0.5rem 0; }
summary { cursor: pointer; color: var(--cp-link); font-weight: 600; }
table { width: 100%; border-collapse: collapse; background: var(--cp-surface); }
th, td { padding: 0.55rem 0.65rem; border-bottom: 1px solid var(--cp-border); text-align: left; vertical-align: top; }
th { background: var(--cp-surface-soft); font-size: 0.78rem; }
code { font-family: Consolas, "Courier New", Courier, monospace; font-size: 0.8rem; }
@media (max-width: 42rem) {
    header { padding: 1.25rem; }
    header h1 { font-size: 1.5rem; line-height: 1.25; }
    .report-meta { display: block; }
    .report-meta span { display: block; width: 100%; word-break: break-all; }
    main { padding: 1rem 0.9rem 3rem; }
    .toc { display: grid; grid-template-columns: minmax(0, 1fr); gap: 0.45rem; }
    .summary-grid, .confidence-grid, .card-list, .validation-columns {
        grid-template-columns: minmax(0, 1fr);
    }
    .metric { min-width: 0; min-height: auto; }
    .metric strong { word-break: break-word; }
    .timeline-head, .card-head, .action-head { display: block; }
    .badge { margin-top: 0.35rem; }
}
@media print {
    body { background: var(--cp-surface); }
    .toc { display: none; }
    section { break-inside: avoid; }
}
"""

L10N = {
        "en-US": {
                "lang": "en", "title": "SkillsCollection Incident Analysis Report",
                "subtitle": "Evidence-grounded IIS and Microsoft Edge diagnosis",
                "executive": "Executive Summary", "problem": "Problem Description",
                "process": "Troubleshooting Process", "causes": "Cause Analysis",
                "solutions": "Recommended Solutions",
                "next": "Next Actions and Required Logs",
                "validation": "Cross-Validation Results", "agreements": "Agreements",
                "differences": "Differences", "partial": "Partial Agreements",
                "skills": "Skill Invocation Chain and Process", "evidence": "Evidence Chain",
                "confidence": "Confidence", "status": "Status", "products": "Products",
                "evidence_state": "Evidence state", "root_cause": "Root cause",
                "not_proven": "Root cause not proven",
                "no_causes": "No evidence-complete cause is currently established.",
                "no_solutions": "No evidence-linked solution is currently available.",
                "no_actions": "No additional action is currently recorded.",
                "no_entries": "No entries.", "why": "Why", "owner": "Owner",
                "priority": "Priority", "collection": "How to collect",
                "evidence_ids": "Evidence IDs", "falsifiers": "Falsifiers",
                "verification": "Verification", "rollback": "Rollback",
                "rounds": "Validation rounds", "providers": "Providers",
                "agreement_ratio": "Agreement ratio", "what_did": "What this skill did",
                "original_text": "Technical result", "generated": "Generated",
                "report_id": "Report ID", "language": "English",
                "high": "High", "medium": "Medium", "low": "Low",
                "high_priority": "High", "normal_priority": "Normal",
                "collect": "Collect", "input_scope": "Input scope",
                "time_range": "Time range", "problem_types": "Problem types",
                "reason": "Reason", "claims": "Claims", "analyzers": "Analyzers",
                "common_evidence": "Common evidence", "score": "Score",
                "confidence_overview": "Confidence by Module",
                "engineering": "Senior Engineering Assessment",
                "supporting": "Supporting evidence", "counter": "Counter-evidence and uncertainty",
                "alternatives": "Alternative explanations", "noise": "Noise and interference assessment",
                "principles": "Decision principles", "recommended_move": "Recommended next decision",
                "source": "Source", "record": "Record", "time": "Time",
                "facts": "Key facts", "relevance": "Symptom relevance",
                "background": "Background", "contextual": "Contextual", "supportive": "Supportive",
                "ungraded": "Not graded", "more_evidence": "Additional evidence is listed in the appendix",
                "evidence_count": "Evidence items", "blocking_gaps": "Blocking gaps",
                "audit_evidence": "Background and audit evidence",
                "audit_evidence_note": "Retained for traceability; these items do not support the leading cause.",
                "report_note": "Technical evidence text is preserved verbatim to avoid changing diagnostic meaning.",
                "completed": "Completed", "skipped": "Skipped", "failed": "Failed",
                "timeout": "Timeout", "pending": "Pending", "unknown": "Unknown",
                "not_configured": "Not configured", "conflict": "Conflict",
                "requested": "Requested", "missing": "Missing",
                "candidate": "Candidate", "supported": "Supported",
                "unsupported": "Unsupported", "proven": "Proven",
                "supported_hypothesis": "Supported hypothesis",
                "inconclusive": "Inconclusive",
                "validation_difference": "Validation difference",
                "evidence_gap": "Evidence gap", "falsifier": "Falsifier",
                "uncertainty": "Uncertainty",
                "root_cause_found": "Root cause proven",
                "inconclusive_evidence": "Evidence inconclusive",
                "missing_required_evidence": "Required evidence missing",
                "time_not_comparable": "Time not comparable",
                "permission_blocked": "Permission blocked",
                "tool_unavailable": "Tool unavailable",
                "unknown_log_type": "Unknown log type",
                "live_collection_required": "Live collection required",
                "collection_plan_ready": "Collection plan ready",
        },
        "zh-CN": {
                "lang": "zh-CN", "title": "SkillsCollection 故障分析报告",
                "subtitle": "基于证据的 IIS 与 Microsoft Edge 诊断",
                "executive": "执行摘要", "problem": "问题描述",
                "process": "问题排查过程", "causes": "问题原因分析",
                "solutions": "问题解决方案", "next": "下一步操作与所需日志",
                "validation": "交叉验证结果", "agreements": "相同点",
                "differences": "不同点", "partial": "部分一致",
                "skills": "Skills 调用链与工作说明", "evidence": "证据链与附录",
                "confidence": "置信度", "status": "状态", "products": "产品范围",
                "evidence_state": "证据状态", "root_cause": "根本原因",
                "not_proven": "尚未证明根本原因",
                "no_causes": "当前没有证据完整的原因结论。",
                "no_solutions": "当前没有与证据关联的解决方案。",
                "no_actions": "当前没有记录额外操作。",
                "no_entries": "暂无内容。", "why": "原因", "owner": "负责 Skill",
                "priority": "优先级", "collection": "收集方法",
                "evidence_ids": "证据 ID", "falsifiers": "反证条件",
                "verification": "验证标准", "rollback": "回滚条件",
                "rounds": "验证轮次", "providers": "验证提供方",
                "agreement_ratio": "一致率", "what_did": "该 Skill 执行的工作",
                "original_text": "技术结论", "generated": "生成时间",
                "report_id": "报告 ID", "language": "中文版",
                "high": "高", "medium": "中", "low": "低",
                "high_priority": "高", "normal_priority": "普通",
                "collect": "收集", "input_scope": "输入范围",
                "time_range": "时间范围", "problem_types": "问题类型",
                "reason": "原因", "claims": "结论", "analyzers": "分析器",
                "common_evidence": "共同证据", "score": "分数",
                "confidence_overview": "各模块置信度",
                "engineering": "高级工程师综合研判",
                "supporting": "支持证据", "counter": "反证与不确定性",
                "alternatives": "替代解释", "noise": "噪声与干扰因素评估",
                "principles": "判断原则", "recommended_move": "建议的下一决策",
                "source": "来源", "record": "记录定位", "time": "时间",
                "facts": "关键事实", "relevance": "与问题现象的相关性",
                "background": "背景观察", "contextual": "上下文相关", "supportive": "支持性证据",
                "ungraded": "未分级", "more_evidence": "更多证据见证据附录",
                "evidence_count": "证据条目", "blocking_gaps": "阻塞性证据缺口",
                "audit_evidence": "背景与审计证据",
                "audit_evidence_note": "以下内容仅用于追溯，不支持当前首要原因。",
                "report_note": "为避免改变诊断含义，技术证据文本保留原始内容。",
                "completed": "已完成", "skipped": "已跳过", "failed": "失败",
                "timeout": "超时", "pending": "等待中", "unknown": "未知",
                "not_configured": "未配置", "conflict": "存在差异",
                "requested": "已请求", "missing": "缺失",
                "candidate": "候选原因", "supported": "已有支持证据",
                "unsupported": "证据不足", "proven": "已证明",
                "supported_hypothesis": "证据支持的假设",
                "inconclusive": "尚无定论",
                "validation_difference": "交叉验证差异",
                "evidence_gap": "证据缺口", "falsifier": "反证条件",
                "uncertainty": "不确定性",
                "root_cause_found": "根本原因已证明",
                "inconclusive_evidence": "证据不足以得出结论",
                "missing_required_evidence": "缺少必要证据",
                "time_not_comparable": "时间基准不可比较",
                "permission_blocked": "权限受限",
                "tool_unavailable": "分析工具不可用",
                "unknown_log_type": "未知日志类型",
                "live_collection_required": "需要实时采集",
                "collection_plan_ready": "日志收集计划已就绪",
        },
}


def _t(locale: str, key: str) -> str:
    language = L10N.get(locale, L10N["en-US"])
    return str(language.get(key) or L10N["en-US"].get(key) or key)


def _confidence_badge(value: Any, locale: str) -> str:
    level = _confidence_level(value)
    return (
        f'<span class="badge confidence-{escape(level)}">'
        f'{escape(_t(locale, level))}</span>'
    )


def _status_badge(value: Any, locale: str = "en-US") -> str:
    status = str(value or "unknown")
    css = "status-" + "".join(
        character if character.isalnum() else "-"
        for character in status.casefold()
    )
    label = _t(locale, status.casefold())
    return f'<span class="badge {escape(css)}">{escape(label)}</span>'


def _evidence_state_label(value: Any, locale: str) -> str:
    state = str(value or "unknown").casefold()
    return _t(locale, state)


def _chips(values: Iterable[Any]) -> str:
    items = [str(value) for value in values if value not in (None, "")]
    return '<div class="chips">' + "".join(
        f'<span class="chip">{escape(value)}</span>' for value in items
    ) + "</div>" if items else ""


def _display_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return "-"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _evidence_refs(
    values: Iterable[Any],
    locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    max_items: int = 4,
) -> str:
    refs = [str(value) for value in values if value]
    if not refs:
        return ""
    if evidence_lookup:
        cards = []
        for ref in refs[:max_items]:
            evidence = evidence_lookup.get(ref)
            if not isinstance(evidence, Mapping):
                cards.append(
                    f'<div class="evidence-item"><code>{escape(ref)}</code>'
                    f'<span class="meta"> · {escape(_t(locale, "ungraded"))}</span></div>'
                )
                continue
            display = _as_mapping(evidence.get("display")) or evidence
            facts = ''.join(
                f'<span class="evidence-fact"><strong>{escape(str(item.get("name")))}:</strong> '
                f'{escape(_display_value(item.get("value")))}</span>'
                for item in display.get("facts") or []
                if isinstance(item, Mapping)
            )
            relevance = display.get("signal_classification") or display.get("symptom_relevance")
            meta = [
                f'{_t(locale, "source")}: {_display_value(display.get("source"))}',
                f'{_t(locale, "record")}: {_display_value(display.get("record"))}',
                f'{_t(locale, "time")}: {_display_value(display.get("time"))}',
            ]
            cards.append(
                '<div class="evidence-item">'
                f'<code>{escape(ref)}</code>'
                f'<strong>{escape(str(display.get("headline") or evidence.get("summary") or ""))}</strong>'
                f'{_chips([display.get("product"), display.get("skill"), _t(locale, str(relevance or "ungraded"))])}'
                f'<div class="meta">{escape(" · ".join(meta))}</div>'
                + (f'<div class="evidence-facts">{facts}</div>' if facts else "")
                + '</div>'
            )
        more = (
            f'<div class="meta">+{len(refs) - max_items} · '
            f'{escape(_t(locale, "more_evidence"))}</div>'
            if len(refs) > max_items else ""
        )
        return '<div class="evidence-list">' + ''.join(cards) + more + '</div>'
    return (
        f'<div class="meta"><strong>{escape(_t(locale, "evidence_ids"))}:</strong> '
        f'<span class="evidence-refs">{escape(", ".join(refs))}</span></div>'
    )


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    body = list(rows)
    if not body:
        return '<p class="empty">No entries.</p>'
    return "<table><thead><tr>" + "".join(f"<th>{escape(value)}</th>" for value in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{escape(str(value if value is not None else ''))}</td>" for value in row) + "</tr>" for row in body) + "</tbody></table>"


def _heading(title: str, confidence: Any, locale: str) -> str:
    return (
        '<div class="card-head">'
        f'<h2>{escape(title)}</h2>{_confidence_badge(confidence, locale)}'
        '</div>'
    )


def _render_problem(report: Mapping[str, Any], locale: str) -> str:
    problem = report.get("problem_statement") or {}
    time_range = problem.get("time_range") or {}
    description = str(
        problem.get("description_zh" if locale == "zh-CN" else "description_en")
        or problem.get("description")
        or _t(locale, "no_entries")
    )
    details = [
        f'<div class="metric"><span class="meta">{escape(_t(locale, "products"))}</span><strong>{escape(", ".join(problem.get("products") or []))}</strong></div>',
        f'<div class="metric"><span class="meta">{escape(_t(locale, "problem_types"))}</span><strong>{escape(", ".join(problem.get("problem_types") or []) or "-")}</strong></div>',
        f'<div class="metric"><span class="meta">{escape(_t(locale, "time_range"))}</span><strong>{escape(str(time_range.get("start") or "-"))}<br>{escape(str(time_range.get("end") or "-"))}</strong></div>',
    ]
    return (
        f'<section id="problem">{_heading(_t(locale, "problem"), problem.get("confidence"), locale)}'
        f'<div class="callout"><p>{escape(description)}</p>'
        f'<p class="meta">{escape(_t(locale, "report_note"))}</p></div>'
        f'<div class="summary-grid">{"".join(details)}</div></section>'
    )


def _render_process(
    report: Mapping[str, Any], locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    process = report.get("investigation_process") or {}
    steps = process.get("steps") or []
    language_key = "description_zh" if locale == "zh-CN" else "description_en"
    title_key = "title_zh" if locale == "zh-CN" else "title_en"
    items = []
    for step in steps:
        notes = list(step.get("notes") or [])
        items.append(
            '<li><div class="timeline-head">'
            f'<div><h3>{escape(str(step.get(title_key) or step.get("id")))}</h3>'
            f'<p>{escape(str(step.get(language_key) or ""))}</p></div>'
            f'{_confidence_badge(step.get("confidence"), locale)}</div>'
            f'{_status_badge(step.get("status"), locale)}'
            + (f'<p class="meta">{escape("; ".join(str(note) for note in notes))}</p>' if notes else "")
            + _evidence_refs(
                step.get("evidence_refs") or [], locale, evidence_lookup,
                max_items=2,
            )
            + '</li>'
        )
    summary = process.get("summary_zh" if locale == "zh-CN" else "summary_en") or ""
    return (
        f'<section id="process">{_heading(_t(locale, "process"), process.get("confidence"), locale)}'
        f'<p>{escape(str(summary))}</p><ol class="timeline">{"".join(items)}</ol></section>'
    )


def _render_causes(
    report: Mapping[str, Any], locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    causes = report.get("possible_causes") or []
    cards = []
    for cause in causes:
        falsifiers = cause.get("falsifiers") or []
        label = cause.get("label_zh" if locale == "zh-CN" else "label_en")
        summary = cause.get("summary_zh" if locale == "zh-CN" else "summary_en")
        cards.append(
            '<article class="card"><div class="card-head">'
            f'<h3>{escape(str(label or cause.get("label") or cause.get("summary") or ""))}</h3>'
            f'{_confidence_badge(cause.get("confidence"), locale)}</div>'
            f'<div class="chips">{_status_badge(cause.get("status"), locale)}'
            + ''.join(f'<span class="chip">{escape(str(skill))}</span>' for skill in cause.get("supporting_skills") or [])
            + '</div>'
            f'<p>{escape(str(summary or cause.get("summary") or ""))}</p>'
            + _evidence_refs(
                cause.get("evidence_refs") or [], locale, evidence_lookup,
            )
            + (f'<p class="meta"><strong>{escape(_t(locale, "falsifiers"))}:</strong> {escape("; ".join(str(value) for value in falsifiers))}</p>' if falsifiers else "")
            + '</article>'
        )
    body = '<div class="card-list">' + ''.join(cards) + '</div>' if cards else f'<p class="empty">{escape(_t(locale, "no_causes"))}</p>'
    confidence = (report.get("confidence_summary") or {}).get("possible_causes")
    return f'<section id="causes">{_heading(_t(locale, "causes"), confidence, locale)}{body}</section>'


def _render_solutions(
    report: Mapping[str, Any], locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    cards = []
    for solution in report.get("solutions") or []:
        verification = solution.get("verification") or {}
        steps = ''.join(f'<li>{escape(str(step))}</li>' for step in solution.get("steps") or [])
        cards.append(
            '<article class="card"><div class="card-head">'
            f'<h3>{escape(str(solution.get("title") or ""))}</h3>'
            f'{_confidence_badge(solution.get("confidence"), locale)}</div>'
            f'{_chips([solution.get("product"), solution.get("problem_ref"), solution.get("evidence_relation")])}'
            + (f'<ol>{steps}</ol>' if steps else "")
            + _evidence_refs(
                solution.get("evidence_refs") or [], locale, evidence_lookup,
            )
            + f'<p class="meta"><strong>{escape(_t(locale, "verification"))}:</strong> {escape(str(verification.get("metric") or "-"))}</p>'
            + f'<p class="meta"><strong>{escape(_t(locale, "rollback"))}:</strong> {escape(str(verification.get("rollback") or "-"))}</p>'
            + '</article>'
        )
    body = '<div class="card-list">' + ''.join(cards) + '</div>' if cards else f'<p class="empty">{escape(_t(locale, "no_solutions"))}</p>'
    confidence = (report.get("confidence_summary") or {}).get("solutions")
    return f'<section id="solutions">{_heading(_t(locale, "solutions"), confidence, locale)}{body}</section>'


def _render_next(report: Mapping[str, Any], locale: str) -> str:
    actions = []
    for item in report.get("next_actions") or []:
        collection = item.get("how_to_collect")
        action = item.get("action_zh" if locale == "zh-CN" else "action_en") or item.get("action")
        actions.append(
            f'<article class="action priority-{escape(str(item.get("priority") or "normal"))}">'
            '<div class="action-head"><div>'
            f'<h3>{escape(str(action or ""))}</h3>'
            f'<p>{escape(str(item.get("why") or ""))}</p></div>'
            f'{_confidence_badge(item.get("confidence"), locale)}</div>'
            f'{_chips([item.get("product"), item.get("skill"), item.get("log_kind")])}'
            + (f'<p class="meta"><strong>{escape(_t(locale, "collection"))}:</strong> {escape(str(collection))}</p>' if collection else "")
            + '</article>'
        )
    body = '<div class="action-list">' + ''.join(actions) + '</div>' if actions else f'<p class="empty">{escape(_t(locale, "no_actions"))}</p>'
    confidence = (report.get("confidence_summary") or {}).get("next_actions")
    return f'<section id="next">{_heading(_t(locale, "next"), confidence, locale)}{body}</section>'


def _validation_items(
    values: Iterable[Mapping[str, Any]], locale: str, kind: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    output = []
    for item in values:
        conclusion = str(item.get("conclusion") or item.get("reason") or "")
        claim_details = []
        for claim in item.get("claims") or []:
            claim_details.append(
                f'<li><strong>{escape(str(claim.get("analyzer") or ""))}:</strong> '
                f'{escape(str(claim.get("conclusion") or ""))} '
                f'{_confidence_badge(claim.get("confidence"), locale)}'
                + _evidence_refs(
                    claim.get("evidence_refs") or [],
                    locale,
                    evidence_lookup,
                    max_items=2,
                )
                + '</li>'
            )
        output.append(
            '<article class="card">'
            f'<div class="card-head"><h3>{escape(conclusion or str(item.get("claim_key") or ""))}</h3>'
            f'{_confidence_badge(item.get("confidence"), locale)}</div>'
            f'{_chips(item.get("analyzers") or [])}'
            + _evidence_refs(
                item.get("common_evidence_refs") or item.get("evidence_refs") or [],
                locale, evidence_lookup,
            )
            + (f'<ul>{"".join(claim_details)}</ul>' if claim_details else "")
            + (f'<p class="meta"><strong>{escape(_t(locale, "reason"))}:</strong> {escape(str(item.get("reason")))}</p>' if item.get("reason") else "")
            + '</article>'
        )
    return ''.join(output) or f'<p class="empty">{escape(_t(locale, "no_entries"))}</p>'


def _render_validation(
    report: Mapping[str, Any], locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    validation = report.get("cross_validation") or {}
    rounds = validation.get("rounds") or []
    round_rows = (
        [item.get("round"), item.get("name"), item.get("status"), item.get("evidence_sources"), _t(locale, str(item.get("confidence") or "low"))]
        for item in rounds
    )
    overview = (
        '<div class="summary-grid">'
        f'<div class="metric"><span class="meta">{escape(_t(locale, "status"))}</span><strong>{escape(_t(locale, str(validation.get("status") or "not_configured").casefold()))}</strong></div>'
        f'<div class="metric"><span class="meta">{escape(_t(locale, "agreement_ratio"))}</span><strong>{escape(f"{float(validation.get('agreement_ratio', 0.0) or 0.0):.0%}")}</strong></div>'
        f'<div class="metric"><span class="meta">{escape(_t(locale, "providers"))}</span><strong>{escape(str(validation.get("completed_provider_count") or 0))}/{escape(str(validation.get("provider_count") or 0))}</strong></div>'
        f'<div class="metric"><span class="meta">{escape(_t(locale, "rounds"))}</span><strong>{escape(str(validation.get("round_count") or 0))}</strong></div>'
        '</div>'
    )
    columns = (
        '<div class="validation-columns">'
        f'<div class="validation-column agree"><h3>{escape(_t(locale, "agreements"))}</h3>{_validation_items(validation.get("similarities") or [], locale, "agree", evidence_lookup)}</div>'
        f'<div class="validation-column differ"><h3>{escape(_t(locale, "differences"))}</h3>{_validation_items(validation.get("differences") or [], locale, "differ", evidence_lookup)}</div>'
        f'<div class="validation-column partial"><h3>{escape(_t(locale, "partial"))}</h3>{_validation_items(validation.get("partial_agreements") or [], locale, "partial", evidence_lookup)}</div>'
        '</div>'
    )
    table = _table(
        ["#", _t(locale, "original_text"), _t(locale, "status"), "Sources", _t(locale, "confidence")],
        round_rows,
    )
    return (
        f'<section id="validation">{_heading(_t(locale, "validation"), validation.get("confidence"), locale)}'
        f'{overview}{columns}<details><summary>{escape(_t(locale, "rounds"))}</summary>{table}</details></section>'
    )


def _render_engineering(
    report: Mapping[str, Any], locale: str,
    evidence_lookup: Mapping[str, Mapping[str, Any]],
) -> str:
    assessment = report.get("engineering_assessment") or {}
    summary = assessment.get("summary_zh" if locale == "zh-CN" else "summary_en") or ""
    supporting = assessment.get("supporting_evidence") or []
    support_refs = [str(item.get("id")) for item in supporting if item.get("id")]
    counter = ''.join(
        '<li>'
        f'<strong>{escape(_t(locale, str(item.get("type") or "uncertainty")))}:</strong> '
        f'{escape(str(item.get("summary") or ""))} '
        f'{_confidence_badge(item.get("confidence"), locale)}'
        '</li>'
        for item in assessment.get("counter_evidence_and_uncertainty") or []
    )
    alternatives = ''.join(
        '<li>'
        f'{escape(str(item.get("cause") or ""))} '
        f'{_confidence_badge(item.get("confidence"), locale)}'
        + _evidence_refs(
            item.get("evidence_refs") or [], locale, evidence_lookup,
            max_items=2,
        )
        + '</li>'
        for item in assessment.get("alternative_explanations") or []
    )
    noise = assessment.get("noise_assessment") or {}
    noise_rows = (
        [family, values.get("supportive", 0), values.get("contextual", 0),
         values.get("background", 0), values.get("ungraded", 0)]
        for family, values in (noise.get("by_source_family") or {}).items()
    )
    principles = ''.join(
        f'<li>{escape(str(value))}</li>'
        for value in noise.get(
            "principles_zh" if locale == "zh-CN" else "principles_en"
        ) or []
    )
    recommended = assessment.get("recommended_next_move") or {}
    recommended_action = (
        recommended.get("action_zh" if locale == "zh-CN" else "action_en")
        or recommended.get("action")
    )
    return (
        f'<section id="engineering">{_heading(_t(locale, "engineering"), assessment.get("confidence"), locale)}'
        f'<div class="callout"><p>{escape(str(summary))}</p>'
        f'{_chips([_t(locale, str(assessment.get("position") or "inconclusive"))])}</div>'
        '<div class="reasoning-grid">'
        f'<div class="reasoning-column support"><h3>{escape(_t(locale, "supporting"))}</h3>'
        + _evidence_refs(support_refs, locale, evidence_lookup, max_items=8)
        + '</div>'
        f'<div class="reasoning-column counter"><h3>{escape(_t(locale, "counter"))}</h3>'
        + (f'<ul>{counter}</ul>' if counter else f'<p class="empty">{escape(_t(locale, "no_entries"))}</p>')
        + '</div>'
        f'<div class="reasoning-column alternative"><h3>{escape(_t(locale, "alternatives"))}</h3>'
        + (f'<ul>{alternatives}</ul>' if alternatives else f'<p class="empty">{escape(_t(locale, "no_entries"))}</p>')
        + '</div></div>'
        f'<h3>{escape(_t(locale, "noise"))}</h3>'
        + _table(
            [_t(locale, "source"), _t(locale, "supportive"), _t(locale, "contextual"),
             _t(locale, "background"), _t(locale, "ungraded")],
            noise_rows,
        )
        + (f'<h3>{escape(_t(locale, "principles"))}</h3><ul>{principles}</ul>' if principles else "")
        + (f'<div class="callout"><strong>{escape(_t(locale, "recommended_move"))}:</strong> {escape(str(recommended_action))}</div>' if recommended_action else "")
        + '</section>'
    )


def _render_skills(report: Mapping[str, Any], locale: str) -> str:
    nodes = (report.get("invocation_chain") or {}).get("nodes") or []
    work_key = "work_summary_zh" if locale == "zh-CN" else "work_summary_en"
    items = []
    for index, node in enumerate(nodes, start=1):
        execution = [node.get("runtime"), node.get("role"), node.get("product")]
        items.append(
            '<li><span class="step-number">' + escape(str(index)) + '</span><div>'
            '<div class="timeline-head"><div>'
            f'<h3>{escape(str(node.get("skill") or "unknown"))}</h3>'
            f'<p>{escape(str(node.get(work_key) or ""))}</p></div>'
            f'{_confidence_badge(node.get("confidence"), locale)}</div>'
            f'{_status_badge(node.get("status"), locale)}{_chips(execution)}'
            f'<p class="meta"><code>{escape(str(node.get("id") or ""))}</code>'
            + (f' · exit {escape(str(node.get("exit_code")))}' if node.get("exit_code") is not None else "")
            + '</p></div></li>'
        )
    body = '<ol class="skill-chain">' + ''.join(items) + '</ol>' if items else f'<p class="empty">{escape(_t(locale, "no_entries"))}</p>'
    confidence = (report.get("invocation_chain") or {}).get("confidence")
    return f'<section id="skills">{_heading(_t(locale, "skills"), confidence, locale)}{body}</section>'


def _render_confidence(report: Mapping[str, Any], locale: str) -> str:
    labels = {
        "problem_statement": _t(locale, "problem"),
        "engineering_assessment": _t(locale, "engineering"),
        "investigation_process": _t(locale, "process"),
        "possible_causes": _t(locale, "causes"),
        "solutions": _t(locale, "solutions"),
        "next_actions": _t(locale, "next"),
        "cross_validation": _t(locale, "validation"),
        "skill_invocation_chain": _t(locale, "skills"),
    }
    metrics = ''.join(
        '<div class="metric">'
        f'<span class="meta">{escape(str(labels.get(key) or key))}</span>'
        f'<strong>{escape(_t(locale, _confidence_level(value)))}</strong>'
        '</div>'
        for key, value in (report.get("confidence_summary") or {}).items()
    )
    return f'<section id="confidence"><h2>{escape(_t(locale, "confidence_overview"))}</h2><div class="confidence-grid">{metrics}</div></section>'


def _render_evidence(report: Mapping[str, Any], locale: str) -> str:
    primary_refs = {
        str(ref)
        for section in ("possible_causes", "solutions")
        for item in report.get(section) or []
        if isinstance(item, Mapping)
        for ref in item.get("evidence_refs") or []
    }
    primary_cards = []
    audit_cards = []
    for item in report.get("evidence_index") or []:
        if not isinstance(item, Mapping):
            continue
        display = _as_mapping(item.get("display"))
        facts = ''.join(
            '<li>'
            f'<strong>{escape(str(fact.get("name") or ""))}:</strong> '
            f'{escape(_display_value(fact.get("value")))}'
            '</li>'
            for fact in display.get("facts") or []
            if isinstance(fact, Mapping)
        )
        reasons = ''.join(
            f'<li>{escape(str(reason))}</li>'
            for reason in display.get("relevance_reasons") or []
        )
        raw_detail = json.dumps(
            item.get("evidence") or {},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        card = (
            '<article class="card">'
            '<div class="card-head"><div>'
            f'<code>{escape(str(item.get("id") or ""))}</code>'
            f'<h3>{escape(str(display.get("headline") or item.get("summary") or ""))}</h3>'
            '</div>'
            f'{_chips([item.get("product"), item.get("skill"), item.get("severity")])}</div>'
            f'<p><strong>{escape(_t(locale, "source"))}:</strong> {escape(_display_value(display.get("source")))}</p>'
            f'<p><strong>{escape(_t(locale, "time"))}:</strong> {escape(_display_value(display.get("time")))}</p>'
            f'<p><strong>{escape(_t(locale, "record"))}:</strong> {escape(_display_value(display.get("record")))}</p>'
            f'{_chips([_t(locale, str(display.get("signal_classification") or "ungraded")), _t(locale, str(display.get("noise_risk") or "ungraded"))])}'
            + (f'<h3>{escape(_t(locale, "facts"))}</h3><ul>{facts}</ul>' if facts else "")
            + (f'<h3>{escape(_t(locale, "relevance"))}</h3><ul>{reasons}</ul>' if reasons else "")
            + '<details><summary>' + escape(_t(locale, "original_text")) + '</summary>'
            + f'<pre><code>{escape(raw_detail)}</code></pre></details>'
            + '</article>'
        )
        is_primary = (
            str(item.get("id")) in primary_refs
            if primary_refs else _is_claimable_evidence(item)
        )
        (primary_cards if is_primary else audit_cards).append(card)
    body = (
        '<div class="card-list">' + ''.join(primary_cards) + '</div>'
        if primary_cards else f'<p class="empty">{escape(_t(locale, "no_entries"))}</p>'
    )
    if audit_cards:
        body += (
            '<details class="audit-evidence"><summary>'
            f'{escape(_t(locale, "audit_evidence"))} ({len(audit_cards)})</summary>'
            f'<p class="meta">{escape(_t(locale, "audit_evidence_note"))}</p>'
            '<div class="card-list">' + ''.join(audit_cards) + '</div></details>'
        )
    return f'<section id="evidence"><h2>{escape(_t(locale, "evidence"))}</h2>{body}</section>'


def render_manager_html(report: Mapping[str, Any], locale: str = "en-US") -> str:
    report = redact_default(dict(report))
    locale = locale if locale in L10N else "en-US"
    status = report.get("status") or {}
    confidence = report.get("confidence_summary") or {}
    blocking_gaps = sum(
        1 for item in (report.get("quality") or {}).get("evidence_gaps") or []
        if item.get("blocking")
    )
    evidence_lookup = {
        str(item.get("id")): item
        for item in report.get("evidence_index") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    summary_metrics = [
        (_t(locale, "products"), ", ".join(report.get("products") or []) or "-"),
        (_t(locale, "evidence_state"), _evidence_state_label(status.get("evidence_state"), locale)),
        (_t(locale, "confidence"), _t(locale, _confidence_level(status.get("confidence")))),
        (_t(locale, "root_cause"), status.get("root_cause") or _t(locale, "not_proven")),
        (_t(locale, "evidence_count"), (report.get("quality") or {}).get("reference_integrity", {}).get("evidence_count", 0)),
        (_t(locale, "blocking_gaps"), blocking_gaps),
    ]
    summary_html = ''.join(
        f'<div class="metric"><span class="meta">{escape(str(label))}</span><strong>{escape(str(value))}</strong></div>'
        for label, value in summary_metrics
    )
    toc_items = [
        ("problem", _t(locale, "problem")), ("process", _t(locale, "process")),
        ("engineering", _t(locale, "engineering")),
        ("causes", _t(locale, "causes")), ("solutions", _t(locale, "solutions")),
        ("next", _t(locale, "next")), ("validation", _t(locale, "validation")),
        ("skills", _t(locale, "skills")), ("evidence", _t(locale, "evidence")),
    ]
    toc = '<nav class="toc">' + ''.join(
        f'<a href="#{escape(anchor)}">{escape(label)}</a>' for anchor, label in toc_items
    ) + '</nav>'
    sections = [
        _render_confidence(report, locale), _render_problem(report, locale),
        _render_process(report, locale, evidence_lookup),
        _render_engineering(report, locale, evidence_lookup),
        _render_causes(report, locale, evidence_lookup),
        _render_solutions(report, locale, evidence_lookup), _render_next(report, locale),
        _render_validation(report, locale, evidence_lookup), _render_skills(report, locale),
        _render_evidence(report, locale),
    ]
    return "\n".join([
        '<!doctype html>',
        f'<html lang="{escape(_t(locale, "lang"))}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        _THEME_SCRIPT,
        f'<title>{escape(_t(locale, "title"))}</title><style>{_CSS}</style></head><body>',
        f'<header><h1>{escape(_t(locale, "title"))}</h1>'
        f'<p>{escape(_t(locale, "subtitle"))}</p>'
        f'<p class="meta report-meta">'
        f'<span>{escape(_t(locale, "report_id"))}: {escape(str(report.get("report_id")))}</span>'
        f'<span>{escape(_t(locale, "generated"))}: {escape(str(report.get("generated_at")))}</span>'
        f'<span>{escape(_t(locale, "language"))}</span></p></header>',
        f'<main>{toc}<section id="executive"><h2>{escape(_t(locale, "executive"))}</h2>'
        f'<div class="summary-grid">{summary_html}</div></section>',
        *sections,
        '</main></body></html>',
    ])


def _bilingual_paths(path: Path) -> dict[str, Path]:
    requested = Path(path)
    suffix = requested.suffix or ".html"
    english = requested if requested.suffix else requested.with_suffix(suffix)
    if english.stem.endswith(".zh-CN"):
        base = english.stem[:-6]
        chinese = english
        english = english.with_name(f"{base}.en-US{suffix}")
    else:
        base = english.stem[:-6] if english.stem.endswith(".en-US") else english.stem
        chinese = english.with_name(f"{base}.zh-CN{suffix}")
    return {"en-US": english, "zh-CN": chinese}


def write_manager_html(
    path: Path, report: Mapping[str, Any], locale: str = "en-US",
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_manager_html(report, locale), encoding="utf-8")
    return path


def write_manager_reports(path: Path, report: Mapping[str, Any]) -> dict[str, Path]:
    paths = _bilingual_paths(Path(path))
    temporary_paths: list[Path] = []
    try:
        for locale, target in paths.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(render_manager_html(report, locale), encoding="utf-8")
            temporary_paths.append(temporary)
        for locale, target in paths.items():
            temporary = target.with_name(target.name + ".tmp")
            temporary.replace(target)
        return paths
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def validate_manager_report(report: Mapping[str, Any]) -> list[str]:
    required = {
        "schema_version", "report_id", "generated_at", "products", "status",
        "problem_statement", "engineering_assessment", "analysis_process", "investigation_process",
        "evidence_chain", "possible_causes", "solutions", "next_actions",
        "missing_logs", "cross_validation", "invocation_chain",
        "confidence_summary", "quality",
    }
    errors = [f"missing field: {key}" for key in sorted(required - set(report))]
    if report.get("schema_version") != "1.0":
        errors.append("schema_version must equal 1.0")
    evidence_ids = {
        str(item.get("id")) for item in report.get("evidence_index") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    for section in ("evidence_chain", "possible_causes", "solutions"):
        for index, item in enumerate(report.get(section) or []):
            if not isinstance(item, Mapping):
                errors.append(f"{section}[{index}] must be an object")
                continue
            for ref in item.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errors.append(f"{section}[{index}] unresolved evidence ref: {ref}")
    for index, cause in enumerate(report.get("possible_causes") or []):
        if isinstance(cause, Mapping) and cause.get("status") == "proven":
            if not cause.get("evidence_refs"):
                errors.append(f"possible_causes[{index}] proven cause has no evidence")
            if not cause.get("falsifiers"):
                errors.append(f"possible_causes[{index}] proven cause has no falsifier")
    rounds = (
        (report.get("cross_validation") or {}).get("rounds")
        if isinstance(report.get("cross_validation"), Mapping) else []
    )
    round_ids: set[int] = set()
    for item in rounds or []:
        if not isinstance(item, Mapping):
            continue
        round_number = item.get("round")
        if isinstance(round_number, int):
            round_ids.add(round_number)
    if round_ids != {1, 2, 3}:
        errors.append("cross_validation.rounds must contain rounds 1, 2, and 3")
    return sorted(set(errors))


__all__ = [
    "build_manager_report", "render_manager_html", "validate_manager_report",
    "write_manager_html", "write_manager_reports",
]