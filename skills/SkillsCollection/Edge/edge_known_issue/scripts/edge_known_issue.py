#!/usr/bin/env python3
"""
edge_known_issue.py — prepare a deep-investigation plan for Edge known issues.

This skill does not call MCP tools directly. Instead, it emits a structured
query plan and suggested ADO / workiq / public-web calls that the agent can
execute, then merge back into the final answer or report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _shared.contract import (  # noqa: E402
    Finding, NextStep, SkillResult, fail, load_context,
)
from _shared import playbook  # noqa: E402

SKILL_ID = "edge_known_issue"

WORD_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{1,}", re.IGNORECASE)
VERSION_RE = re.compile(r"\b(\d{2,3})(?:\.\d+(?:\.\d+(?:\.\d+)?)?)?\b")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "browser", "by", "for",
    "from", "how", "i", "in", "is", "issue", "it", "known", "microsoft",
    "my", "of", "on", "or", "please", "problem", "site", "that", "the",
    "this", "to", "when", "why", "with", "works", "working", "work",
    "but", "after", "before", "update", "edge",
}

ALIASES = {
    "crash": "crash startup",
    "hangs": "hang",
    "hang": "please wait popup",
    "signin": "sign in",
    "sign-in": "sign in",
    "login": "sign in",
    "policy": "managed policy",
    "downloads": "download hub",
    "download": "download hub",
    "popup": "window opener popup",
    "ie": "ie mode",
}

LANE_SOURCE_TYPE = {
    "ado_microsoft": "fact_ado",
    "ado_cssdigital": "fact_ado",
    "workiq": "fact_workiq",
    "public_web": "fact_public",
    "recent_commits": "inference",
}

LANE_BASE_SCORE = {
    "ado_microsoft": 120,
    "ado_cssdigital": 115,
    "workiq": 80,
    "public_web": 70,
    "recent_commits": 40,
}

STRENGTH_BONUS = {
    "exact": 40,
    "adjacent": 10,
    "unknown": 0,
}

ALIGNMENT_BONUS = {
    "aligned": 15,
    "unknown": 0,
    "mismatch": -90,
    "conflict": -90,
}


def _sanitize_text(text: str) -> tuple[str, dict[str, Any]]:
    redactions = {
        "emails": len(EMAIL_RE.findall(text)),
        "urls": len(URL_RE.findall(text)),
    }
    sanitized = EMAIL_RE.sub("[redacted-email]", text)
    sanitized = URL_RE.sub("[redacted-url]", sanitized)
    return sanitized.strip(), {
        "sanitized": sanitized != text,
        "redactions": redactions,
    }


def _collect_problem_text(ctx: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("question", "summary", "problem_summary", "symptom", "title"):
        value = ctx.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    extra = ctx.get("extra") or {}
    for key in ("question", "summary", "problem_summary", "symptom", "title"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    for problem in ctx.get("problems") or []:
        if not isinstance(problem, dict):
            continue
        for key in ("summary", "title"):
            value = problem.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        lowered = item.lower()
        if lowered not in seen:
            seen.add(lowered)
            deduped.append(item)
    return " | ".join(deduped)


def _keyword_seed_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in WORD_RE.findall(text.lower()):
        if raw.isdigit():
            continue
        token = ALIASES.get(raw) or raw
        if token in STOP_WORDS:
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _build_keyword_groups(text: str) -> list[dict[str, Any]]:
    lowered = text.lower()
    versions = VERSION_RE.findall(lowered)
    seeds = _keyword_seed_tokens(lowered)

    groups: list[dict[str, Any]] = []
    if seeds:
        groups.append({
            "name": "symptom_core",
            "keywords": seeds[:3],
            "why": "Shortest symptom phrase for broad recall across ADO and workiq.",
        })
    if len(seeds) >= 4:
        groups.append({
            "name": "mechanism_variant",
            "keywords": seeds[1:5],
            "why": "Mechanism-oriented terms to reduce false positives from generic bugs.",
        })
    if versions:
        version_keywords = [f"Edge {versions[0]}"]
        if len(seeds) >= 2:
            version_keywords.extend(seeds[:2])
        groups.append({
            "name": "version_scoped",
            "keywords": version_keywords,
            "why": "Version-polarity check when the symptom changed after an update.",
        })
    if not groups:
        groups.append({
            "name": "fallback",
            "keywords": [text[:48].strip() or "edge known issue"],
            "why": "Fallback query when token extraction produced no useful keywords.",
        })
    return groups[:3]


def _build_query_plan(problem_text: str, problem_type: str | None) -> dict[str, Any]:
    keyword_groups = _build_keyword_groups(problem_text)
    return {
        "problem_type": problem_type,
        "keyword_groups": keyword_groups,
        "organization_resolution": {
            "required": True,
            "method": "Probe azuredevops core_list_projects at runtime and map suffix to org by returned URL/project list.",
            "expected_projects": {
                "microsoft": ["Edge"],
                "CSSDigital": ["Browsers"],
            },
        },
        "safety": {
            "sanitize_before_workiq": True,
            "sanitize_before_public_web": True,
            "disallow_customer_pii": True,
        },
        "recent_commit_scan": {
            "enabled_on_no_match": True,
            "window_days_default": 30,
            "window_days_max": 90,
            "reason": "Avoid concluding 'no known issue' before checking fresh adjacent changes.",
        },
    }


def _lane_calls(problem_text: str, query_plan: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for group in query_plan["keyword_groups"]:
        search_text = " ".join(group["keywords"])
        calls.append({
            "lane": "ado_microsoft",
            "tool_family": "azuredevops",
            "server_role": "microsoft",
            "logical_tool": "search_workitem",
            "project": "Edge",
            "query": search_text,
            "include_facets": True,
            "why": f"Search product bugs and linked PRs using keyword group '{group['name']}'.",
        })
        calls.append({
            "lane": "ado_cssdigital",
            "tool_family": "azuredevops",
            "server_role": "CSSDigital",
            "logical_tool": "search_workitem",
            "project": "Browsers",
            "query": search_text,
            "include_facets": True,
            "why": f"Search Top Issue / Solution / Known Issue items using keyword group '{group['name']}'.",
        })
        calls.append({
            "lane": "workiq",
            "tool": "mcp_workiq_ask_work_iq",
            "aliases": [
                "css edge", "css edge dev", "css edge release",
                "css edge policy", "css edge sign-in", "css edge web platform",
            ],
            "query": (
                "Search internal Edge discussion for sanitized symptom keywords: "
                f"{search_text}. Return Bug/Top Issue/PR IDs, fix versions, owner, workaround."
            ),
            "why": f"Cross-check internal discussion using keyword group '{group['name']}'.",
        })
        calls.append({
            "lane": "public_web",
            "tool": "fetch_webpage",
            "query": (
                "Microsoft Edge Chromium known issue "
                f"{search_text}"
            ),
            "urls": [
                "https://issues.chromium.org/",
                "https://learn.microsoft.com/microsoft-edge/",
                "https://support.microsoft.com/microsoft-edge",
            ],
            "why": f"Find public corroboration or platform-change documentation for '{group['name']}'.",
        })

    calls.append({
        "lane": "recent_commits",
        "tool_family": "azuredevops",
        "server_role": "microsoft",
        "logical_tool": "repo_search_commits",
        "query": " ".join(query_plan["keyword_groups"][0]["keywords"]),
        "only_when": "no credible exact match in ado/workiq/public lanes",
        "why": "Find fresh adjacent changes before concluding no known issue exists.",
    })
    return calls


def _lane_placeholders(calls: list[dict[str, Any]]) -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    for call in calls:
        lane = call["lane"]
        if lane not in lanes:
            lanes[lane] = {
                "status": "pending_agent_execution",
                "calls": [],
                "results": [],
            }
        lanes[lane]["calls"].append(call)
    return lanes


def _initial_evidence_matrix() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "pending-lane-execution",
            "source_type": "inference",
            "summary": "Deep-investigation plan generated; external evidence still needs MCP execution.",
            "status": "pending",
        }
    ]


def _extract_mcp_results(ctx: dict[str, Any]) -> dict[str, Any]:
    direct = ctx.get("mcp_results")
    if isinstance(direct, dict):
        return direct
    extra = ctx.get("extra") or {}
    nested = extra.get("mcp_results")
    return nested if isinstance(nested, dict) else {}


def _normalize_lane_results(
    planned_lanes: dict[str, Any],
    mcp_results: dict[str, Any],
) -> dict[str, Any]:
    lane_results = {
        lane: {
            "status": str(info.get("status") or "pending_agent_execution"),
            "calls": list(info.get("calls") or []),
            "results": list(info.get("results") or []),
        }
        for lane, info in planned_lanes.items()
    }

    incoming = mcp_results.get("lane_results")
    if not isinstance(incoming, dict):
        incoming = {
            lane: mcp_results.get(lane)
            for lane in LANE_SOURCE_TYPE
            if isinstance(mcp_results.get(lane), dict)
        }

    for lane, payload in incoming.items():
        if not isinstance(payload, dict):
            continue
        existing = lane_results.setdefault(lane, {
            "status": "pending_agent_execution",
            "calls": [],
            "results": [],
        })
        existing["status"] = str(payload.get("status") or ("completed" if payload.get("results") else existing["status"]))
        if isinstance(payload.get("calls"), list) and payload["calls"]:
            existing["calls"] = payload["calls"]
        if isinstance(payload.get("results"), list):
            existing["results"] = payload["results"]
        if payload.get("error"):
            existing["error"] = str(payload["error"])
    return lane_results


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "candidate"


def _candidate_from_result(lane: str, item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or item.get("summary") or item.get("id") or "candidate")
    item_id = str(item.get("id") or _slug(title))
    strength = str(item.get("match_strength") or ("adjacent" if lane == "recent_commits" else "unknown"))
    version_polarity = str(item.get("version_polarity") or "unknown")
    surface_layer = str(item.get("surface_layer") or "unknown")
    source_type = str(item.get("source_type") or LANE_SOURCE_TYPE.get(lane, "inference"))

    rank_score = item.get("rank_score")
    if not isinstance(rank_score, (int, float)):
        rank_score = (
            LANE_BASE_SCORE.get(lane, 0)
            + STRENGTH_BONUS.get(strength, 0)
            + ALIGNMENT_BONUS.get(version_polarity, 0)
            + ALIGNMENT_BONUS.get(surface_layer, 0)
        )

    rank_reason = item.get("rank_reason")
    if not isinstance(rank_reason, str) or not rank_reason.strip():
        rank_reason = (
            f"lane={lane}; strength={strength}; version_polarity={version_polarity}; "
            f"surface_layer={surface_layer}"
        )

    return {
        "id": item_id,
        "title": title,
        "url": item.get("url"),
        "summary": item.get("summary") or title,
        "lane": lane,
        "source_type": source_type,
        "match_strength": strength,
        "rank_score": rank_score,
        "rank_reason": rank_reason,
        "version_polarity": version_polarity,
        "surface_layer": surface_layer,
        "fix_version": item.get("fix_version"),
        "workaround": item.get("workaround"),
        "owner": item.get("owner"),
        "evidence_id": f"{lane}:{item_id}",
    }


def _build_evidence_matrix(lane_results: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for lane, payload in lane_results.items():
        results = payload.get("results") or []
        for item in results:
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_result(lane, item)
            evidence.append({
                "evidence_id": candidate["evidence_id"],
                "source_type": candidate["source_type"],
                "lane": lane,
                "summary": candidate["summary"],
                "title": candidate["title"],
                "url": candidate.get("url"),
                "match_strength": candidate["match_strength"],
                "version_polarity": candidate["version_polarity"],
                "surface_layer": candidate["surface_layer"],
                "status": "collected",
            })
    if evidence:
        return evidence
    return _initial_evidence_matrix()


def _build_match_ranking(lane_results: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for lane, payload in lane_results.items():
        for item in payload.get("results") or []:
            if isinstance(item, dict):
                candidates.append(_candidate_from_result(lane, item))

    candidates.sort(key=lambda c: (-float(c["rank_score"]), c["lane"], c["title"]))
    best = candidates[0] if candidates else None
    return {
        "ranked_best_match": best,
        "candidates": candidates,
        "ranking_model": "v1",
    }


def _aggregate_alignment(candidates: list[dict[str, Any]], field: str) -> dict[str, Any]:
    if not candidates:
        return {
            "status": "pending",
            "expected_behavior": "Awaiting MCP evidence before alignment can be evaluated.",
        }

    statuses = [str(c.get(field) or "unknown") for c in candidates]
    if any(s in {"conflict", "mismatch"} for s in statuses):
        status = "conflict"
    elif any(s == "aligned" for s in statuses):
        status = "aligned"
    else:
        status = "unknown"

    return {
        "status": status,
        "counts": {
            "aligned": sum(1 for s in statuses if s == "aligned"),
            "unknown": sum(1 for s in statuses if s == "unknown"),
            "conflict": sum(1 for s in statuses if s in {"conflict", "mismatch"}),
        },
        "best_candidate_value": statuses[0],
        "expected_behavior": (
            "An exact match must align with the user's working/failing version direction."
            if field == "version_polarity"
            else "An exact match must be on the same surface, such as browser tab vs embedded WebView2."
        ),
    }


def _build_not_found_gaps(lane_results: dict[str, Any], ranking: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for lane, payload in lane_results.items():
        results = payload.get("results") or []
        status = str(payload.get("status") or "pending_agent_execution")
        if status in {"pending", "pending_agent_execution"}:
            gaps.append(f"{lane} has not been executed yet.")
        elif status == "completed" and not results:
            gaps.append(f"{lane} returned no relevant evidence.")
        elif payload.get("error"):
            gaps.append(f"{lane} failed: {payload['error']}")

    best = ranking.get("ranked_best_match")
    if not best:
        gaps.append("No ranked candidate was found after MCP result merge.")
    elif best.get("match_strength") != "exact":
        gaps.append("No exact known-issue match is confirmed yet; best candidate is adjacent.")
    return gaps


def _confidence_from_ranking(ranking: dict[str, Any]) -> str:
    best = ranking.get("ranked_best_match")
    if not isinstance(best, dict):
        return "low"
    if (
        best.get("match_strength") == "exact"
        and best.get("version_polarity") == "aligned"
        and best.get("surface_layer") == "aligned"
    ):
        return "high"
    if best.get("match_strength") in {"exact", "adjacent"}:
        return "medium"
    return "low"


def _apply_mcp_results(raw_block: dict[str, Any], mcp_results: dict[str, Any]) -> None:
    lane_results = _normalize_lane_results(raw_block["lane_results"], mcp_results)
    ranking = _build_match_ranking(lane_results)
    candidates = ranking["candidates"]

    raw_block["lane_results"] = lane_results
    raw_block["evidence_matrix"] = _build_evidence_matrix(lane_results)
    raw_block["match_ranking"] = ranking
    raw_block["version_polarity_check"] = _aggregate_alignment(candidates, "version_polarity")
    raw_block["surface_layer_check"] = _aggregate_alignment(candidates, "surface_layer")
    raw_block["not_found_gaps"] = _build_not_found_gaps(lane_results, ranking)
    raw_block["adjacent_changes"] = [
        c for c in candidates if c.get("lane") == "recent_commits"
    ]
    org_resolution = mcp_results.get("organization_resolution")
    if isinstance(org_resolution, dict):
        raw_block["query_plan"]["organization_resolution"]["resolved"] = org_resolution
    if isinstance(mcp_results.get("customer_ready_message"), str):
        raw_block["customer_ready_message"] = mcp_results["customer_ready_message"]


def _escape_html(value: Any) -> str:
        text = str(value if value is not None else "")
        return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;")
        )


def _resolve_report_paths(workspace_root: Path) -> tuple[Path, Path]:
        temp_dir = workspace_root / "Temp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        base_zh = temp_dir / "edge-known-issue-report.zh.html"
        base_en = temp_dir / "edge-known-issue-report.en.html"
        if not base_zh.exists() and not base_en.exists():
                return base_zh, base_en

        idx = 1
        while True:
                p_zh = temp_dir / f"edge-known-issue-report.zh.{idx}.html"
                p_en = temp_dir / f"edge-known-issue-report.en.{idx}.html"
                if not p_zh.exists() and not p_en.exists():
                        return p_zh, p_en
                idx += 1


def _render_report_html(
        *,
        lang: str,
        best_match: dict[str, Any] | None,
        evidence: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        problem_text: str,
        version_status: str,
        surface_status: str,
        generated_at: str,
        other_filename: str,
        customer_ready_message: str,
) -> str:
        is_zh = lang == "zh-CN"

        title = "Edge Known Issue Report" if not is_zh else "Edge \u5df2\u77e5\u95ee\u9898\u8c03\u67e5\u62a5\u544a"
        subtitle = (
                "Evidence-based summary generated from ADO/workiq/public lanes"
                if not is_zh
                else "\u57fa\u4e8e ADO/workiq/public \u771f\u5b9e\u8bc1\u636e\u7684\u5f52\u5e76\u7ed3\u679c"
        )
        links_heading = "1. Links" if not is_zh else "1. \u94fe\u63a5"
        internal_heading = "Internal discussion" if not is_zh else "\u5185\u90e8\u8ba8\u8bba"
        public_heading = "Public references" if not is_zh else "\u516c\u5f00\u53c2\u8003"
        desc_heading = "2. Description and repro" if not is_zh else "2. \u95ee\u9898\u63cf\u8ff0\u4e0e\u590d\u73b0"
        mapping_heading = "3. Symptom to defect mapping" if not is_zh else "3. \u75c7\u72b6\u5230\u7f3a\u9677\u6620\u5c04"
        root_cause_heading = "4. Root cause" if not is_zh else "4. \u6839\u56e0"
        code_heading = "5. Code explanation" if not is_zh else "5. \u4ee3\u7801\u8bf4\u660e"
        workaround_heading = "6. Workarounds" if not is_zh else "6. \u89c4\u907f\u65b9\u6848"
        fix_heading = "7. Fix progress and version" if not is_zh else "7. \u4fee\u590d\u8fdb\u5ea6\u4e0e\u7248\u672c"
        adjacent_heading = "7b. Recent adjacent changes" if not is_zh else "7b. \u8fd1\u671f\u76f8\u90bb\u53d8\u66f4"
        crm_heading = "8. Customer Ready Message" if not is_zh else "8. \u5ba2\u6237\u53ef\u7528\u6d88\u606f"
        ranking_heading = "Ranking details" if not is_zh else "\u6392\u5e8f\u660e\u7ec6"
        footer_note = (
                "Internal material. Evidence-based output."
                if not is_zh
                else "\u5185\u90e8\u6750\u6599\uff0c\u4ec5\u57fa\u4e8e\u8bc1\u636e\u8f93\u51fa\u3002"
        )

        best_line = ""
        if best_match:
                best_line = (
                        f"<p><strong>{_escape_html(best_match.get('title'))}</strong> "
                        f"(id={_escape_html(best_match.get('id'))}, "
                        f"strength={_escape_html(best_match.get('match_strength'))}, "
                        f"version={_escape_html(version_status)}, surface={_escape_html(surface_status)})</p>"
                )
        else:
                best_line = (
                        "<p>No ranked best match.</p>"
                        if not is_zh
                        else "<p>\u672a\u5f97\u5230\u6700\u4f73\u5339\u914d\u3002</p>"
                )

        ado_rows = [c for c in candidates if c.get("lane") in {"ado_microsoft", "ado_cssdigital"}]
        workiq_rows = [c for c in candidates if c.get("lane") == "workiq"]
        public_rows = [c for c in candidates if c.get("lane") == "public_web"]
        adjacent_rows = [c for c in candidates if c.get("lane") == "recent_commits"]

        status_by_lane: dict[str, str] = {
            "ado_microsoft": "Fact",
            "ado_cssdigital": "Fact",
            "workiq": "Fact (workiq)",
            "public_web": "Fact (public)",
            "recent_commits": "Inference",
        }
        if is_zh:
            status_by_lane = {
                "ado_microsoft": "\u4e8b\u5b9e",
                "ado_cssdigital": "\u4e8b\u5b9e",
                "workiq": "\u4e8b\u5b9e(workiq)",
                "public_web": "\u4e8b\u5b9e(public)",
                "recent_commits": "\u63a8\u65ad",
            }

        def _rows_html(rows: list[dict[str, Any]], include_lane: bool = False) -> str:
            if not rows:
                colspan = "7" if include_lane else "6"
                nf = "not found" if not is_zh else "\u672a\u627e\u5230"
                return f"<tr><td colspan='{colspan}'>{nf}</td></tr>"
            parts: list[str] = []
            for row in rows:
                link = row.get("url")
                title_cell = _escape_html(row.get("title"))
                if isinstance(link, str) and link.strip():
                    title_cell = f"<a href='{_escape_html(link)}'>{title_cell}</a>"
                lane_cell = f"<td>{_escape_html(row.get('lane'))}</td>" if include_lane else ""
                parts.append(
                    "<tr>"
                    f"<td>{_escape_html(row.get('id'))}</td>"
                    f"{lane_cell}"
                    f"<td>{title_cell}</td>"
                    f"<td>{_escape_html(row.get('match_strength'))}</td>"
                    f"<td>{_escape_html(row.get('version_polarity'))}</td>"
                    f"<td>{_escape_html(row.get('surface_layer'))}</td>"
                    f"<td>{_escape_html(row.get('fix_version') or '-')}</td>"
                    "</tr>"
                )
            return "".join(parts)

        def _mapping_rows_html() -> str:
            if not candidates:
                nf = "not found" if not is_zh else "\u672a\u627e\u5230"
                return f"<tr><td colspan='5'>{nf}</td></tr>"
            parts: list[str] = []
            for idx, row in enumerate(candidates[:5], start=1):
                mechanism = row.get("summary") or row.get("title")
                evidence_label = status_by_lane.get(str(row.get("lane")), "Inference")
                parts.append(
                    "<tr>"
                    f"<td>{idx}</td>"
                    f"<td>{_escape_html(problem_text)}</td>"
                    f"<td>{_escape_html(mechanism)}</td>"
                    f"<td>{_escape_html(row.get('id'))}</td>"
                    f"<td>{_escape_html(evidence_label)}</td>"
                    "</tr>"
                )
            return "".join(parts)

        def _root_cause_html() -> str:
            focus = candidates[:3]
            if not focus:
                return "<p>not found</p>" if not is_zh else "<p>\u672a\u627e\u5230</p>"
            blocks: list[str] = []
            for row in focus:
                title_cell = _escape_html(row.get("title"))
                if row.get("url"):
                    title_cell = f"<a href='{_escape_html(row.get('url'))}'>{title_cell}</a>"
                blocks.append(
                    "<p>"
                    f"<strong>{title_cell}</strong><br/>"
                    f"{_escape_html(row.get('summary'))}<br/>"
                    f"lane={_escape_html(row.get('lane'))}, strength={_escape_html(row.get('match_strength'))}, "
                    f"version={_escape_html(row.get('version_polarity'))}, surface={_escape_html(row.get('surface_layer'))}"
                    "</p>"
                )
            return "".join(blocks)

        def _workaround_html() -> str:
            values: list[str] = []
            seen: set[str] = set()
            for row in candidates:
                val = row.get("workaround")
                if isinstance(val, str) and val.strip() and val not in seen:
                    seen.add(val)
                    values.append(val)
            if not values:
                return "<li>not found</li>" if not is_zh else "<li>\u672a\u627e\u5230</li>"
            return "".join(f"<li>{_escape_html(v)}</li>" for v in values)

        def _fix_progress_rows_html() -> str:
            if not candidates:
                nf = "not found" if not is_zh else "\u672a\u627e\u5230"
                return f"<tr><td colspan='4'>{nf}</td></tr>"
            parts: list[str] = []
            for row in candidates:
                parts.append(
                    "<tr>"
                    f"<td>{_escape_html(row.get('id'))}</td>"
                    f"<td>{_escape_html(row.get('lane'))}</td>"
                    f"<td>{_escape_html(row.get('fix_version') or ('not found' if not is_zh else '\u672a\u627e\u5230'))}</td>"
                    f"<td>{_escape_html(row.get('match_strength'))}</td>"
                    "</tr>"
                )
            return "".join(parts)

        def _adjacent_rows_html() -> str:
            if not adjacent_rows:
                return (
                    "<p>No adjacent changes found in this run.</p>"
                    if not is_zh
                    else "<p>\u672c\u6b21\u672a\u53d1\u73b0\u76f8\u90bb\u53d8\u66f4\u3002</p>"
                )
            parts: list[str] = []
            for row in adjacent_rows:
                title_cell = _escape_html(row.get("title"))
                if row.get("url"):
                    title_cell = f"<a href='{_escape_html(row.get('url'))}'>{title_cell}</a>"
                parts.append(
                    "<p>"
                    f"<strong>{title_cell}</strong> "
                    f"[{_escape_html('Inference' if not is_zh else '\u63a8\u65ad')}]<br/>"
                    f"{_escape_html(row.get('summary'))}"
                    "</p>"
                )
            return "".join(parts)

        crm_value = None
        if isinstance(best_match, dict) and isinstance(best_match.get("customer_ready_message"), str):
            crm_value = best_match.get("customer_ready_message")
        if not crm_value and customer_ready_message.strip():
            crm_value = customer_ready_message

        evidence_count = len(evidence)

        return f"""<!doctype html>
