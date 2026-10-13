"""Offline local reset: archive business state and retain only MCP configuration."""

import argparse
import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sage.agent_drafts import agent_to_dict
from sage.catalog import initial_generation
from sage.control_models import ControlPlaneState
from sage.control_store import LocalControlStore
from sage.reference_solutions.incident_diagnostics import default_diagnostic_agent


def require_stopped(ports: list[int]) -> None:
    if not ports:
        raise ValueError("Specify all API and Temporal ports for this data directory")
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                raise ValueError(f"Port {port} is still listening; stop the owning services first")
        except (ConnectionRefusedError, TimeoutError, OSError):
            continue


def reset_state(
    root: Path, skills_root: Path, ports: list[int], *, exclude_mcp: tuple[str, ...] = (),
) -> dict:
    require_stopped(ports)
    if root.is_symlink() or root.is_junction():
        raise ValueError("Refusing a linked data directory")
    root = root.resolve(strict=True)
    skills_root = skills_root.resolve(strict=True)
    if root == skills_root or skills_root.is_relative_to(root):
        raise ValueError("Baseline Skills must be outside the data directory")
    state_file = root / "control-plane.json"
    original = state_file.read_bytes()
    previous = ControlPlaneState.model_validate(LocalControlStore._migrate(json.loads(original)))
    unknown = set(exclude_mcp) - previous.mcp_servers.keys()
    if unknown:
        raise ValueError(f"Unknown MCP server exclusions: {sorted(unknown)}")
    preserved_servers = {
        key: value for key, value in previous.mcp_servers.items() if key not in exclude_mcp
    }
    preserved_reviews = {
        key: value for key, value in previous.mcp_test_reviews.items()
        if value.server_id not in exclude_mcp
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    backup = root.with_name(root.name + ".backup-" + stamp)
    staging = root.with_name(root.name + ".reset-" + stamp)
    try:
        baseline = staging / "generations" / "gen_startup" / "skills"
        shutil.copytree(skills_root, baseline)
        agent = default_diagnostic_agent()
        generation = initial_generation(
            baseline, {agent.agent_id: agent_to_dict(agent)}, agent.agent_id,
        ).model_copy(update={"skills_root": str(root / "generations/gen_startup/skills")})
        store = LocalControlStore(staging, generation)

        def preserve(state: ControlPlaneState) -> None:
            state.mcp_servers = preserved_servers
            state.mcp_test_reviews = preserved_reviews

        store.update(preserve)
        verified = LocalControlStore(staging).snapshot()
        if verified.mcp_servers != preserved_servers:
            raise ValueError("MCP preservation verification failed")
        require_stopped(ports)
        if state_file.read_bytes() != original:
            raise ValueError("State changed while preparing reset; refusing to proceed")
        root.rename(backup)
        try:
            staging.rename(root)
        except BaseException:
            backup.rename(root)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "data_root": str(root), "backup": str(backup),
        "mcp_servers_preserved": len(preserved_servers),
        "mcp_risk_reviews_preserved": len(preserved_reviews),
        "solutions": 0, "releases": 0, "workflow_drafts": 0,
        "custom_agents": 0, "uploaded_skills": 0, "tool_registrations": 0,
        "candidates": 0, "approvals": 0, "historical_generations": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--skills-root", type=Path, default=Path(__file__).parents[1] / "skills")
    parser.add_argument("--ports", type=int, nargs="+", required=True)
    parser.add_argument("--exclude-mcp", nargs="*", default=[])
    parser.add_argument("--confirm-reset", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(reset_state(
        args.data_root, args.skills_root, args.ports, exclude_mcp=tuple(args.exclude_mcp),
    ), indent=2))


if __name__ == "__main__":
    main()