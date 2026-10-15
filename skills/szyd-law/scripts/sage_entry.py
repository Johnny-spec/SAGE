"""Stateless, offline-only bridge for the bundled compliance contracts."""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle/law/scripts"))

from rules import asset, lookup_rules  # noqa: E402
from schemas import gate, resume, validate_final, validate_state  # noqa: E402
from validate import validate_bundle  # noqa: E402


def transition(operation, state, message, payload):
    if operation == "preflight":
        return {"data": validate_bundle()}
    if operation == "schema":
        from schemas import FinalResult, Review, Stage, State

        return {
            "data": {
                model.__name__: model.model_json_schema()
                for model in (State, Stage, Review, FinalResult)
            }
        }
    if operation == "rules":
        return {"data": lookup_rules(payload)}
    if operation == "prompt":
        return {"data": {"guidance_only": True, "prompt": asset("prompts.json")[payload["name"]]}}
    if not state:
        if operation != "open" or not message.strip():
            raise ValueError("Open a case with nonempty business material first")
        state = validate_state(
            {"run_id": f"case_{uuid4().hex}", "input_text": message}
        ).model_dump()
    state = deepcopy(state)
    decision = gate(state)
    if state.get("final_result") and operation not in {"open", "inspect"}:
        raise ValueError("Completed case is immutable; use a new thread")
    if operation == "resume":
        state = resume({"state": state, "addition": {"input_text": message, **payload}})["state"]
    elif operation == "evidence":
        if state["stages"]:
            raise ValueError("Register evidence before analysis, or supply it during resume")
        state["evidence"].extend(payload["evidence"])
    elif operation == "stage":
        from schemas import CHILDREN

        name = payload["name"]
        if decision["action"] != "RUN_SKILLS" or CHILDREN.get(name) not in decision["skills"]:
            raise ValueError("Stage is not authorized by Gate")
        state["stages"][name] = payload["stage"]
    elif operation == "review":
        if decision["action"] != "REVIEW":
            raise ValueError("Gate does not authorize a review")
        state["review"] = payload
    elif operation == "final":
        validate_final({"state": state, "final_result": payload})
        state["final_result"] = payload
    elif operation not in {"open", "inspect"}:
        raise ValueError("Unsupported offline operation")
    state = validate_state(state).model_dump()
    return {"state": state, "decision": gate(state), "completed": bool(state["final_result"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--state", default="null")
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--message", default="")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--payload-file", type=Path)
    parser.add_argument("--message-file", type=Path)
    arguments = parser.parse_args()
    try:
        state = (
            json.loads(arguments.state_file.read_text(encoding="utf-8"))
            if arguments.state_file
            else json.loads(arguments.state)
        )
        payload = (
            json.loads(arguments.payload_file.read_text(encoding="utf-8"))
            if arguments.payload_file
            else json.loads(arguments.payload)
        )
        message = (
            json.loads(arguments.message_file.read_text(encoding="utf-8"))
            if arguments.message_file
            else arguments.message
        )
        result = transition(
            arguments.operation,
            state,
            message,
            payload,
        )
    except (ValueError, KeyError, TypeError) as error:
        result = {"error": type(error).__name__, "reason": str(error)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
