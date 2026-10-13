import json
import socket
from pathlib import Path

import pytest

from sage.agent_drafts import agent_to_dict
from sage.catalog import initial_generation
from sage.control_models import McpServerRecord, SolutionDefinitionRecord
from sage.control_store import LocalControlStore
from sage.reference_solutions.incident_diagnostics import default_diagnostic_agent
from scripts.reset_local_state import require_stopped, reset_state


@pytest.mark.parametrize("schema_version", [12, 13])
@pytest.mark.parametrize("exclude_demo", [False, True])
def test_reset_preserves_mcp_and_archives_all_business_files(
    tmp_path, schema_version, exclude_demo,
):
    skills = Path(__file__).parent / "fixtures/skills"
    agent = default_diagnostic_agent()
    generation = initial_generation(skills, {agent.agent_id: agent_to_dict(agent)}, agent.agent_id)
    root = tmp_path / "data"
    store = LocalControlStore(root, generation)

    def populate(state):
        state.mcp_servers["mcp-demo"] = McpServerRecord(
            server_id="mcp-demo", alias="keep", transport="streamable-http",
            url="https://example.test/mcp", secret_ref="env:DEMO_SECRET",
            config_digest="sha256:" + "a" * 64,
        )
        state.solutions["old"] = SolutionDefinitionRecord(
            solution_id="old", title="Old", owner="tester",
        )

    store.update(populate)
    previous_mcp = store.snapshot().mcp_servers
    state_file = root / "control-plane.json"
    raw_state = json.loads(state_file.read_text(encoding="utf-8"))
    raw_state["schema_version"] = schema_version
    state_file.write_text(json.dumps(raw_state), encoding="utf-8")
    (root / "runs.sqlite3").write_bytes(b"old-run-history")
    (root / "temporal.db").write_bytes(b"old-temporal-history")
    original = (root / "control-plane.json").read_bytes()
    result = reset_state(
        root, skills, [65534], exclude_mcp=("mcp-demo",) if exclude_demo else (),
    )
    current = LocalControlStore(root).snapshot()
    assert current.mcp_servers == ({} if exclude_demo else previous_mcp)
    assert not current.solutions
    assert len(current.generations) == 1
    assert not (root / "runs.sqlite3").exists()
    assert not (root / "temporal.db").exists()
    backup = Path(result["backup"])
    assert (backup / "control-plane.json").read_bytes() == original
    assert (backup / "runs.sqlite3").read_bytes() == b"old-run-history"
    assert Path(current.generations["gen_startup"].skills_root).is_dir()


def test_reset_refuses_running_services():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(ValueError, match="still listening"):
            require_stopped([listener.getsockname()[1]])


def test_reset_requires_explicit_ports():
    with pytest.raises(ValueError, match="Specify"):
        require_stopped([])