<html lang=\"{_escape_html(lang)}\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{_escape_html(title)}</title>
    <style>
        :root {{ --bg:#ffffff; --ink:#1f2328; --muted:#656d76; --line:#d0d7de; --soft:#f6f8fa; --brand:#0969da; }}
        body {{ margin:0; background:var(--bg); color:var(--ink); font-family:Segoe UI,Arial,sans-serif; }}
        .wrap {{ max-width: 920px; margin: 0 auto; padding: 24px; }}
        h1,h2 {{ margin: 0 0 12px; }}
        p {{ margin: 8px 0; }}
        .muted {{ color: var(--muted); }}
        .langbar {{ margin: 0 0 16px; padding: 10px 12px; border:1px solid var(--line); background:var(--soft); }}
        .card {{ border:1px solid var(--line); border-radius:8px; padding:14px; margin: 14px 0; }}
        table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
        th,td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; font-size:13px; }}
        th {{ background:var(--soft); }}
        a {{ color:var(--brand); text-decoration:none; }}
        a:hover {{ text-decoration:underline; }}
        code {{ background:var(--soft); padding:2px 4px; border-radius:4px; }}
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"langbar\">Switch language: <a href=\"{_escape_html(other_filename)}\">{_escape_html(other_filename)}</a></div>
        <h1>{_escape_html(title)}</h1>
        <p class=\"muted\">{_escape_html(subtitle)}</p>

        <div class=\"card\">
            <h2>{_escape_html(links_heading)}</h2>
            {best_line}
            <p class=\"muted\">Evidence count: {_escape_html(evidence_count)} | Generated at: {_escape_html(generated_at)}</p>
            <table>
                <thead>
                    <tr><th>ID</th><th>Title</th><th>Strength</th><th>Version</th><th>Surface</th><th>Fix</th></tr>
                </thead>
                <tbody>
                    {_rows_html(ado_rows)}
                </tbody>
            </table>
            <h3>{_escape_html(internal_heading)}</h3>
            <table>
                <thead>
                    <tr><th>ID</th><th>Title</th><th>Strength</th><th>Version</th><th>Surface</th><th>Fix</th></tr>
                </thead>
                <tbody>
                    {_rows_html(workiq_rows)}
                </tbody>
            </table>
            <h3>{_escape_html(public_heading)}</h3>
            <table>
                <thead>
                    <tr><th>ID</th><th>Title</th><th>Strength</th><th>Version</th><th>Surface</th><th>Fix</th></tr>
                </thead>
                <tbody>
                    {_rows_html(public_rows)}
                </tbody>
            </table>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(desc_heading)}</h2>
            <p>{_escape_html(problem_text)}</p>
            <p class=\"muted\">version_polarity_check={_escape_html(version_status)}; surface_layer_check={_escape_html(surface_status)}</p>
            <ol>
                <li>{_escape_html(problem_text)}</li>
                <li>{_escape_html('Evidence merged from ADO/workiq/public lanes' if not is_zh else '\u57fa\u4e8e ADO/workiq/public \u8bc1\u636e\u5f52\u5e76')}</li>
            </ol>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(mapping_heading)}</h2>
            <table>
                <thead>
                    <tr><th>#</th><th>{_escape_html('Symptom' if not is_zh else '\u75c7\u72b6')}</th><th>{_escape_html('Defect mechanism' if not is_zh else '\u7f3a\u9677\u673a\u5236')}</th><th>{_escape_html('Work item' if not is_zh else 'Work Item')}</th><th>{_escape_html('Evidence label' if not is_zh else '\u8bc1\u636e\u6807\u7b7e')}</th></tr>
                </thead>
                <tbody>
                    {_mapping_rows_html()}
                </tbody>
            </table>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(root_cause_heading)}</h2>
            {_root_cause_html()}
        </div>

        <div class=\"card\">
            <h2>{_escape_html(code_heading)}</h2>
            <p>{_escape_html('not found' if not is_zh else '\u672a\u627e\u5230')} (no code diff payload was provided in this run)</p>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(workaround_heading)}</h2>
            <ul>
                {_workaround_html()}
            </ul>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(fix_heading)}</h2>
            <table>
                <thead>
                    <tr><th>ID</th><th>Lane</th><th>{_escape_html('Fix version' if not is_zh else '\u4fee\u590d\u7248\u672c')}</th><th>{_escape_html('Match' if not is_zh else '\u5339\u914d')}</th></tr>
                </thead>
                <tbody>
                    {_fix_progress_rows_html()}
                </tbody>
            </table>
            <h3>{_escape_html(adjacent_heading)}</h3>
            {_adjacent_rows_html()}
        </div>

        <div class=\"card\">
            <h2>{_escape_html(crm_heading)}</h2>
            <p>{_escape_html(crm_value or ('not found' if not is_zh else '\u672a\u627e\u5230'))}</p>
        </div>

        <div class=\"card\">
            <h2>{_escape_html(ranking_heading)}</h2>
            <table>
                <thead>
                    <tr><th>ID</th><th>Lane</th><th>Title</th><th>Strength</th><th>Version</th><th>Surface</th><th>Fix</th></tr>
                </thead>
                <tbody>
                    {_rows_html(candidates, include_lane=True)}
                </tbody>
            </table>
        </div>

        <p class=\"muted\">{_escape_html(footer_note)}</p>
    </div>
