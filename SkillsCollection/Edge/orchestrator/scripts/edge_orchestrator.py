#!/usr/bin/env python3
"""
edge_orchestrator.py — fan-out coordinator for the Edge skill collection.

Input: a payload with at minimum `skills_to_trigger` (list[str]) and
optionally `problems`, `time_range`, `extra`. Accepts either the raw
edge_diagnostics envelope or just its `raw` block.

Runs each follow-up in parallel via _shared.contract.skill_entry().
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _shared.contract import (  # noqa: E402
    Finding, SkillResult, load_context, registry, skill_entry,
    Solution, NextStep, LogRequest,
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

SKILL_ID = "orchestrator"
DEFAULT_PER_SKILL_TIMEOUT = 60
DEFAULT_TOTAL_TIMEOUT = 240
MAX_WORKERS = 3

# Backward-compatible defaults; preferred source is registry metadata.
DEFAULT_SKILL_PRODUCES_KINDS: dict[str, tuple[str, ...]] = {
    "edge_crash":       ("crashpad",),
    "edge_diagnostics": ("crashpad", "registry", "user_data", "processes"),
    "edge_extensions":  ("user_data",),
    "edge_known_issue": (),
    "edge_network":     ("registry",),
    "edge_netlog":      ("netlog",),
    "edge_procmon":     ("procmon",),
    "edge_performance": ("processes",),
    "edge_policy":      ("registry",),
    "edge_qa":          (),
}


def _skill_produces_kinds_map() -> dict[str, tuple[str, ...]]:
    src = registry().get("source_registry") or {}
    if not isinstance(src, dict) or not src:
        return DEFAULT_SKILL_PRODUCES_KINDS
    out: dict[str, tuple[str, ...]] = {}
    for skill, kinds in src.items():
        if isinstance(kinds, (list, tuple)):
            out[str(skill)] = tuple(str(k) for k in kinds if k)
    return out or DEFAULT_SKILL_PRODUCES_KINDS


# Backward-compatible defaults; preferred source is registry metadata.
DEFAULT_EXTRA_KEY_TO_KIND: dict[str, str] = {
    "netlog_paths":   "netlog",
    "netlog_folder":  "netlog",
    "crashpad_path":  "crashpad",
    "evtx_paths":     "evtx",
    "evtx_folder":    "evtx",
    "procmon_paths":  "procmon",
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


def _child_produced_kind(skill_id: str, kind: str, env: dict[str, Any]) -> bool:
    if not env.get("ok"):
        return False
    if kind != "procmon":
        return True
    quality = ((env.get("raw") or {}).get("quality") or {})
    return int(quality.get("independent_sources") or 0) > 0


def _unwrap_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept either an edge_diagnostics envelope or its inner raw block."""
    if "skills_to_trigger" in raw or "problems" in raw:
        return raw
    inner = raw.get("raw")
    if isinstance(inner, dict) and ("skills_to_trigger" in inner or "problems" in inner):
        return inner
    return raw


