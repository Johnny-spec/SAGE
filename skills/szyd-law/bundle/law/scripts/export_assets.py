"""Build-time extraction. The installed skills never need the source repository."""

import argparse
import ast
import hashlib
import json
from pathlib import Path


def literals(path):
    result = {}
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = value
    return result


def flatten_rules(value):
    found = []
    if isinstance(value, dict):
        if value.get("序号") is not None and value.get("决策事项"):
            found.append(value)
        for child in value.values():
            found.extend(flatten_rules(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(flatten_rules(child))
    return found


def export(source, destination):
    package = source / "soe_compliance_workflow"
    identification = literals(package / "agents/smjy_identify_agent.py")
    items = {}
    duplicates = []
    for item in identification["details_decision_json"]:
        key = str(item["序号"])
        if key in items:
            if items[key] != item:
                raise ValueError(f"Conflicting checklist ID: {key}")
            duplicates.append(key)
        items[key] = item
    rules = json.loads((package / "prompts/rules.json").read_text(encoding="utf-8-sig"))
    rule_ids = {str(item["序号"]) for item in flatten_rules(rules)}
    ontology = json.loads((package / "ontology/ontology.json").read_text(encoding="utf-8-sig"))
    schema = literals(package / "schema/graph_schema.py")
    schema = {key: value for key, value in schema.items() if key.isupper()}
    prompts = {}
    for path in sorted((package / "prompts").glob("*.py")):
        if path.name in {"finally.py", "promopt_openai2.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            expression = node.value
            if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute):
                if expression.func.attr == "from_messages" and expression.args:
                    expression = expression.args[0]
            try:
                value = ast.literal_eval(expression)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and "PROMPT" in target.id:
                    prompts[target.id] = value
    enterprise = ast.parse((package / "agents/enterprise_graph_extraction_agent.py").read_text(encoding="utf-8-sig"))
    for node in enterprise.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "from_messages":
                for target in node.targets:
                    if isinstance(target, ast.Name) and "PROMPT" in target.id:
                        prompts[target.id] = ast.literal_eval(node.value.args[0])
    assets = {
        "major.json": {"categories": identification["major_decision_config"], "items": list(items.values())},
        "rules.json": rules,
        "ontology.json": ontology,
        "graph_schema.json": schema,
        "prompts.json": prompts,
    }
    manifest = {
        "source": "LawAgents original workflow; company-specific, unverified legal currency",
        "deduplicated_ids": sorted(set(duplicates)),
        "unmapped_item_ids": sorted(set(items) - rule_ids),
        "checklist_count": len(items),
        "rule_count": len(rule_ids),
        "prompt_names": sorted(prompts),
        "sha256": {},
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in assets.items():
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        manifest["sha256"][name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        (destination / name).write_text(content, encoding="utf-8", newline="\n")
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "assets")
    arguments = parser.parse_args()
    print(json.dumps(export(arguments.source, arguments.destination), ensure_ascii=True, indent=2))