</body>
</html>
"""


def _emit_bilingual_report(raw_block: dict[str, Any], workspace_root: Path) -> dict[str, str]:
        zh_path, en_path = _resolve_report_paths(workspace_root)

        ranking = raw_block.get("match_ranking") or {}
        best = ranking.get("ranked_best_match")
        candidates = list(ranking.get("candidates") or [])
        evidence = list(raw_block.get("evidence_matrix") or [])
        problem_text = str(raw_block.get("problem_text") or "")
        version_status = str((raw_block.get("version_polarity_check") or {}).get("status") or "unknown")
        surface_status = str((raw_block.get("surface_layer_check") or {}).get("status") or "unknown")
        generated_at = datetime.now(timezone.utc).isoformat()
        customer_ready_message = str(raw_block.get("customer_ready_message") or "")

        zh_html = _render_report_html(
                lang="zh-CN",
                best_match=best if isinstance(best, dict) else None,
                evidence=evidence,
                candidates=candidates,
                problem_text=problem_text,
                version_status=version_status,
                surface_status=surface_status,
                generated_at=generated_at,
                other_filename=en_path.name,
                customer_ready_message=customer_ready_message,
        )
        en_html = _render_report_html(
                lang="en",
                best_match=best if isinstance(best, dict) else None,
                evidence=evidence,
                candidates=candidates,
                problem_text=problem_text,
                version_status=version_status,
                surface_status=surface_status,
                generated_at=generated_at,
                other_filename=zh_path.name,
                customer_ready_message=customer_ready_message,
        )

        zh_path.write_text(zh_html, encoding="utf-8")
        en_path.write_text(en_html, encoding="utf-8")

        return {
                "zh": str(zh_path),
                "en": str(en_path),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Edge known-issue deep investigation planner")
    ap.add_argument("context", nargs="?", default=None)
    args = ap.parse_args(argv)

    ctx = load_context([args.context] if args.context else [])
    edge_root = Path(__file__).resolve().parents[2]
    workspace_root = edge_root.parent
    extra = ctx.get("extra") or {}
    report_mode = str(extra.get("report_mode") or "json")
    use_known_issue_mcp = bool(extra.get("use_known_issue_mcp", True))
    problem_type = str(ctx.get("problem_type") or "known_issue")

    problem_text = _collect_problem_text(ctx)
    if not problem_text:
        fail(SKILL_ID, "Missing problem description. Provide question, summary, symptom, or problems[].summary.")
        return 1

    sanitized_text, sanitize_meta = _sanitize_text(problem_text)
    query_plan = _build_query_plan(sanitized_text, problem_type)
    suggested_calls = _lane_calls(sanitized_text, query_plan) if use_known_issue_mcp else []
    mcp_results = _extract_mcp_results(ctx)

    findings = [
        Finding(
            summary="Prepared a deep-investigation plan for known-issue triage.",
            severity="info",
            evidence={
                "problem_type": problem_type,
                "keyword_groups": query_plan["keyword_groups"],
            },
        )
    ]
    if sanitize_meta["sanitized"]:
        findings.append(Finding(
            summary="Sensitive terms were sanitized before planning workiq/public queries.",
            severity="info",
            evidence=sanitize_meta,
        ))

    recommendations = [
        "Use short keyword groups per lane; avoid a single long search phrase.",
        "Validate version polarity and surface layer before treating any candidate as an exact match.",
        "If all three lanes miss, run the recent-commits scan before concluding no known issue exists.",
    ]
    if use_known_issue_mcp:
        recommendations.extend([
            "[mcp:azuredevops] Agent: resolve the microsoft/CSSDigital tool-family mapping at runtime, then execute raw.known_issue_lookup.suggested_calls for ADO lanes.",
            "[mcp:mcp_workiq_ask_work_iq] Agent: execute workiq calls using the sanitized symptom keywords only.",
            "[mcp:fetch_webpage] Agent: use public-web calls as corroboration, not as primary root-cause authority unless the issue is a platform/vendor change.",
        ])

    raw_block: dict[str, Any] = {
        "problem_text": sanitized_text,
        "report_mode": report_mode,
        "query_plan": query_plan,
        "lane_results": _lane_placeholders(suggested_calls),
        "evidence_matrix": _initial_evidence_matrix(),
        "match_ranking": {
            "ranked_best_match": None,
            "candidates": [],
            "ranking_model": "v1",
        },
        "version_polarity_check": {
            "status": "pending",
            "expected_behavior": "An exact match must align with the user's working/failing version direction.",
        },
        "surface_layer_check": {
            "status": "pending",
            "expected_behavior": "An exact match must be on the same surface, such as browser tab vs embedded WebView2.",
        },
        "not_found_gaps": [
            "No ADO/workiq/public evidence has been fetched yet.",
        ],
        "adjacent_changes": [],
        "known_issue_lookup": {
            "enabled": use_known_issue_mcp,
            "rationale": (
                "edge_known_issue follows a deep-investigation workflow but the "
                "Python skill cannot call MCP tools directly. The agent should "
                "execute the suggested lane calls, then merge the fetched evidence "
                "back into the final answer or bilingual HTML report."
            ),
            "suggested_calls": suggested_calls,
            "query_context": {
                "problem_type": problem_type,
                "report_mode": report_mode,
                "sanitized": sanitize_meta["sanitized"],
            },
        },
    }

    if mcp_results:
        _apply_mcp_results(raw_block, mcp_results)

    result = SkillResult(
        skill=SKILL_ID,
        ok=True,
        findings=findings,
        confidence=_confidence_from_ranking(raw_block["match_ranking"]),
        recommendations=recommendations,
        raw=raw_block,
    )

    best_match = raw_block["match_ranking"].get("ranked_best_match")
    if isinstance(best_match, dict):
        findings.append(Finding(
            summary=(
                f"Best known-issue candidate: {best_match['title']} "
                f"({best_match['match_strength']})"
            ),
            severity="info",
            evidence={
                "lane": best_match.get("lane"),
                "rank_score": best_match.get("rank_score"),
                "version_polarity": best_match.get("version_polarity"),
                "surface_layer": best_match.get("surface_layer"),
            },
        ))
    elif mcp_results:
        findings.append(Finding(
            summary="MCP results were merged but no credible candidate was ranked.",
            severity="warning",
            evidence={"not_found_gaps": raw_block["not_found_gaps"]},
        ))

    if report_mode == "html_bilingual":
        paths = _emit_bilingual_report(raw_block, workspace_root)
        raw_block["bilingual_report"] = {
            "generated": True,
            "paths": paths,
        }
        findings.append(Finding(
            summary="Bilingual HTML report generated from merged evidence.",
            severity="info",
            evidence={"paths": paths},
        ))

    pending_lanes = any(
        str((info or {}).get("status") or "") in {"pending", "pending_agent_execution"}
        for info in raw_block["lane_results"].values()
    )

    if use_known_issue_mcp and pending_lanes:
        result.next_steps.append(NextStep(
            action=(
                "Resolve ADO org mapping, run the ADO/workiq/public lane calls in "
                "raw.known_issue_lookup.suggested_calls, then rank candidates by "
                "match strength, version polarity, and surface-layer alignment."
            ),
            why="Deep known-issue analysis depends on external evidence fetched via MCP.",
            skill="edge_known_issue_mcp",
        ))
    if report_mode == "html_bilingual":
        generated = bool(raw_block.get("bilingual_report"))
        action = (
            "Review and share the generated bilingual HTML reports, or rerun report "
            "generation after refreshing MCP evidence for the latest links and fix "
            "timeline."
            if generated
            else (
                "After lane execution and evidence ranking, generate bilingual HTML "
                "reports with links, symptom-to-defect mapping, root cause, code "
                "explanation, workaround, and fix progress sections."
            )
        )
        result.next_steps.append(NextStep(
            action=action,
            why="The caller requested report_mode=html_bilingual.",
            skill="edge-known-issue-report",
        ))

    playbook.merge_into_result(result, [problem_type])
    result.emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())