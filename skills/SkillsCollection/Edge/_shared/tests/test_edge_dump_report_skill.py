from __future__ import annotations

import json
import re
from pathlib import Path


def _frontmatter(text: str) -> str:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    return match.group(1)


def test_edge_dump_report_skill_metadata_and_workflow(project_root: Path):
    skill_path = project_root / "edge-dump-report" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)

    assert re.search(r"^name: edge-dump-report$", frontmatter, re.MULTILINE)
    assert re.search(r"^description: .+", frontmatter, re.MULTILINE)
    assert "user-invocable: true" in frontmatter
    assert "disable-model-invocation: false" in frontmatter
    assert len(text.splitlines()) <= 500

    for required in (
        "WinDbg/*",
        ".load mex",
        "!analyze -v",
        ".exr -1",
        ".ecxr",
        "lmf",
        "Get-WinEvent",
        "Compare all dumps",
        "Classify each component",
        "confidence rules",
        "Write a Markdown report",
    ):
        assert required in text


def test_edge_dump_report_is_registered_as_agent_only(project_root: Path):
    registry = json.loads(
        (project_root / "_shared" / "registry.json").read_text(encoding="utf-8")
    )
    workflow = registry["agent_workflows"]["edge-dump-report"]

    assert workflow["folder"] == "edge-dump-report"
    assert workflow["role"] == "agent_workflow"
    assert workflow["requires_mcp_servers"] == ["WinDbg"]
    assert "WinDbg/*" in workflow["requires_tools"]
    assert workflow["orchestrator_dispatch"] is False
    assert "edge-dump-report" not in registry["skills"]
    assert all(
        "edge-dump-report" not in problem.get("follow_ups", [])
        for problem in registry["problem_types"].values()
    )


def test_edge_dump_custom_agent_grants_windbg_tools(project_root: Path):
    agent_path = (
        project_root.parent / ".github" / "agents" / "edge-dump.agent.md"
    )
    text = agent_path.read_text(encoding="utf-8")

    assert "'WinDbg/*'" in text
    assert "../../Edge/edge-dump-report/SKILL.md" in text
    assert "Python child skills cannot call" in text