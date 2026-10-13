#!/usr/bin/env python3
"""Evidence-grounded multi-agent / multi-model cross-validation reducer.

Provider subprocesses are configured only through the trusted process
environment (``SKILLS_VALIDATOR_PROVIDERS_JSON``), never through incident
input. Each provider receives the same redacted evidence pack and cannot see
other providers' output. Reconciliation is deterministic and code-driven.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

COMMON_ROOT = Path(__file__).resolve().parents[3]
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))

from investigation_protocol import (  # noqa: E402
    EvidenceGap,
    EvidenceState,
    build_collection_plan,
    derive_evidence_state,
)

SKILL_ID = "validator"
PROVIDER_ENV = "SKILLS_VALIDATOR_PROVIDERS_JSON"
MAX_EVIDENCE_ITEMS = 120
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 512 * 1024
DEFAULT_PROVIDER_TIMEOUT = 45
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|authorization|cookie|set-cookie|client_secret|"
    r"access_token|refresh_token|api_key|private_key|sas_token)"
)
TOKEN_RE = re.compile(
    r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+|([?&](?:sig|token|code|key)=)[^&#\s]+"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9]+")
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in",
    "is", "of", "on", "or", "the", "to", "was", "were", "with",
}


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


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 4:
        return "<max-depth>"
    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 20:
                output["_truncated"] = True
                break
            output[str(child_key)] = _sanitize(
                child_value, key=str(child_key), depth=depth + 1
            )
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [
            _sanitize(item, key=key, depth=depth + 1) for item in items[:20]
        ] + (["<truncated>"] if len(items) > 20 else [])
    if isinstance(value, str):
        if key.lower() in {"source_file", "file", "path", "full_path"}:
            return {
                "basename": Path(value).name,
                "path_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        redacted = URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
        redacted = TOKEN_RE.sub(
            lambda match: f"{match.group(1) or match.group(2)}<redacted>", redacted
        )
        return redacted[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _children_from_context(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    extra = ctx.get("extra") or {}
    candidates = [
        extra.get("children"),
        extra.get("results"),
        ctx.get("children"),
        ctx.get("results"),
        (ctx.get("raw") or {}).get("children") if isinstance(ctx.get("raw"), dict) else None,
        (ctx.get("raw") or {}).get("results") if isinstance(ctx.get("raw"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return {
                str(skill): result for skill, result in candidate.items()
                if isinstance(result, dict)
            }
    return {}


def _stable_evidence_id(
    *,
    skill: str,
    kind: str,
    summary: Any,
    evidence: Any,
    producer_id: Any = None,
) -> str:
    if producer_id:
        return str(producer_id)
    identity = {
        "skill": skill,
        "kind": kind,
        "summary": _sanitize(summary),
        "evidence": _sanitize(evidence),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        .encode("utf-8")
    ).hexdigest()[:16]
    return f"E-{digest}"


def build_evidence_pack(ctx: dict[str, Any]) -> dict[str, Any]:
    """Create bounded redacted facts; raw logs and full source never enter it."""
    children = _children_from_context(ctx)
    items: list[dict[str, Any]] = []
    background_excluded: dict[str, int] = {}
    seen_ids: set[str] = set()
    truncated = False
    for skill in sorted(children):
        result = children[skill]
        if not result.get("ok"):
            summary = str(result.get("error") or "skill failed")[:500]
            stable_id = _stable_evidence_id(
                skill=skill, kind="skill_failure", summary=summary, evidence={}
            )
            if stable_id in seen_ids:
                continue
            seen_ids.add(stable_id)
            items.append({
                "id": stable_id,
                "legacy_id": f"E{len(items) + 1:04d}",
                "skill": skill,
                "kind": "skill_failure",
                "summary": summary,
                "severity": "warning",
                "evidence": {},
            })
            continue
        for finding in result.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            raw_evidence = finding.get("evidence") or {}
            if (
                isinstance(raw_evidence, dict)
                and raw_evidence.get("signal_classification") == "background"
            ):
                background_excluded[skill] = background_excluded.get(skill, 0) + 1
                continue
            evidence = _sanitize(finding.get("evidence") or {})
            summary = _sanitize(str(finding.get("summary") or ""))
            producer_id = finding.get("evidence_id")
            if not producer_id and isinstance(finding.get("evidence"), dict):
                producer_id = finding["evidence"].get("evidence_id")
            stable_id = _stable_evidence_id(
                skill=skill,
                kind="finding",
                summary=summary,
                evidence=evidence,
                producer_id=producer_id,
            )
            if stable_id in seen_ids:
                continue
            seen_ids.add(stable_id)
            items.append({
                "id": stable_id,
                "legacy_id": f"E{len(items) + 1:04d}",
                "skill": skill,
                "kind": "finding",
                "summary": summary,
                "severity": str(finding.get("severity") or "info"),
                "evidence": evidence,
                **({"producer_evidence_id": _sanitize(producer_id)} if producer_id else {}),
                "source_confidence": str(result.get("confidence") or "low"),
            })
            if len(items) >= MAX_EVIDENCE_ITEMS:
                truncated = True
                break
        if len(items) >= MAX_EVIDENCE_ITEMS:
            break

    if not items:
        for finding in ctx.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            raw_evidence = finding.get("evidence") or {}
            if (
                isinstance(raw_evidence, dict)
                and raw_evidence.get("signal_classification") == "background"
            ):
                background_excluded[str(ctx.get("skill") or "input")] = (
                    background_excluded.get(str(ctx.get("skill") or "input"), 0) + 1
                )
                continue
            evidence = _sanitize(finding.get("evidence") or {})
            summary = _sanitize(str(finding.get("summary") or ""))
            skill = str(ctx.get("skill") or "input")
            stable_id = _stable_evidence_id(
                skill=skill,
                kind="finding",
                summary=summary,
                evidence=evidence,
                producer_id=finding.get("evidence_id"),
            )
            if stable_id in seen_ids:
                continue
            seen_ids.add(stable_id)
            items.append({
                "id": stable_id,
                "legacy_id": f"E{len(items) + 1:04d}",
                "skill": skill,
                "kind": "finding",
                "summary": summary,
                "severity": str(finding.get("severity") or "info"),
                "evidence": evidence,
                "source_confidence": str(ctx.get("confidence") or "low"),
            })

    while items:
        encoded = json.dumps(items, ensure_ascii=True).encode("utf-8")
        if len(encoded) <= MAX_EVIDENCE_BYTES:
            break
        items.pop()
        truncated = True

    problems = []
    for problem in ctx.get("problems") or (ctx.get("extra") or {}).get("problems") or []:
        if isinstance(problem, dict):
            problems.append({
                "type": str(problem.get("type") or "unknown"),
                "severity": str(problem.get("severity") or "warning"),
                "summary": _sanitize(problem.get("summary") or problem.get("description") or ""),
            })
    return {
        "schema_version": "1.0",
        "problem_type": ctx.get("problem_type"),
        "problems": problems[:20],
        "time_range": _sanitize(ctx.get("time_range") or {}),
        "items": items,
        "integrity": {
            "items": len(items),
            "truncated": truncated,
            "max_items": MAX_EVIDENCE_ITEMS,
            "max_bytes": MAX_EVIDENCE_BYTES,
            "raw_logs_included": False,
            "full_source_included": False,
            "background_observations_excluded": sum(background_excluded.values()),
            "background_excluded_by_skill": background_excluded,
        },
    }


def provider_request(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "Independently analyze the evidence and return JSON only.",
        "rules": [
            "Do not assume another model's conclusion.",
            "Every claim must cite one or more evidence_refs from the pack.",
            "Separate observed facts from hypotheses.",
            "List falsifiers and the next evidence needed when uncertain.",
            "Do not infer that missing evidence proves absence.",
            "Background observations were excluded from claimable evidence; do not reconstruct them as causes.",
            "Event volume and time co-occurrence alone do not establish causality.",
        ],
        "response_schema": {
            "analyzer": "string",
            "model": "model/deployment identifier",
            "agent_id": "independent agent/run identifier",
            "independence_group": "outputs sharing context/reasoning must use the same group",
            "confidence": "number 0..1",
            "claims": [{
                "claim_key": "stable short identifier",
                "conclusion": "string",
                "problem_type": "string or null",
                "severity": "critical|warning|info",
                "confidence": "number 0..1",
                "evidence_refs": ["E0001"],
                "falsifiers": ["string"],
                "recommended_next_evidence": ["log_kind"],
            }],
            "alternative_hypotheses": ["string"],
            "reasoning_summary": "short string",
        },
        "evidence_pack": evidence_pack,
    }


def _provider_configs() -> list[dict[str, Any]]:
    raw = os.environ.get(PROVIDER_ENV, "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _run_provider(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    name = str(config.get("name") or "provider")
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return {"analyzer": name, "provider_status": "invalid_config", "error": "command must be an argv list"}
    timeout = max(1, int(config.get("timeout_seconds", DEFAULT_PROVIDER_TIMEOUT)))
    try:
        process = subprocess.run(
            command,
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"analyzer": name, "provider_status": "provider_error", "error": str(exc)}
    if len(process.stdout.encode("utf-8")) > MAX_PROVIDER_OUTPUT_BYTES:
        return {"analyzer": name, "provider_status": "provider_error", "error": "provider output exceeded budget"}
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        return {"analyzer": name, "provider_status": "provider_error", "error": f"non-JSON output: {exc}"}
    if not isinstance(result, dict):
        return {"analyzer": name, "provider_status": "provider_error", "error": "output was not a JSON object"}
    result["analyzer"] = name
    result.setdefault("model", str(config.get("model") or name))
    result.setdefault("agent_id", str(config.get("agent_id") or name))
    result.setdefault("independence_group", str(config.get("independence_group") or name))
    result["provider_status"] = "completed" if process.returncode == 0 else "provider_error"
    if process.returncode != 0:
        result["error"] = process.stderr.strip() or f"exit {process.returncode}"
    return result


def _embedded_analyses(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (ctx.get("extra") or {}).get("model_analyses") or ctx.get("model_analyses") or {}
    output: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for analyzer, value in raw.items():
            if isinstance(value, dict):
                output.append({**value, "analyzer": str(analyzer), "provider_status": "completed"})
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict):
                output.append({**value, "provider_status": "completed"})
    return output


def collect_analyses(ctx: dict[str, Any], request: dict[str, Any]) -> list[dict[str, Any]]:
    analyses = _embedded_analyses(ctx)
    configs = _provider_configs()
    if configs:
        with ThreadPoolExecutor(max_workers=min(4, len(configs))) as pool:
            futures = [pool.submit(_run_provider, config, request) for config in configs]
            for future in as_completed(futures):
                analyses.append(future.result())
    return analyses


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _claim_key(conclusion: str, problem_type: str | None) -> str:
    words = [word for word in WORD_RE.findall(conclusion.lower()) if word not in STOP_WORDS]
    return f"{problem_type or 'unknown'}:{'-'.join(words[:12]) or 'claim'}"


def _conclusion_similarity(left: str, right: str) -> float:
    left_tokens = {word for word in WORD_RE.findall(left.lower()) if word not in STOP_WORDS}
    right_tokens = {word for word in WORD_RE.findall(right.lower()) if word not in STOP_WORDS}
    if not left_tokens and not right_tokens:
        return 1.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def normalize_analyses(
    analyses: Iterable[dict[str, Any]], evidence_pack: dict[str, Any]
) -> list[dict[str, Any]]:
    ref_aliases = {
        str(alias): str(item["id"])
        for item in evidence_pack["items"]
        for alias in (item.get("id"), item.get("legacy_id"))
        if alias
    }
    normalized: list[dict[str, Any]] = []
    seen_analyzers: set[str] = set()
    for index, analysis in enumerate(analyses, start=1):
        analyzer = str(analysis.get("analyzer") or f"analyzer_{index}")
        if analyzer in seen_analyzers:
            analyzer = f"{analyzer}_{index}"
        seen_analyzers.add(analyzer)
        status = str(analysis.get("provider_status") or "completed")
        claims: list[dict[str, Any]] = []
        rejected_claims: list[dict[str, Any]] = []
        raw_claims = analysis.get("claims") or analysis.get("findings") or []
        for raw_claim in raw_claims:
            if isinstance(raw_claim, str):
                conclusion = raw_claim
                source: dict[str, Any] = {}
            elif isinstance(raw_claim, dict):
                conclusion = str(raw_claim.get("conclusion") or raw_claim.get("summary") or "")
                source = raw_claim
            else:
                continue
            if not conclusion.strip():
                continue
            problem_type = source.get("problem_type") or evidence_pack.get("problem_type")
            refs_requested = [str(ref) for ref in source.get("evidence_refs") or []]
            refs = sorted({ref_aliases[ref] for ref in refs_requested if ref in ref_aliases})
            invalid_refs = sorted({ref for ref in refs_requested if ref not in ref_aliases})
            falsifiers = [_sanitize(item) for item in source.get("falsifiers") or []][:10]
            if not refs:
                rejected_claims.append({
                    "conclusion": _sanitize(conclusion),
                    "reason": "no_valid_evidence_refs",
                    "invalid_evidence_refs": invalid_refs,
                })
                continue
            if not falsifiers:
                rejected_claims.append({
                    "conclusion": _sanitize(conclusion),
                    "reason": "missing_falsifiers",
                    "evidence_refs": refs,
                })
                continue
            confidence = _clamp(
                source.get("confidence"), _clamp(analysis.get("confidence"))
            )
            if invalid_refs:
                confidence = min(confidence, 0.5)
            claims.append({
                "claim_key": str(source.get("claim_key") or _claim_key(conclusion, problem_type)),
                "conclusion": _sanitize(conclusion),
                "problem_type": problem_type,
                "severity": str(source.get("severity") or "warning"),
                "confidence": confidence,
                "evidence_refs": refs,
                "invalid_evidence_refs": invalid_refs,
                "falsifiers": falsifiers,
                "recommended_next_evidence": [
                    str(item) for item in source.get("recommended_next_evidence") or []
                ][:10],
            })
        normalized.append({
            "analyzer": analyzer,
            "model": str(analysis.get("model") or analyzer),
            "agent_id": str(analysis.get("agent_id") or analyzer),
            "independence_group": str(analysis.get("independence_group") or analyzer),
            "provider_status": status,
            "confidence": _clamp(analysis.get("confidence")),
            "claims": claims,
            "rejected_claims": rejected_claims,
            "alternative_hypotheses": [
                _sanitize(item) for item in analysis.get("alternative_hypotheses") or []
            ][:10],
            "reasoning_summary": _sanitize(analysis.get("reasoning_summary") or ""),
            "error": _sanitize(analysis.get("error")) if analysis.get("error") else None,
        })
    return normalized


def reconcile(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [analysis for analysis in analyses if analysis["provider_status"] == "completed"]
    independent_groups = {analysis["independence_group"] for analysis in completed}
    models = {analysis["model"] for analysis in completed}
    agents = {analysis["agent_id"] for analysis in completed}
    groups: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for analysis in completed:
        for claim in analysis["claims"]:
            groups.setdefault(claim["claim_key"], []).append((
                analysis["analyzer"], analysis["independence_group"], claim
            ))

    agreed: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    direct_conflicts: list[dict[str, Any]] = []
    for key, values in sorted(groups.items()):
        analyzers = sorted({analyzer for analyzer, _, _ in values})
        claim_independence = sorted({group for _, group, _ in values})
        if len(claim_independence) < 2:
            continue
        similarities = [
            _conclusion_similarity(values[left][2]["conclusion"], values[right][2]["conclusion"])
            for left in range(len(values))
            for right in range(left + 1, len(values))
        ]
        minimum_similarity = min(similarities) if similarities else 1.0
        if minimum_similarity < 0.55:
            direct_conflicts.append({
                "problem_type": str(values[0][2].get("problem_type") or "unknown"),
                "claim_key": key,
                "claims": [
                    {
                        "analyzer": analyzer,
                        "independence_group": independence_group,
                        **claim,
                    }
                    for analyzer, independence_group, claim in values
                ],
                "minimum_conclusion_similarity": round(minimum_similarity, 4),
                "reason": "providers reused a claim key for materially different conclusions",
            })
            continue
        refs_by_analyzer = [set(claim["evidence_refs"]) for _, _, claim in values]
        common_refs = set.intersection(*refs_by_analyzer) if refs_by_analyzer else set()
        union_refs = set.union(*refs_by_analyzer) if refs_by_analyzer else set()
        record = {
            "claim_key": key,
            "conclusion": values[0][2]["conclusion"],
            "analyzers": analyzers,
            "independence_groups": claim_independence,
            "evidence_refs": sorted(union_refs),
            "common_evidence_refs": sorted(common_refs),
            "confidence": min(claim["confidence"] for _, _, claim in values),
            "severity_variants": sorted({claim["severity"] for _, _, claim in values}),
        }
        if common_refs:
            agreed.append(record)
        else:
            record["reason"] = "claim matched but providers did not cite common evidence"
            partial.append(record)

    agreed_keys = {item["claim_key"] for item in agreed}
    by_problem: dict[str, list[dict[str, Any]]] = {}
    for analysis in completed:
        for claim in analysis["claims"]:
            if claim["claim_key"] in agreed_keys:
                continue
            by_problem.setdefault(str(claim.get("problem_type") or "unknown"), []).append({
                "analyzer": analysis["analyzer"],
                **claim,
            })
    conflicts = list(direct_conflicts)
    for problem_type, claims in sorted(by_problem.items()):
        keys = {claim["claim_key"] for claim in claims}
        analyzers = {claim["analyzer"] for claim in claims}
        if len(keys) > 1 and len(analyzers) > 1:
            conflicts.append({
                "problem_type": problem_type,
                "claims": claims,
                "reason": "independent analyzers proposed different root-cause claims",
            })

    unique_keys = set(groups)
    agreement_ratio = len(agreed) / max(1, len(unique_keys))
    completed_confidences = [analysis["confidence"] for analysis in completed]
    diversity_factor = 1.0 if len(models) >= 2 else 0.85
    consensus_confidence = (
        min(completed_confidences) * agreement_ratio * diversity_factor
        if completed_confidences else 0.0
    )
    status = (
        "not_configured" if not analyses
        else "provider_error" if not completed
        else "insufficient_independence" if len(independent_groups) < 2
        else "conflict" if conflicts
        else "completed" if agreed
        else "no_evidence_grounded_agreement"
    )
    return {
        "status": status,
        "provider_count": len(analyses),
        "completed_provider_count": len(completed),
        "independent_analysis_count": len(independent_groups),
        "diversity": {
            "models": sorted(models),
            "agents": sorted(agents),
            "independence_groups": sorted(independent_groups),
            "mode": (
                "multi_model" if len(models) >= 2
                else "multi_agent_single_model" if len(agents) >= 2
                else "single_analysis"
            ),
            "confidence_factor": diversity_factor,
        },
        "agreed_findings": [item["conclusion"] for item in agreed],
        "agreed_claims": agreed,
        "conflicting_findings": conflicts,
        "partial_agreement": partial,
        "consensus_confidence": round(consensus_confidence, 4),
        "agreement_ratio": round(agreement_ratio, 4),
    }


def analyze(ctx: dict[str, Any]) -> dict[str, Any]:
    evidence_pack = build_evidence_pack(ctx)
    request = provider_request(evidence_pack)
    normalized = normalize_analyses(collect_analyses(ctx, request), evidence_pack)
    consensus = reconcile(normalized)
    children = _children_from_context(ctx)
    source_groups = (ctx.get("extra") or {}).get("evidence_source_groups") or {}
    evidence_sources = sorted({
        str(source_groups.get(skill) or skill)
        for skill, result in children.items()
        if result.get("ok") and skill not in {"validator", "source_logging_discovery"}
    })
    minimum_sources = max(
        1, int((ctx.get("extra") or {}).get("minimum_independent_skills", 2))
    )
    if (consensus["status"] == "completed"
            and len(evidence_sources) < minimum_sources):
        consensus["status"] = "insufficient_evidence_sources"
        consensus["required_evidence_sources"] = minimum_sources
        consensus["actual_evidence_sources"] = len(evidence_sources)
    status = consensus["status"]
    agreed = consensus["agreed_claims"]
    conflicts = consensus["conflicting_findings"]

    requested_logs: dict[str, dict[str, Any]] = {}
    for analysis in normalized:
        for claim in analysis["claims"]:
            for log_kind in claim["recommended_next_evidence"]:
                requested_logs.setdefault(log_kind, {
                    "log_kind": log_kind,
                    "why": f"Independent analyzer {analysis['analyzer']} requested this evidence to test claim {claim['claim_key']}.",
                    "skill": None,
                })
    plan = build_collection_plan(
        requested_logs.values(),
        mode=str((ctx.get("extra") or {}).get("incident_mode") or "offline_bundle"),
        time_range=ctx.get("time_range") or {},
        products=[str((ctx.get("extra") or {}).get("product") or "unknown")],
    ) if requested_logs else None

    gaps = []
    if status == "not_configured":
        gaps.append(asdict(EvidenceGap(
            code="validation_provider_not_configured",
            summary=(
                "No independent validator providers or injected model analyses were configured."
            ),
            blocking=False,
            details={"provider_env": PROVIDER_ENV},
        )))
    elif status in {"insufficient_independence", "insufficient_evidence_sources"}:
        gaps.append(asdict(EvidenceGap(
            code=status,
            summary=(
                "At least two independent completed analyses are required for consensus."
                if status == "insufficient_independence" else
                f"At least {minimum_sources} independent evidence-producing skills are required; "
                f"only {len(evidence_sources)} succeeded."
            ),
            blocking=True,
        )))
    elif status == "provider_error":
        gaps.append(asdict(EvidenceGap(
            code="validation_provider_error",
            summary="All configured validation providers failed or returned invalid output.",
            blocking=True,
            details={
                "errors": [analysis.get("error") for analysis in normalized if analysis.get("error")]
            },
        )))
    elif status == "no_evidence_grounded_agreement":
        gaps.append(asdict(EvidenceGap(
            code="no_evidence_grounded_agreement",
            summary="Independent analyses completed but did not agree on a claim backed by common evidence.",
            blocking=True,
        )))
    elif conflicts:
        gaps.append(asdict(EvidenceGap(
            code="model_interpretation_conflict",
            summary="Independent analyses conflict; collect the listed falsifying evidence.",
            blocking=True,
            details={"conflict_count": len(conflicts)},
        )))

    candidate_cause = (
        agreed[0]["conclusion"]
        if agreed and not conflicts and status == "completed" else None
    )
    confidence = (
        "medium" if candidate_cause and consensus["consensus_confidence"] >= 0.35
        else "low"
    )
    quality = {
        "direct_join": False,
        "independent_sources": len(evidence_sources),
        "sample_sufficient": bool(evidence_pack.get("items")),
        "capture_complete": False,
        "time_comparable": False,
        "blocking_gaps": any(bool(gap.get("blocking")) for gap in gaps),
    }
    state = derive_evidence_state(
        root_cause=candidate_cause,
        confidence=confidence,
        independent_sources=len(evidence_sources),
        blocking_gaps=quality["blocking_gaps"],
        sample_sufficient=quality["sample_sufficient"],
        capture_complete=quality["capture_complete"],
        tool_unavailable=status == "provider_error",
    )
    if state == EvidenceState.INCONCLUSIVE_EVIDENCE and plan and plan.requests:
        state = EvidenceState.COLLECTION_PLAN_READY
    root_cause = candidate_cause if state == EvidenceState.ROOT_CAUSE_FOUND else None
    if root_cause is None:
        confidence = "low"
    cross_validation = {
        "enabled": bool(normalized),
        "status": status,
        "problem_type": ctx.get("problem_type"),
        "severity": "warning" if conflicts else "info",
        "analyses": {analysis["analyzer"]: analysis for analysis in normalized},
        "consensus": consensus,
        "evidence_chain": [{
            "step_name": "evidence_pack",
            "method": "bounded_redacted_fact_projection",
            "input_summary": {
                "child_skills": sorted(children),
                "evidence_sources": evidence_sources,
            },
            "output_summary": evidence_pack["integrity"],
            "data_range": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, {
            "step_name": "independent_analysis",
            "method": "parallel_provider_or_injected_analysis",
            "input_summary": {"provider_count": len(normalized)},
            "output_summary": {"status": status},
            "data_range": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, {
            "step_name": "deterministic_reconciliation",
            "method": "claim_key_and_common_evidence_refs",
            "input_summary": {"analyses": len(normalized)},
            "output_summary": {
                "agreed": len(agreed),
                "conflicts": len(conflicts),
                "partial": len(consensus["partial_agreement"]),
            },
            "data_range": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "new_hypotheses": sorted({
            str(hypothesis)
            for analysis in normalized
            for hypothesis in analysis["alternative_hypotheses"]
        }),
        "validation_notes": (
            "Consensus requires at least two analyzers citing common evidence ids. "
            "The validator never raises confidence above medium on model agreement alone."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if status == "not_configured":
        summary = "Cross-validation is available but no independent providers were configured."
    elif conflicts:
        summary = f"Cross-validation found {len(conflicts)} interpretation conflict(s)."
    elif agreed:
        summary = f"Cross-validation found {len(agreed)} evidence-grounded agreed claim(s)."
    else:
        summary = "Cross-validation completed without evidence-grounded agreement."
    return {
        "skill": SKILL_ID,
        "ok": status != "provider_error",
        "findings": [{
            "summary": summary,
            "severity": "warning" if conflicts or status != "completed" else "info",
            "evidence": {
                "status": status,
                "provider_count": consensus["provider_count"],
                "agreed_claims": len(agreed),
                "conflicts": len(conflicts),
            },
        }],
        "root_cause": root_cause,
        "confidence": confidence,
        "recommendations": [
            "Configure at least two independent providers or inject two independent model analyses."
            if status == "not_configured" else
            "Resolve conflicts using the requested falsifying evidence before remediation."
            if conflicts else
            "Treat model agreement as corroboration, not a substitute for direct evidence."
        ],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": list(requested_logs.values()),
        "cross_validation": cross_validation,
        "evidence_state": state,
        "evidence_gaps": gaps,
        "collection_plan": asdict(plan) if plan else None,
        "raw": {
            "quality": quality,
            "candidate_cause": candidate_cause,
            "evidence_pack": evidence_pack,
            "provider_request": request if status == "not_configured" else None,
            "provider_configuration": {
                "environment_variable": PROVIDER_ENV,
                "command_from_incident_input_allowed": False,
                "shell_execution_used": False,
            },
        },
        "error": (
            "All configured validation providers failed or returned invalid output."
            if status == "provider_error" else None
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evidence-grounded cross-validator")
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