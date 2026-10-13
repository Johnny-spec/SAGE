#!/usr/bin/env python3
"""Safe SAGE binding for the imported SkillsCollection root orchestrator."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

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


def _load_manager() -> ModuleType:
    skills_root = Path(__file__).resolve().parents[2]
    manager_path = skills_root / "SkillsCollection" / "orchestrator" / "scripts" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("sage_imported_manager", manager_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load the imported SkillsCollection orchestrator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _context(
    message: str,
    evidence_path: Path | None,
    child_skill: str | None = None,
) -> tuple[dict[str, Any], Path | None]:
    extra: dict[str, Any] = {
        "symptoms": message,
        "incident_mode": "offline_bundle",
        "skip_validation": True,
    }
    folder = None
    if evidence_path is not None:
        if not evidence_path.exists():
            raise ValueError("Uploaded evidence does not exist")
        folder = evidence_path if evidence_path.is_dir() else evidence_path.parent
        if evidence_path.is_file() and evidence_path.suffix.casefold() == ".json":
            head = evidence_path.read_text(encoding="utf-8-sig", errors="replace")[: 1024 * 1024]
            if '"constants"' in head[:1024] and any(
                marker in head for marker in ('"logSourceType"', '"logEventTypes"')
            ):
                extra["netlog_paths"] = [str(evidence_path)]
    payload: dict[str, Any] = {
        "question": message,
        "summary": message,
        "extra": extra,
    }
    if child_skill:
        payload["skills_to_trigger"] = [child_skill]
    return payload, folder


def _validate_child(product: str, child_skill: str | None) -> None:
    if child_skill is None:
        return
    if product not in _SAFE_CHILDREN or child_skill not in _SAFE_CHILDREN[product]:
        raise ValueError(
            f"Child Skill {child_skill!r} is not enabled for Web execution under {product!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True)
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--product", choices=("auto", "IIS", "Edge", "both"), default="auto")
    parser.add_argument("--child-skill")
    arguments = parser.parse_args()

    manager = _load_manager()
    _validate_child(arguments.product, arguments.child_skill)
    payload, folder = _context(
        arguments.message,
        arguments.evidence_path,
        arguments.child_skill,
    )
    manager_input = manager.ManagerInput(
        mode="folder" if folder is not None else "context",
        payload=payload,
        folder=folder,
        label=arguments.evidence_path.name if arguments.evidence_path else "web-request",
    )
    result = manager.orchestrate_manager(
        manager_input,
        explicit_product=arguments.product,
        options=manager.ManagerOptions(
            product_timeout=90,
            validator_timeout=30,
            allow_live_host=False,
            skip_cross_validation=True,
        ),
    )
    print(json.dumps(manager.redact_result(result), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
