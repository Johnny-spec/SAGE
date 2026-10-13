"""Semantic closure validation for product skill registries and playbooks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _external_skill_ids(registry: dict[str, Any], skill_root: Path) -> set[str]:
    result: set[str] = set()
    for relative in registry.get("external_registries", []) or []:
        path = (skill_root / str(relative)).resolve()
        if not path.is_file():
            continue
        external = _read_json(path)
        result.update((external.get("skills") or {}).keys())
    return result


def validate_registry(registry_path: Path, playbook_path: Path) -> list[str]:
    registry_path = registry_path.resolve()
    playbook_path = playbook_path.resolve()
    skill_root = (
        registry_path.parent.parent
        if registry_path.parent.name == "_shared"
        else registry_path.parent
    )
    errors: list[str] = []

    try:
        registry = _read_json(registry_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"registry: {exc}"]
    try:
        playbook = _read_json(playbook_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"playbook: {exc}"]

    schema_ref = registry.get("$schema")
    if schema_ref and not (registry_path.parent / str(schema_ref)).resolve().is_file():
        errors.append(f"registry schema does not exist: {schema_ref}")

    skills = registry.get("skills") or {}
    problem_types = registry.get("problem_types") or {}
    if not isinstance(skills, dict) or not skills:
        errors.append("registry.skills must be a non-empty object")
        skills = {}
    if not isinstance(problem_types, dict):
        errors.append("registry.problem_types must be an object")
        problem_types = {}

    external_skills = _external_skill_ids(registry, skill_root)
    overrides = registry.get("skill_overrides") or {}
    resolvable = set(skills) | external_skills | set(overrides)
    for alias, target in overrides.items():
        if target not in skills and target not in external_skills:
            errors.append(f"skill override {alias}: target does not exist: {target}")

    for skill_id, skill in skills.items():
        if not isinstance(skill, dict):
            errors.append(f"skill {skill_id}: definition must be an object")
            continue
        entries = skill.get("entry") or {}
        if not isinstance(entries, dict) or not entries:
            errors.append(f"skill {skill_id}: entry must be a non-empty object")
            continue
        for runtime, relative in entries.items():
            entry_path = (skill_root / str(relative)).resolve()
            if not entry_path.is_file():
                errors.append(f"skill {skill_id}: {runtime} entry missing: {relative}")
        for runtime in skill.get("runtime_order") or []:
            if runtime not in entries:
                errors.append(f"skill {skill_id}: runtime_order references undeclared {runtime}")
        for runtime in (skill.get("runtime_status") or {}):
            if runtime not in entries:
                errors.append(f"skill {skill_id}: runtime_status references undeclared {runtime}")
        for problem_type in skill.get("triggers_for") or []:
            if problem_type not in problem_types:
                errors.append(f"skill {skill_id}: unknown triggers_for problem type {problem_type}")

    for problem_type, definition in problem_types.items():
        if not isinstance(definition, dict):
            errors.append(f"problem type {problem_type}: definition must be an object")
            continue
        for skill_id in definition.get("follow_ups", []) or []:
            if skill_id not in resolvable:
                errors.append(f"problem type {problem_type}: unknown follow-up skill {skill_id}")

    playbook_types = playbook.get("problem_types") or {}
    for problem_type in problem_types:
        if problem_type not in playbook_types:
            errors.append(f"playbook missing problem type {problem_type}")
    for problem_type, definition in playbook_types.items():
        if not isinstance(definition, dict):
            errors.append(f"playbook {problem_type}: definition must be an object")
            continue
        for section in ("next_steps", "additional_logs_needed"):
            for item in definition.get(section, []) or []:
                skill_id = item.get("skill") if isinstance(item, dict) else None
                if skill_id and skill_id not in resolvable:
                    errors.append(f"playbook {problem_type}.{section}: unknown skill {skill_id}")

    return sorted(set(errors))


def validate_manager_registry(registry_path: Path) -> list[str]:
    """Validate root-manager delegates without imposing product playbook rules."""
    registry_path = registry_path.resolve()
    repository_root = registry_path.parents[2]
    errors: list[str] = []
    try:
        registry = _read_json(registry_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"manager registry: {exc}"]

    skill = registry.get("skill")
    if not isinstance(skill, dict) or skill.get("id") != "orchestrator":
        errors.append("manager skill.id must equal orchestrator")
    else:
        python_entry = (skill.get("entry") or {}).get("python")
        if not python_entry:
            errors.append("manager skill.entry.python is required")
        elif not (repository_root / str(python_entry)).is_file():
            errors.append(f"manager python entry missing: {python_entry}")

    delegates = registry.get("delegates")
    if not isinstance(delegates, dict):
        errors.append("manager delegates must be an object")
        delegates = {}
    for product in ("IIS", "Edge"):
        definition = delegates.get(product)
        if not isinstance(definition, dict):
            errors.append(f"manager delegate missing: {product}")
            continue
        if not definition.get("orchestrator"):
            errors.append(f"manager delegate {product}.orchestrator is required")
        for key, relative in definition.items():
            if key in {"orchestrator", "diagnostics", "registry"}:
                if not (repository_root / str(relative)).is_file():
                    errors.append(f"manager delegate path missing: {product}.{key}={relative}")

    validation = registry.get("validation") or {}
    validator = validation.get("validator") if isinstance(validation, dict) else None
    if not validator or not (repository_root / str(validator)).is_file():
        errors.append(f"manager validator path missing: {validator}")
    if int(validation.get("minimum_rounds", 0) or 0) < 3:
        errors.append("manager validation.minimum_rounds must be at least 3")

    report = registry.get("report") or {}
    required_sections = set(report.get("required_sections") or [])
    expected_sections = {
        "analysis_process", "evidence_chain", "possible_causes",
        "solutions", "missing_logs", "invocation_chain",
    }
    for section in sorted(expected_sections - required_sections):
        errors.append(f"manager report missing required section: {section}")
    schema_path = registry_path.parent / "manager_report.schema.json"
    try:
        schema = _read_json(schema_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"manager report schema: {exc}")
    else:
        schema_required = set(schema.get("required") or [])
        for section in sorted(expected_sections - schema_required):
            errors.append(f"manager report schema missing required field: {section}")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate product skill registry closure")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--playbook", type=Path, required=True)
    args = parser.parse_args(argv)
    errors = validate_registry(args.registry, args.playbook)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
