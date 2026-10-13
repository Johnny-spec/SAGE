"""Repository-level deterministic release checks for diagnostic skills."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from registry_validation import validate_manager_registry, validate_registry


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATHS = (
    ROOT / "Common" / "schemas" / "skill_result.schema.json",
    ROOT / "IIS" / "_shared" / "schemas" / "skill_result.schema.json",
    ROOT / "Edge" / "_shared" / "schemas" / "skill_result.schema.json",
)
PRODUCT_CONTRACTS = (
    (
        "IIS",
        ROOT / "IIS" / "_shared" / "registry.json",
        ROOT / "IIS" / "_shared" / "playbook.json",
    ),
    (
        "Edge",
        ROOT / "Edge" / "_shared" / "registry.json",
        ROOT / "Edge" / "_shared" / "playbook.json",
    ),
)
MANAGER_REGISTRY = ROOT / "orchestrator" / "_shared" / "registry.json"


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def validate_schema_mirrors() -> list[str]:
    errors: list[str] = []
    schemas: list[Any] = []
    for path in SCHEMA_PATHS:
        try:
            schemas.append(_read_json(path))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"schema {path.relative_to(ROOT)}: {exc}")
    if len(schemas) == len(SCHEMA_PATHS):
        canonical = schemas[0]
        for path, schema in zip(SCHEMA_PATHS[1:], schemas[1:]):
            if schema != canonical:
                errors.append(
                    f"schema mirror differs from Common: {path.relative_to(ROOT)}"
                )
    return errors


def validate_product_contracts() -> list[str]:
    errors: list[str] = []
    for product, registry_path, playbook_path in PRODUCT_CONTRACTS:
        for error in validate_registry(registry_path, playbook_path):
            errors.append(f"{product}: {error}")
    return errors


def validate_manager_report_runtime() -> list[str]:
    module_path = ROOT / "orchestrator" / "scripts" / "manager_report.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "quality_gate_manager_report", module_path
        )
        if spec is None or spec.loader is None:
            return ["manager report module could not be loaded"]
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        report = module.build_manager_report(
            report_id="MR-0000000000000000",
            products={},
            status={
                "ok": False, "evidence_state": "MISSING_REQUIRED_EVIDENCE",
                "confidence": "low", "root_cause": None,
                "root_cause_refs": [],
            },
            routing={"products": [], "ambiguous": True},
            input_summary={"mode": "context", "label": "quality-gate"},
            invocation_nodes=[],
            validation_rounds=[
                {"round": 1, "name": "product", "status": "skipped"},
                {"round": 2, "name": "manager", "status": "skipped"},
                {"round": 3, "name": "consistency", "status": "completed"},
            ],
            root_quality={}, evidence_gaps=[], collection_plan=None,
            next_steps=[],
        )
        return list(module.validate_manager_report(report))
    except Exception as exc:
        return [f"manager report runtime validation failed: {type(exc).__name__}: {exc}"]


def run_static_checks() -> dict[str, Any]:
    checks = {
        "schema_mirrors": validate_schema_mirrors(),
        "product_contracts": validate_product_contracts(),
        "manager_registry": validate_manager_registry(MANAGER_REGISTRY),
        "manager_report_runtime": validate_manager_report_runtime(),
    }
    errors = [
        f"{check}: {error}"
        for check, values in checks.items()
        for error in values
    ]
    return {"ok": not errors, "checks": checks, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate cross-product skill release contracts"
    )
    parser.parse_args(argv)
    result = run_static_checks()
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())