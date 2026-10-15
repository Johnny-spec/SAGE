import json
import subprocess
import sys
from pathlib import Path

from sage.adapters.subprocess import build_subprocess_argv
from sage.models import ArgumentBinding
from sage.registry import SkillRegistry

ENTRY = Path(__file__).parents[1] / "skills/szyd-law/scripts/sage_entry.py"


def invoke(operation, *, state=None, message="", payload=None, cwd=None):
    result = subprocess.run(
        [
            sys.executable,
            str(ENTRY),
            "--operation",
            operation,
            "--state",
            json.dumps(state),
            "--payload",
            json.dumps(payload or {}),
            "--message",
            message,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def test_offline_bundle_and_supplement_resume(tmp_path):
    assert invoke("preflight", cwd=tmp_path)["data"]["valid"] is True
    opened = invoke("open", message="Review an investment proposal")
    assert opened["decision"] == {"action": "RUN_SKILLS", "skills": ["smjy"]}
    stage = {
        "version": 1,
        "status": "INSUFFICIENT",
        "data": {"is_major": None, "matched_categories": [], "matched_items": []},
        "reasoning": "Missing scope",
        "evidence_refs": [],
        "needs": ["Investment amount"],
    }
    waiting = invoke(
        "stage", state=opened["state"], payload={"name": "identification", "stage": stage}
    )
    assert waiting["decision"]["action"] == "SUPPLEMENT"
    resumed = invoke("resume", state=waiting["state"], message="Investment amount is 10 million")
    assert resumed["state"]["version"] == 2
    assert resumed["state"]["supplement_round"] == 1
    assert resumed["state"]["stages"] == {}


def test_gate_rejects_early_final_and_out_of_order_stage():
    state = invoke("open", message="Review proposal")["state"]
    assert "error" in invoke("final", state=state, payload={})
    assert "error" in invoke("stage", state=state, payload={"name": "legal", "stage": {}})


def test_package_binding_serializes_structured_arguments():
    definition = SkillRegistry.load_definition(ENTRY.parents[1])
    assert definition.name == "szyd-law"
    value = {"flag": True, "absent": None, "items": [1, "text"]}
    arguments = build_subprocess_argv(
        [ArgumentBinding(name="payload", option="--payload")],
        {"payload": value},
    )
    assert json.loads(arguments[1]) == value
