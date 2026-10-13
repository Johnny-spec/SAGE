"""Deterministic Solution bundle planning against an immutable active snapshot."""

import hashlib
import io
import json
import zipfile
from typing import Any

import yaml

from sage.control_models import ControlPlaneState
from sage.digests import canonical_digest, directory_digest
from sage.registry import SkillRegistry
from sage.solution_packages import SolutionBundleManifest
from sage.workflow_contracts import check_workflow_fields
from sage.workflow_drafts import DraftConflict
from sage.workflows import WorkflowTemplate


def plan_solution_bundle(
    state: ControlPlaneState,
    registry: SkillRegistry,
    *,
    draft_id: str,
    workspace_id: str,
    revision: int,
    version: str,
) -> dict[str, Any]:
    draft = state.workflow_drafts.get(draft_id)
    if draft is None or draft.workspace_id != workspace_id:
        raise KeyError(draft_id)
    if draft.revision != revision:
        raise DraftConflict("Draft changed; review dependencies again")
    solution = state.solutions[draft.solution_id]
    generation = state.generations[state.active_generation_id]
    template = WorkflowTemplate.model_validate(draft.definition)
    skills = {skill.name: skill for skill in registry}
    locks: dict[tuple[str, str], dict[str, Any]] = {}
    reasons: dict[tuple[str, str], set[str]] = {}
    visiting: set[tuple[str, str]] = set()
    risks: dict[str, str] = {}

    def capability(identifier: str, reason: str) -> None:
        if identifier.startswith("skill:"):
            resolve("skill", identifier.removeprefix("skill:"), reason)
        elif identifier.startswith("tool:mcp:"):
            resolve("mcp-tool", identifier, reason)
        else:
            raise ValueError(f"Unsupported capability: {identifier}")

    def resolve(kind: str, identifier: str, reason: str) -> None:
        key = (kind, identifier)
        reasons.setdefault(key, set()).add(reason)
        if key in visiting:
            raise ValueError(f"Dependency cycle: {kind}:{identifier}")
        if key in locks:
            return
        if len(locks) + len(visiting) >= 499:
            raise ValueError("Solution dependency limit exceeded")
        visiting.add(key)
        if kind == "agent":
            agent = generation.agent_definitions.get(identifier)
            if agent is None:
                raise ValueError(f"Agent is not active: {identifier}")
            component_version = agent["version"]
            digest = canonical_digest(agent)
            for selected in agent.get("capability_ids", []):
                capability(selected, f"agent:{identifier}")
            for child in agent.get("subagents", []):
                for selected in child.get("capability_ids", []):
                    capability(selected, f"agent:{identifier}/subagent:{child['name']}")
            for delegation in agent.get("delegations", []):
                resolve("agent", delegation["target_agent_id"], f"agent:{identifier}/delegation")
        elif kind == "skill":
            skill = skills.get(identifier)
            if skill is None or not skill.manifest.spec.policy.execution_enabled:
                raise ValueError(f"Skill is unavailable: {identifier}")
            component_version = skill.version
            digest = directory_digest(skill.package_root)
            if (
                generation.skill_versions.get(identifier) != component_version
                or generation.skill_digests.get(identifier) != digest
            ):
                raise DraftConflict(f"Published Skill content drifted: {identifier}")
            risks[f"skill:{identifier}"] = skill.manifest.spec.policy.risk
            for selected in skill.manifest.spec.execution.capability_ids:
                capability(selected, f"skill:{identifier}")
        elif kind == "mcp-tool":
            tool = generation.tool_definitions.get(identifier)
            if tool is None or not tool.enabled:
                raise ValueError(f"MCP Tool is unavailable: {identifier}")
            component_version = tool.version
            digest = tool.content_digest
            risks[identifier] = tool.risk
        else:
            raise ValueError(f"Unsupported dependency kind: {kind}")
        locks[key] = {
            "kind": kind,
            "id": identifier,
            "version": component_version,
            "contentDigest": digest,
        }
        visiting.remove(key)

    for step in template.steps:
        if (
            step.kind == "skill"
            and step.target in skills
            and skills[step.target].manifest.spec.execution.type in {"model", "workflow"}
        ):
            raise ValueError(f"Direct orchestrated Skill node is unsupported: {step.target}")
        if step.concurrency_key or step.failure_policy == "escalate":
            raise ValueError(f"Unsupported scheduling option in node {step.step_id}")
        resolve(
            "mcp-tool" if step.kind == "tool" else step.kind,
            step.target,
            f"workflow:{template.workflow_id}/{step.step_id}",
        )
    if solution.entry_agent_id:
        resolve("agent", solution.entry_agent_id, "solution:entry-agent")
    schemas = {}
    for step in template.steps:
        if step.kind == "skill":
            spec = skills[step.target].manifest.spec
            schemas[(step.kind, step.target)] = (spec.input_schema, spec.output_schema)
        elif step.kind == "tool":
            tool = generation.tool_definitions[step.target]
            schemas[(step.kind, step.target)] = (tool.input_schema, tool.output_schema or {})
        else:
            schemas[(step.kind, step.target)] = (
                {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string"}},
                },
                {"type": "object", "properties": {"message": {"type": "string"}}},
            )
    warnings = check_workflow_fields(template, schemas)
    definition = template.model_dump(mode="json", by_alias=True, exclude_none=True)
    workflow_bytes = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True).encode("utf-8")
    workflow_lock = {
        "kind": "workflow",
        "id": template.workflow_id,
        "version": template.version,
        "contentDigest": "sha256:" + hashlib.sha256(workflow_bytes).hexdigest(),
        "path": "workflow.yaml",
    }
    manifest = SolutionBundleManifest.model_validate(
        {
            "apiVersion": "sage.dev/v1alpha1",
            "kind": "SolutionBundle",
            "metadata": {"name": solution.solution_id, "title": solution.title, "version": version},
            "spec": {
                "owner": solution.owner,
                "description": solution.description,
                "entryAgentId": solution.entry_agent_id,
                "allowedRoles": solution.allowed_roles,
                "supportedProfiles": sorted(set(solution.supported_profiles) | {"durable"}),
                "components": [locks[key] for key in sorted(locks)] + [workflow_lock],
            },
        }
    )
    manifest_value = manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
    plan = {
        "draft_id": draft_id,
        "draft_revision": revision,
        "workspace_id": workspace_id,
        "generation_id": generation.generation_id,
        "manifest": manifest_value,
        "definition": definition,
        "design": draft.design,
        "dependencies": [
            {**locks[key], "required_by": sorted(reasons[key])} for key in sorted(locks)
        ],
        "risks": risks,
        "warnings": warnings,
    }
    return {**plan, "build_digest": canonical_digest(plan)}


def solution_bundle_bytes(plan: dict[str, Any]) -> bytes:
    files = {
        name: yaml.safe_dump(value, sort_keys=False, allow_unicode=True).encode("utf-8")
        for name, value in {
            "solution.yaml": plan["manifest"],
            "workflow.yaml": plan["definition"],
            "design.yaml": plan["design"],
        }.items()
    }
    files["sbom.json"] = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "components": [
                {
                    "type": "application",
                    "name": item["id"],
                    "version": item["version"],
                    "bom-ref": f"{item['kind']}:{item['id']}",
                    "hashes": [{"alg": "SHA-256", "content": item["contentDigest"][7:]}],
                }
                for item in plan["manifest"]["spec"]["components"]
            ],
        },
        sort_keys=True,
    ).encode("utf-8")
    files["provenance.json"] = json.dumps(
        {
            "builderId": "sage:solution-workbench",
            "sourceUri": f"sage:workflow-draft:{plan['draft_id']}",
            "sourceDigest": plan["build_digest"],
            "buildType": "sage-solution-workbench-v1",
        },
        sort_keys=True,
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, value)
    return output.getvalue()
