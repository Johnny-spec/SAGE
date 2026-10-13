"""IIS and Edge incident-diagnostics reference Solution integration."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from sage.domain import AgentDefinition, RuntimeContext, SubagentDefinition
from sage.errors import ExecutionError
from sage.models import SkillDefinition
from sage.route_catalog import RouteChoice

_SAFE_CHILDREN = {
    "IIS": {"iis_logs", "httperror", "ftp_logs", "netlog", "procmon"},
    "Edge": {
        "edge_diagnostics",
        "edge_crash",
        "edge_known_issue",
        "edge_netlog",
        "edge_procmon",
        "edge_qa",
    },
}


def default_diagnostic_agent() -> AgentDefinition:
    return AgentDefinition(
        agent_id="diagnostic-agent",
        name="sage-coordinator",
        version="1.1.0",
        instructions=(
            "You are the SAGE coordinator. Before invoking any skill_* tool, use read_file "
            "to load that Skill's SKILL.md. Read referenced files only when the loaded "
            "instructions require them. Match the uploaded evidence type to the Skill: use "
            "netlog for Chromium net-export JSON and iis-logs only for IIS W3C access logs. "
            "Raw evidence Skills netlog and iis-logs are reserved for incident-analyst: "
            "delegate them and do not repeat their execution in the coordinator. After the "
            "subagent returns structured evidence, load and call report-summary once, then "
            "return a concise final result. Never reveal private reasoning or host paths."
        ),
        capability_ids=(
            "skill:echo",
            "skill:iis-logs",
            "skill:netlog",
            "skill:report-summary",
            "skill:skills-collection",
        ),
        subagents=(
            SubagentDefinition(
                name="incident-analyst",
                description=(
                    "Analyzes uploaded IIS or browser-network evidence using read-only Skills "
                    "and returns a structured result."
                ),
                instructions=(
                    "You are the incident analyst subagent. Inspect the request and Skill "
                    "descriptions, then progressively load the matching Skill. Use netlog for "
                    "Chromium net-export JSON and iis-logs only for IIS W3C access logs. Return "
                    "only the selected Skill's structured JSON result."
                ),
                capability_ids=("skill:iis-logs", "skill:netlog"),
                exclusive_capability_ids=("skill:iis-logs", "skill:netlog"),
            ),
        ),
        memory_policy="thread",
        max_steps=40,
    )


def report_summary(
    arguments: Mapping[str, Any],
    context: RuntimeContext,
) -> dict[str, Any]:
    del context
    analysis = arguments["analysis"]
    if not isinstance(analysis, Mapping):
        raise ExecutionError("report-summary analysis must be an object")
    findings = analysis.get("findings", [])
    recommendations = analysis.get("recommendations", [])
    raw = analysis.get("raw", {})
    metrics = raw.get("metrics", {}) if isinstance(raw, Mapping) else {}
    if analysis.get("skill") == "netlog" and isinstance(raw, Mapping):
        per_file = raw.get("per_file", [])
        request_count = (
            sum(
                int(item.get("requests_total", 0))
                for item in per_file
                if isinstance(item, Mapping)
            )
            if isinstance(per_file, list)
            else 0
        )
        evidence_label = "browser network"
    else:
        request_count = metrics.get("total_requests", 0) if isinstance(metrics, Mapping) else 0
        evidence_label = "IIS"
    actionable_findings = (
        [
            finding
            for finding in findings
            if not isinstance(finding, Mapping) or finding.get("severity") != "info"
        ]
        if isinstance(findings, list)
        else []
    )
    safe_recommendations = (
        [str(recommendation) for recommendation in recommendations]
        if isinstance(recommendations, list)
        else []
    )
    finding_count = len(actionable_findings)
    title = str(arguments.get("title") or "Skill execution summary")
    return {
        "title": title,
        "summary": (
            f"Analyzed {request_count} {evidence_label} requests and identified "
            f"{finding_count} actionable finding(s)."
        ),
        "request_count": request_count,
        "finding_count": finding_count,
        "status": "attention" if finding_count else "healthy",
        "recommendations": safe_recommendations,
    }


def require_nonempty_iis_metrics(
    definition: SkillDefinition,
    result: Mapping[str, Any],
) -> None:
    raw = result.get("raw", {})
    metrics = raw.get("metrics", {}) if isinstance(raw, Mapping) else {}
    if not isinstance(metrics, Mapping) or metrics.get("total_requests", 0) == 0:
        raise ExecutionError(
            f"Skill returned no parseable IIS W3C request records: {definition.name}"
        )


def build_incident_route_choices(skills_root: Path) -> list[RouteChoice]:
    collection_root = skills_root / "SkillsCollection"
    if not collection_root.is_dir():
        return []
    routes = [
        RouteChoice(
            id=f"skills-collection/{product.casefold()}",
            label=f"SkillsCollection - {product}",
            path=["SkillsCollection", product],
            target_skill="skills-collection",
            arguments={"product": product},
            description=f"Coordinate all safe imported {product} diagnostics.",
        )
        for product in ("IIS", "Edge")
    ]
    for product in ("IIS", "Edge", "Common"):
        routes.extend(_registry_routes(collection_root, product))
    return routes


def _registry_routes(collection_root: Path, product: str) -> list[RouteChoice]:
    registry_path = (
        collection_root / "Common" / "log_skills" / "_shared" / "registry.json"
        if product == "Common"
        else collection_root / product / "_shared" / "registry.json"
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    product_root = registry_path.parents[1]
    choices: list[RouteChoice] = []
    for skill_id, metadata in sorted((registry.get("skills") or {}).items()):
        if not isinstance(metadata, dict):
            continue
        folder = str(metadata.get("folder") or skill_id)
        package_root = product_root / folder
        python_entry = (metadata.get("entry") or {}).get("python")
        safe = product in _SAFE_CHILDREN and skill_id in _SAFE_CHILDREN[product]
        entry_exists = bool(python_entry and (product_root / str(python_entry)).is_file())
        selectable = safe and entry_exists
        if product == "Common":
            availability = "internal"
            reason = "Shared delegate selected by IIS or Edge orchestrators."
        elif not selectable:
            availability = "restricted"
            reason = "Requires a restricted runtime, admin access, or unsupported evidence."
        else:
            availability = "available"
            reason = f"Run the imported {product} {skill_id} delegate through the manager."
        arguments: dict[str, Any] = {"product": product}
        role = str(metadata.get("role") or "")
        if selectable and "entry_point" not in role and "product_delegate" not in role:
            arguments["child_skill"] = skill_id
        path = ["SkillsCollection", product, skill_id]
        choices.append(
            RouteChoice(
                id="/".join(("skills-collection", product.casefold(), skill_id)),
                label=" - ".join(path),
                path=path,
                target_skill="skills-collection",
                arguments=arguments,
                selectable=selectable,
                availability=availability,
                description=_description(package_root, reason),
            )
        )
    return choices


def _description(package_root: Path, fallback: str) -> str:
    for filename in ("SKILL.md", "SKILL.MD"):
        path = package_root / filename
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            break
        try:
            closing = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
            frontmatter = yaml.safe_load("\n".join(lines[1:closing]))
        except (StopIteration, yaml.YAMLError):
            break
        if isinstance(frontmatter, dict) and isinstance(frontmatter.get("description"), str):
            return frontmatter["description"].strip()
    return fallback