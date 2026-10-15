"""UTF-8 JSON tool interface used by the host agent; no autonomous LLM calls."""

import argparse
import json
import sys
from pathlib import Path

from rules import aggregate, asset, core_triggers, lookup_rules, normalize
from schemas import State, gate, resume, validate_final, validate_stage, validate_state


def execute(arguments, value):
    command = arguments.command
    if command == "rules":
        return lookup_rules(value)
    if command == "normalize":
        return normalize(value)
    if command == "trigger":
        return core_triggers(value)
    if command == "aggregate":
        return aggregate(value)
    if command == "gate":
        return gate(value)
    if command == "resume":
        return resume(value)
    if command == "schema":
        from schemas import FinalResult, Stage
        return {"state": State.model_json_schema(), "stage": Stage.model_json_schema(), "final_result": FinalResult.model_json_schema()}
    if command == "init":
        return validate_state(value).model_dump()
    if command == "prompt":
        prompts = asset("prompts.json")
        if arguments.name not in prompts:
            raise ValueError("Unknown source prompt")
        return {"name": arguments.name, "guidance_only": True, "prompt": prompts[arguments.name]}
    if command == "validate":
        if arguments.kind == "bundle":
            from validate import validate_bundle
            return validate_bundle()
        if arguments.kind == "stage":
            validate_stage(value["name"], value["stage"])
        elif arguments.kind == "final":
            return validate_final(value)
        else:
            validate_state(value)
        return {"valid": True}
    if command == "preflight":
        from validate import validate_bundle
        result = validate_bundle()
        if arguments.services:
            from retrieval import preflight_services
            result["integration"] = preflight_services()
            result["status"] = result["integration"]["status"]
        else:
            result["services_checked"] = False
        return result
    from retrieval import backfill, chroma_retrieve, neo4j_retrieve, reasoning, rerank
    if command == "retrieve":
        return neo4j_retrieve(value) if arguments.backend == "neo4j" else chroma_retrieve(value)
    if command == "rerank":
        return rerank(value)
    if command == "reason":
        return reasoning(value)
    if command == "backfill":
        return backfill(value, arguments.apply)
    raise ValueError("Unknown operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = ("init", "schema", "prompt", "rules", "normalize", "trigger", "aggregate", "gate", "resume", "validate", "preflight", "retrieve", "rerank", "reason", "backfill")
    for command in commands:
        child = subparsers.add_parser(command)
        child.add_argument("--output", type=Path)
        if command not in {"schema", "prompt", "preflight"}:
            child.add_argument("--input", type=Path, required=command != "validate")
        if command == "prompt":
            child.add_argument("--name", required=True)
        if command == "validate":
            child.add_argument("--kind", choices=("stage", "state", "final", "bundle"), default="state")
        if command == "retrieve":
            child.add_argument("--backend", choices=("neo4j", "chroma"), required=True)
        if command == "backfill":
            child.add_argument("--apply", action="store_true")
        if command == "preflight":
            group = child.add_mutually_exclusive_group()
            group.add_argument("--offline", action="store_true")
            group.add_argument("--services", action="store_true")
    arguments = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    exit_code = 0
    try:
        path = getattr(arguments, "input", None)
        if arguments.command == "validate" and arguments.kind != "bundle" and path is None:
            raise ValueError("--input required for data validation")
        value = json.loads(path.read_text(encoding="utf-8-sig")) if path else {}
        result = execute(arguments, value)
        if result.get("status") == "BLOCKED" or result.get("valid") is False:
            exit_code = 2
    except Exception as error:
        from retrieval import ServiceError
        safe = isinstance(error, ServiceError)
        result = {"status": "BLOCKED" if safe or isinstance(error, ImportError) else "ERROR", "error_type": type(error).__name__,
                  "reason": str(error) if safe else "Operation failed; check input against the documented schema and service configuration"}
        if hasattr(error, "errors"):
            result["diagnostics"] = [{"field": list(item["loc"]), "type": item["type"]} for item in error.errors(include_input=False)]
        exit_code = 2
    output = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if arguments.output:
        try:
            arguments.output.write_text(output, encoding="utf-8")
        except OSError:
            result = {"status": "ERROR", "reason": "Cannot write output file"}
            output = json.dumps(result) + "\n"
            exit_code = 2
    sys.stdout.write(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())