def _build_subcontext(payload: dict[str, Any], problem_type: str | None) -> dict[str, Any]:
    matched_problem: dict[str, Any] | None = None
    for problem in payload.get("problems") or []:
        if isinstance(problem, dict) and problem.get("type") == problem_type:
            matched_problem = problem
            break

    subctx = {
        "time_range": payload.get("time_range") or {},
        "problem_type": problem_type,
        "extra": payload.get("extra") or {},
    }
    for key in ("question", "summary", "symptom", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            subctx[key] = value.strip()
    if matched_problem:
        subctx["problem"] = matched_problem
        subctx["problems"] = [matched_problem]
        for key in ("summary", "title"):
            value = matched_problem.get(key)
            if isinstance(value, str) and value.strip() and key not in subctx:
                subctx[key] = value.strip()
    return subctx


def _problem_type_for_skill(skill_id: str, problems: list[dict[str, Any]]) -> str | None:
    reg = registry()["skills"]
    triggers = set(reg.get(skill_id, {}).get("triggers_for", []))
    for p in problems:
        if p.get("type") in triggers:
            return p["type"]
    return problems[0]["type"] if problems else None


def _build_orchestrator_subcontext(
    payload: dict[str, Any],
    skill_id: str,
    problems: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    context = _build_subcontext(
        payload, _problem_type_for_skill(skill_id, problems)
    )
    if skill_id == "edge_procmon":
        context["extra"] = dict(context.get("extra") or {})
        context["extra"].setdefault("deadline_seconds", max(1, timeout - 10))
    return context


def _run_one(skill_id: str, ctx: dict[str, Any], timeout: int) -> dict[str, Any]:
    try:
        entry = skill_entry(skill_id, "python")
    except KeyError as e:
        return operational_failure(
            skill_id,
            str(e),
            code="skill_resolution_failed",
            execution={"runtime": "python", "stage": "resolve"},
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
    try:
        proc = subprocess.run(
            [sys.executable, str(entry), context_argument],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return operational_failure(
            skill_id,
            f"timed out after {timeout}s",
            code="timeout",
            execution={
                "runtime": "python",
                "stage": "execute",
                "timeout_seconds": timeout,
            },
        )
    except OSError as e:
        return operational_failure(
            skill_id,
            f"failed to start child process: {e}",
            code="process_launch_failed",
            execution={"runtime": "python", "stage": "launch"},
        )
    finally:
        if temporary_context is not None:
            temporary_context.unlink(missing_ok=True)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        result = contract_failure(
            skill_id,
            [ContractIssue("$", f"invalid JSON: {e}")],
            source_excerpt=proc.stdout,
        )
    else:
        try:
            result = normalize_result(result, expected_skill=skill_id)
        except ResultContractError as e:
            result = contract_failure(skill_id, e.issues, source_excerpt=proc.stdout)
    raw = result["raw"]
    raw["execution"] = {
        "runtime": "python",
        "exit_code": proc.returncode,
        **({
            "stderr_character_count": len(proc.stderr),
            "stderr_sha256": hashlib.sha256(
                proc.stderr.encode("utf-8", errors="replace")
            ).hexdigest(),
        } if proc.stderr else {}),
    }
    if proc.returncode != 0 and result.get("ok"):
        return operational_failure(
            skill_id,
            f"child exited with code {proc.returncode} despite ok=true",
            code="child_exit_mismatch",
            execution={
                "runtime": "python",
                "stage": "execute",
                "exit_code": proc.returncode,
            },
        )
    return result


def _has_source_roots(payload: dict[str, Any]) -> bool:
    extra = payload.get("extra") or {}
    return any(extra.get(key) for key in ("repo_roots", "config_roots", "symbols_roots"))


def _successful_root(children: dict[str, dict[str, Any]]) -> bool:
    return any(
        result.get("ok")
        and result.get("root_cause")
        and result.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND
        for result in children.values()
        if isinstance(result, dict)
    )


def _problem_types(payload: dict[str, Any]) -> set[str]:
    return {
        str(problem.get("type"))
        for problem in payload.get("problems") or []
        if isinstance(problem, dict) and problem.get("type")
    }


def _should_validate(payload: dict[str, Any], validator_requested: bool) -> bool:
    config = registry().get("cv_config") or {}
    if not config.get("enabled"):
        return False
    eligible = set(config.get("eligible_problem_types") or [])
    return validator_requested or bool(_problem_types(payload) & eligible)


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _aggregate_advisories(sub_results: dict[str, dict[str, Any]]
                          ) -> tuple[list[Solution], list[NextStep], list[LogRequest]]:
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


def _cross_source_context(payload: dict[str, Any],
                          children: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Summarise which Edge data sources were exercised and which problems
    surfaced across more than one skill (basic correlation hint)."""
    sources_used: set[str] = set()
    produces_map = _skill_produces_kinds_map()
    for sid, env in children.items():
        if not isinstance(env, dict):
            continue
        for kind in produces_map.get(sid, ()):
            if _child_produced_kind(sid, kind, env):
                sources_used.add(kind)

    pt_to_skills: dict[str, list[str]] = {}
    for sid, env in children.items():
        if not isinstance(env, dict) or not env.get("ok"):
            continue
        raw = env.get("raw") or {}
        for p in raw.get("problems") or []:
            t = p.get("type") if isinstance(p, dict) else None
            if t:
                pt_to_skills.setdefault(t, []).append(sid)
    shared = {t: sorted(set(sks)) for t, sks in pt_to_skills.items() if len(set(sks)) > 1}

    return {
        "sources_used": sorted(sources_used),
        "shared_problem_types": shared,
        "skills_run": sorted(children.keys()),
        "had_input_problems": bool(payload.get("problems")),
    }


# ---------------------------------------------------------------------------
# Missing-log gate
# ---------------------------------------------------------------------------

# Subset of `extra.*` keys that indicate a log/data source the caller already
# supplied. Used to mark a log_kind as "provided" without running its
# consumer skill.


def _expected_log_kinds(payload: dict[str, Any],
                        children: dict[str, dict[str, Any]]) -> dict[str, LogRequest]:
    """Build a {log_kind -> LogRequest} map of every log kind the playbook
    (via input problems) or any child skill said it needed.

    Returning the LogRequest preserves actionable metadata (why /
    how_to_collect) when we have to flag the kind as missing.
    """
    out: dict[str, LogRequest] = {}

    def _add(req: LogRequest) -> None:
        if req.log_kind and req.log_kind not in out:
            out[req.log_kind] = req

    for p in payload.get("problems") or []:
        t = p.get("type") if isinstance(p, dict) else None
        if t:
            for req in playbook.logs_for(t):
                _add(req)

    for env in children.values():
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


def _provided_log_kinds(payload: dict[str, Any],
                        children: dict[str, dict[str, Any]]) -> set[str]:
    """Log kinds the caller already supplied (via extra) OR that a child skill
    successfully exercised on this host."""
    provided: set[str] = set()
    produces_map = _skill_produces_kinds_map()
    extra_map = _extra_key_to_kind_map()
    extra = payload.get("extra") or {}
    for key, kind in extra_map.items():
        if extra.get(key) and kind != "procmon":
            provided.add(kind)
    for sid, env in children.items():
        if not isinstance(env, dict):
            continue
        for kind in produces_map.get(sid, ()):
            if _child_produced_kind(sid, kind, env):
                provided.add(kind)
    return provided


def _compute_missing_logs(payload: dict[str, Any],
                           children: dict[str, dict[str, Any]]
                           ) -> list[dict[str, Any]]:
    """Return a list of `additional_logs_needed`-shaped dicts for every log
    kind that was expected but not provided. Entries are sorted by log_kind
    for stable output."""
    expected = _expected_log_kinds(payload, children)
    provided = _provided_log_kinds(payload, children)
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


def _operator_summary_md(*, payload: dict[str, Any],
                          assignments: dict[str, str | None],
                          children: dict[str, dict[str, Any]],
                          findings: list[Finding],
                          root_cause: str | None,
                          confidence: str,
                          solutions: list[Solution],
                          next_steps: list[NextStep],
                          additional_logs_needed: list[LogRequest],
                          cross_source_context: dict[str, Any],
                          missing_logs: list[dict[str, Any]] | None = None,
                          evidence_state: str | None = None,
                          evidence_gaps: list[EvidenceGap] | None = None,
                          collection_plan: object | None = None,
                          validation_status: str | None = None) -> str:
    lines: list[str] = []
    lines.append("# Edge orchestrator summary")
    lines.append("")
    lines.append(f"- Skills run: {', '.join(sorted(children.keys())) or '_none_'}")
    lines.append(f"- Confidence: **{confidence}**")
    if evidence_state:
        lines.append(f"- Evidence state: **{evidence_state}**")
    if validation_status:
        lines.append(f"- Cross-validation: **{validation_status}**")
    if root_cause:
        lines.append(f"- Root cause: {root_cause}")
    avail = cross_source_context.get("sources_used") or []
    lines.append(f"- Data sources exercised: {', '.join(f'`{s}`' for s in avail) or '_none_'}")
    if missing_logs:
        kinds = ", ".join(f"`{m['log_kind']}`" for m in missing_logs)
        lines.append(f"- Missing log kinds: {kinds}")
    shared = cross_source_context.get("shared_problem_types") or {}
    if shared:
        lines.append("- Problem types confirmed by multiple skills:")
        for pt, sks in sorted(shared.items()):
            lines.append(f"  - `{pt}` (from {', '.join(sks)})")

    if findings:
        lines.append("")
        lines.append("## Top findings")
        for f in findings[:10]:
            sev = f.severity if isinstance(f, Finding) else (f.get("severity") if isinstance(f, dict) else "info")
            summ = f.summary if isinstance(f, Finding) else (f.get("summary") if isinstance(f, dict) else str(f))
            lines.append(f"- **{sev}** — {summ}")

    if missing_logs:
        lines.append("")
        lines.append("## Missing required logs")
        lines.append("These log kinds were needed for the requested investigation but were not provided.")
        for m in missing_logs:
            tail = f" (`{m['skill']}`)" if m.get("skill") else ""
            lines.append(f"- `{m['log_kind']}` — {m.get('why') or 'required for full analysis'}{tail}")
            if m.get("how_to_collect"):
                lines.append(f"  - _how:_ {m['how_to_collect']}")

    if evidence_gaps:
        lines.append("")
        lines.append("## Evidence gaps")
        for gap in evidence_gaps:
            blocking = "blocking" if gap.blocking else "non-blocking"
            lines.append(f"- `{gap.code}` ({blocking}) — {gap.summary}")

    plan_requests = getattr(collection_plan, "requests", []) if collection_plan else []
    if plan_requests:
        lines.append("")
        lines.append("## Collection plan (operator review required)")
        for request in plan_requests:
            admin = "admin" if request.requires_admin else "standard user"
            lines.append(
                f"- `{request.log_kind}` — {request.reason} "
                f"(_{request.mode}; {admin}_ )"
            )
            for command in request.commands[:1]:
                lines.append(f"  - `{command.command}`")

    if solutions:
        lines.append("")
        lines.append("## Solutions")
        for s in solutions:
            ref = f" _(for `{s.problem_ref}`)_" if s.problem_ref else ""
            lines.append(f"- **{s.severity}** — {s.title}{ref}")
            for step in s.steps:
                lines.append(f"  - {step}")

    if next_steps:
        lines.append("")
        lines.append("## Next steps")
        for n in next_steps:
            tail = f" (`{n.skill}`)" if n.skill else ""
            lines.append(f"- {n.action}{tail}")
            if n.why:
                lines.append(f"  - _why:_ {n.why}")

    known_issue_matches: list[str] = []
    known_issue_gaps: list[str] = []
    for sid, env in children.items():
        if not isinstance(env, dict) or not env.get("ok"):
            continue
        raw = env.get("raw") or {}
        ranked = (raw.get("match_ranking") or {}).get("ranked_best_match")
        if isinstance(ranked, dict):
            title = ranked.get("title") or ranked.get("id") or "candidate"
            strength = ranked.get("match_strength") or "unknown"
            known_issue_matches.append(f"{sid}: {title} ({strength})")
        for gap in raw.get("not_found_gaps") or []:
            if isinstance(gap, str):
                known_issue_gaps.append(f"{sid}: {gap}")

    if known_issue_matches:
        lines.append("")
        lines.append("## Ranked known-issue matches")
        for item in known_issue_matches:
            lines.append(f"- {item}")

    if known_issue_gaps:
        lines.append("")
        lines.append("## Known-issue gaps")
        for item in known_issue_gaps:
            lines.append(f"- {item}")

    if additional_logs_needed:
        lines.append("")
        lines.append("## Additional logs needed")
        for l in additional_logs_needed:
            tail = f" (`{l.skill}`)" if l.skill else ""
            lines.append(f"- `{l.log_kind}` — {l.why}{tail}")
            if l.how_to_collect:
                lines.append(f"  - _how:_ {l.how_to_collect}")

    return "\n".join(lines)


def orchestrate(payload: dict[str, Any],
                per_skill_timeout: int = DEFAULT_PER_SKILL_TIMEOUT,
                total_timeout: int = DEFAULT_TOTAL_TIMEOUT) -> SkillResult:
    payload = _unwrap_payload(payload)
    requested_follow_ups: list[str] = list(payload.get("skills_to_trigger") or [])
    extra = payload.get("extra") or {}
    if (extra.get("procmon_paths") or extra.get("procmon_folder")) and "edge_procmon" not in requested_follow_ups:
        requested_follow_ups.append("edge_procmon")
    validator_requested = "validator" in requested_follow_ups
    follow_ups = [skill for skill in requested_follow_ups if skill != "validator"]
    problems: list[dict[str, Any]] = payload.get("problems") or []

    if not follow_ups and not _has_source_roots(payload) and not validator_requested:
        return SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(summary="Nothing to orchestrate.", severity="info")],
            recommendations=["No follow-up skills were requested."],
            confidence="low",
            evidence_state=EvidenceState.INCONCLUSIVE_EVIDENCE,
        )

    assignments: dict[str, str | None] = {}
    children: dict[str, dict[str, Any]] = {}

    ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {}
        for sid in follow_ups:
            ptype = _problem_type_for_skill(sid, problems)
            assignments[sid] = ptype
            ctx = _build_orchestrator_subcontext(
                payload, sid, problems, per_skill_timeout
            )
            futures[ex.submit(_run_one, sid, ctx, per_skill_timeout)] = sid
        try:
            for fut in as_completed(futures, timeout=total_timeout):
                sid = futures[fut]
                try:
                    children[sid] = fut.result()
                except Exception as exc:
                    children[sid] = operational_failure(
                        sid,
                        f"child execution raised {type(exc).__name__}",
                        code="child_execution_failed",
                        execution={"stage": "execute"},
                    )
        except TimeoutError:
            for fut, sid in futures.items():
                if sid not in children:
                    fut.cancel()
                    children[sid] = operational_failure(
                        sid,
                        f"total orchestration timeout after {total_timeout}s",
                        code="orchestration_timeout",
                        execution={
                            "stage": "orchestrate",
                            "timeout_seconds": total_timeout,
                        },
                    )
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if _has_source_roots(payload) and not _successful_root(children):
        source_ctx = {
            "problem_type": next(iter(_problem_types(payload)), None),
            "problems": problems,
            "time_range": payload.get("time_range") or {},
            "extra": {**(payload.get("extra") or {}), "product": "Edge"},
        }
        children["source_logging_discovery"] = _run_one(
            "source_logging_discovery", source_ctx, per_skill_timeout
        )

    validation_result: dict[str, Any] | None = None
    if _should_validate(payload, validator_requested) and children:
        extra = payload.get("extra") or {}
        validation_ctx = {
            "problem_type": next(iter(_problem_types(payload)), None),
            "problems": problems,
            "time_range": payload.get("time_range") or {},
            "extra": {
                "product": "Edge",
                "children": children,
                "model_analyses": extra.get("model_analyses") or {},
                "incident_mode": extra.get("incident_mode") or "live_host",
                "minimum_independent_skills": (
                    (registry().get("cv_config") or {}).get("minimum_independent_skills", 2)
                ),
            },
        }
        validation_result = _run_one("validator", validation_ctx, per_skill_timeout)

    # Fuse
    all_findings: list[Finding] = []
    root_causes: list[str] = []
    best_conf = "low"
    for sid, env in children.items():
        if not env.get("ok"):
            all_findings.append(Finding(
                summary=f"{sid} failed: {env.get('error')}",
                severity="warning",
                evidence={"skill": sid},
            ))
            continue
        for f in env.get("findings") or []:
            all_findings.append(Finding(
                summary=f"[{sid}] {f.get('summary')}",
                severity=f.get("severity", "info"),
                evidence={**(f.get("evidence") or {}), "_from": sid},
            ))
        rc = env.get("root_cause")
        conf = env.get("confidence", "low")
        if rc and env.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND:
            root_causes.append(f"{sid}: {rc}")
        if CONFIDENCE_RANK.get(conf, 0) > CONFIDENCE_RANK.get(best_conf, 0):
            best_conf = conf

    if validation_result:
        for finding in validation_result.get("findings") or []:
            all_findings.append(Finding(
                summary=f"[validator] {finding.get('summary')}",
                severity=finding.get("severity", "info"),
                evidence={**(finding.get("evidence") or {}), "_from": "validator"},
            ))
        validation_root = validation_result.get("root_cause")
        if (validation_root and validation_result.get("evidence_state")
            == EvidenceState.ROOT_CAUSE_FOUND):
            root_causes.insert(0, f"validator: {validation_root}")
            best_conf = validation_result.get("confidence", "low")
        status = ((validation_result.get("cross_validation") or {}).get("status"))
        if status == "conflict":
            best_conf = "low"

    recommendations: list[str] = []
    for sid, env in children.items():
        for rec in env.get("recommendations") or []:
            recommendations.append(f"[{sid}] {rec}")
    if validation_result:
        for rec in validation_result.get("recommendations") or []:
            recommendations.append(f"[validator] {rec}")

    advisory_results = dict(children)
    if validation_result:
        advisory_results["validator"] = validation_result
    agg_sols, agg_nxts, agg_logs = _aggregate_advisories(advisory_results)
    cross_ctx = _cross_source_context(payload, children)
    missing_logs = _compute_missing_logs(payload, advisory_results)

    # Surface missing logs as warning findings so they show up in the
    # interactive interface without requiring the operator to inspect raw.
    for m in missing_logs:
        all_findings.append(Finding(
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

    root_cause = " | ".join(root_causes) if root_causes else None
    if root_cause is None:
        best_conf = "low"
    if not root_cause and not _has_source_roots(payload):
        source_action = "Run source-guided logging discovery with explicit repo/config roots"
        if not any(step.action == source_action for step in agg_nxts):
            agg_nxts.append(NextStep(
                action=source_action,
                why=("Existing evidence did not establish a root cause; inspect local "
                     "logging declarations without executing target code."),
                skill="source_logging_discovery",
            ))
    evidence_gaps = gaps_from_missing(missing_logs)
    for sid, child in children.items():
        if not child.get("ok"):
            evidence_gaps.append(EvidenceGap(
                code="skill_failure",
                summary=f"{sid} failed: {child.get('error') or 'unknown error'}",
                skill=sid,
                blocking=True,
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
        for gap in validation_result.get("evidence_gaps") or []:
            if isinstance(gap, dict):
                evidence_gaps.append(EvidenceGap(
                    code=str(gap.get("code") or "validation_gap"),
                    summary=str(gap.get("summary") or "Validation evidence gap"),
                    severity=str(gap.get("severity") or "warning"),
                    blocking=bool(gap.get("blocking", True)),
                    log_kind=gap.get("log_kind"),
                    skill=gap.get("skill"),
                    details=dict(gap.get("details") or {}),
                ))
    if not root_cause and not _has_source_roots(payload):
        evidence_gaps.append(EvidenceGap(
            code="source_guidance_not_run",
            summary="No explicit repo/config roots were supplied for source-guided logging discovery.",
            blocking=False,
            skill="source_logging_discovery",
        ))
    collection_inputs: list[object] = [*agg_logs, *missing_logs]
    collection_plan = build_collection_plan(
        collection_inputs,
        mode=str((payload.get("extra") or {}).get("incident_mode") or "live_host"),
        time_range=payload.get("time_range") or {},
        products=["Edge"],
    ) if collection_inputs else None
    validation_status = (
        (validation_result.get("cross_validation") or {}).get("status")
        if validation_result else None
    )
    quality_inputs = list(children.values())
    if validation_result:
        quality_inputs.append(validation_result)
    root_quality = aggregate_root_quality(quality_inputs)
    root_quality["blocking_gaps"] = (
        root_quality["blocking_gaps"]
        or any(gap.blocking for gap in evidence_gaps)
    )
    evidence_state = derive_evidence_state(
        root_cause=root_cause,
        confidence=best_conf,
        direct_join=root_quality["direct_join"],
        independent_sources=root_quality["independent_sources"],
        blocking_gaps=root_quality["blocking_gaps"],
        sample_sufficient=root_quality["sample_sufficient"],
        capture_complete=root_quality["capture_complete"],
        missing_required=bool(missing_logs),
        time_not_comparable=bool(root_cause) and not root_quality["time_comparable"],
        permission_blocked=any("access" in str(gap.summary).lower() for gap in evidence_gaps),
        tool_unavailable=any("not found" in str(gap.summary).lower() for gap in evidence_gaps),
    )
    if validation_status == "conflict":
        evidence_state = EvidenceState.INCONCLUSIVE_EVIDENCE
    elif not root_cause and collection_plan and collection_plan.requests:
        evidence_state = EvidenceState.COLLECTION_PLAN_READY
    summary_md = _operator_summary_md(
        payload=payload, assignments=assignments, children=children,
        findings=all_findings, root_cause=root_cause, confidence=best_conf,
        solutions=agg_sols, next_steps=agg_nxts,
        additional_logs_needed=agg_logs, cross_source_context=cross_ctx,
        missing_logs=missing_logs,
        evidence_state=evidence_state,
        evidence_gaps=evidence_gaps,
        collection_plan=collection_plan,
        validation_status=validation_status,
    )

    return SkillResult(
        skill=SKILL_ID,
        ok=(
            all(child.get("ok") for child in children.values())
            and (validation_result is None or bool(validation_result.get("ok")))
        ),
        findings=all_findings,
        root_cause=root_cause,
        confidence=best_conf,
        recommendations=recommendations or ["No actionable recommendations from follow-up skills."],
        solutions=agg_sols,
        next_steps=agg_nxts,
        additional_logs_needed=agg_logs,
        cross_validation=(validation_result or {}).get("cross_validation"),
        evidence_state=evidence_state,
        evidence_gaps=evidence_gaps,
        collection_plan=collection_plan,
        raw={
            "problem_assignments": assignments,
            "children": children,
            "skills_run": list(children.keys()),
            "validation": validation_result,
            "quality": root_quality,
            "cross_source_context": cross_ctx,
            "missing_logs": missing_logs,
            "ranked_known_issue_matches": [
                {
                    "skill": sid,
                    "match": ((env.get("raw") or {}).get("match_ranking") or {}).get("ranked_best_match"),
                }
                for sid, env in children.items()
                if isinstance(env, dict) and isinstance(((env.get("raw") or {}).get("match_ranking") or {}).get("ranked_best_match"), dict)
            ],
            "operator_summary": summary_md,
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Edge skill orchestrator")
    ap.add_argument("context", nargs="?", default=None)
    ap.add_argument("--per-skill-timeout", type=int, default=DEFAULT_PER_SKILL_TIMEOUT)
    ap.add_argument("--total-timeout", type=int, default=DEFAULT_TOTAL_TIMEOUT)
    ap.add_argument("--procmon-exe", default=None, metavar="PATH",
                    help=("Trusted Procmon executable used only to export supplied PML files. "
                          "The path is inherited by the child and is never accepted from incident JSON."))
    ap.add_argument("--procmon-time-basis", choices=("unknown", "context"), default=None,
                    help=("Use 'context' only when Procmon CSV timestamps use the same clock "
                          "basis as context.time_range; default is unknown."))
    args = ap.parse_args(argv)

    if args.procmon_exe:
        os.environ["SKILLS_PROCMON_EXE"] = str(Path(args.procmon_exe).resolve())
    if args.procmon_time_basis:
        os.environ["SKILLS_PROCMON_TIME_BASIS"] = args.procmon_time_basis

    if args.context is None and not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            print(f"orchestrator: failed to parse stdin: {e}", file=sys.stderr)
            return 2
    else:
        payload = load_context([args.context] if args.context else [])

    result = orchestrate(payload, args.per_skill_timeout, args.total_timeout)
    result.emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
