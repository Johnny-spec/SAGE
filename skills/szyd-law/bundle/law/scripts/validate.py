"""Offline distribution checks, independent of services and original source."""

import ast
import hashlib
import importlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NAMES = ("law", "smjy", "facts", "funds", "proc", "docs", "supp", "final")


def validate_bundle():
    yaml = importlib.import_module("yaml")
    checks = []
    for name in NAMES:
        path = ROOT / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("Missing YAML frontmatter")
        metadata = yaml.safe_load(parts[1])
        if metadata.get("name") != name or not isinstance(metadata.get("description"), str) or not 1 <= len(metadata["description"]) <= 1024:
            raise ValueError("Invalid skill discovery metadata")
        if len(text.splitlines()) >= 500:
            raise ValueError("Skill body too large")
        checks.append(name)
    for path in ROOT.rglob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                raise ValueError("Missing or external local resource link")
    assets = ROOT / "law/assets"
    manifest = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["sha256"].items():
        path = assets / name
        json.loads(path.read_text(encoding="utf-8"))
        if hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() != expected:
            raise ValueError("Bundled asset checksum mismatch")
    if manifest["unmapped_item_ids"]:
        raise ValueError("Unmapped checklist IDs")
    for path in (ROOT / "law/scripts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any(module.startswith(("soe_compliance_workflow", "langgraph", "langchain")) for module in modules):
                raise ValueError("Runtime depends on original agent stack")
    return {"valid": True, "status": "OK", "skills": checks, "checklist_count": manifest["checklist_count"],
            "asset_checksums": "verified", "services_checked": False}