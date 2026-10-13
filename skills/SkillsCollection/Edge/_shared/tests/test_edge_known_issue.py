"""Tests for edge_known_issue deep-investigation planning output."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


EDGE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = EDGE_ROOT / "edge_known_issue" / "scripts" / "edge_known_issue.py"


def _run(ctx: dict, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), json.dumps(ctx)],
        capture_output=True,
        text=True,
        cwd=str(EDGE_ROOT),
        check=check,
    )


def test_known_issue_requires_problem_text():
    proc = _run({"problem_type": "known_issue"}, check=False)
    assert proc.returncode != 0
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert "Missing problem description" in (out.get("error") or "")


def test_known_issue_emits_deep_lookup_by_default():
    proc = _run({
        "question": "Edge 149 works but 150 download popup hangs",
        "problem_type": "known_issue",
    })
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    raw = out["raw"]
    assert "query_plan" in raw
    assert "lane_results" in raw
    assert "match_ranking" in raw
    assert raw["known_issue_lookup"]["enabled"] is True
    assert len(raw["known_issue_lookup"]["suggested_calls"]) >= 4
    keywords = raw["query_plan"]["keyword_groups"][0]["keywords"]
    assert "download hub" in keywords
    assert any(ns.get("skill") == "edge_known_issue_mcp" for ns in out["next_steps"])


def test_known_issue_can_disable_mcp_and_request_report_mode():
    proc = _run({
        "summary": "Investigate Edge sign-in regression after update",
        "extra": {
            "use_known_issue_mcp": False,
            "report_mode": "html_bilingual",
        },
    })
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    raw = out["raw"]
    assert raw["known_issue_lookup"]["enabled"] is False
    assert raw["known_issue_lookup"]["suggested_calls"] == []
    assert any(ns.get("skill") == "edge-known-issue-report" for ns in out["next_steps"])


def test_known_issue_sanitizes_emails_for_remote_lanes():
    proc = _run({
        "question": "User alice@example.com says popup hangs after update",
        "problem_type": "known_issue",
    })
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert "[redacted-email]" in out["raw"]["problem_text"]
    assert any("sanitized" in f.get("summary", "").lower() for f in out["findings"])


def test_known_issue_merges_mcp_results_into_ranking_and_evidence():
    proc = _run({
        "question": "Edge 149 works but 150 download popup hangs",
        "problem_type": "known_issue",
        "mcp_results": {
            "organization_resolution": {
                "microsoft_tool_family": "mcp_azuredevops_2",
                "cssdigital_tool_family": "mcp_azuredevops_m",
            },
            "lane_results": {
                "ado_microsoft": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "BUG-100",
                            "title": "Edge download popup hang after 150 update",
                            "summary": "Bug links popup hang to the Edge 150 download flow.",
                            "url": "https://dev.azure.com/microsoft/Edge/_workitems/edit/100",
                            "match_strength": "exact",
                            "version_polarity": "aligned",
                            "surface_layer": "aligned",
                            "fix_version": "150.0.200.0",
                        }
                    ],
                },
                "ado_cssdigital": {"status": "completed", "results": []},
                "workiq": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "THREAD-7",
                            "title": "CSS confirms popup hang workaround",
                            "summary": "Internal thread references BUG-100 and lists a workaround.",
                            "url": "https://contoso/thread/7",
                            "match_strength": "adjacent",
                            "version_polarity": "aligned",
                            "surface_layer": "aligned",
                        }
                    ],
                },
                "public_web": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "CHROMIUM-9",
                            "title": "Chromium popup regression",
                            "summary": "Public Chromium issue tracks similar popup behavior.",
                            "url": "https://issues.chromium.org/9",
                            "match_strength": "adjacent",
                            "version_polarity": "aligned",
                            "surface_layer": "aligned",
                        }
                    ],
                },
                "recent_commits": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "abc123",
                            "title": "Adjust popup download flow",
                            "summary": "Recent adjacent change in the download popup logic.",
                            "url": "https://dev.azure.com/microsoft/Edge/_git/chromium.src/commit/abc123",
                            "surface_layer": "aligned",
                        }
                    ],
                },
            },
        },
    })
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["confidence"] == "high"
    raw = out["raw"]
    best = raw["match_ranking"]["ranked_best_match"]
    assert best["id"] == "BUG-100"
    assert best["match_strength"] == "exact"
    assert raw["evidence_matrix"][0]["status"] == "collected"
    assert any(ev["evidence_id"] == "ado_microsoft:BUG-100" for ev in raw["evidence_matrix"])
    assert raw["version_polarity_check"]["status"] == "aligned"
    assert raw["surface_layer_check"]["status"] == "aligned"
    assert raw["query_plan"]["organization_resolution"]["resolved"]["microsoft_tool_family"] == "mcp_azuredevops_2"
    assert raw["adjacent_changes"][0]["lane"] == "recent_commits"
    assert all(ns.get("skill") != "edge_known_issue_mcp" for ns in out["next_steps"])


def test_known_issue_demotes_conflicting_exact_match():
    proc = _run({
        "question": "Edge 149 works but 150 download popup hangs",
        "problem_type": "known_issue",
        "mcp_results": {
            "lane_results": {
                "ado_microsoft": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "BUG-BAD",
                            "title": "Bug introduced in 149 instead of 150",
                            "summary": "Looks similar but the version direction is wrong.",
                            "match_strength": "exact",
                            "version_polarity": "conflict",
                            "surface_layer": "aligned",
                        }
                    ],
                },
                "public_web": {
                    "status": "completed",
                    "results": [
                        {
                            "id": "PUB-GOOD",
                            "title": "Adjacent public reference with aligned version",
                            "summary": "Not perfect, but version direction matches the symptom.",
                            "match_strength": "adjacent",
                            "version_polarity": "aligned",
                            "surface_layer": "aligned",
                        }
                    ],
                },
            },
        },
    })
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    best = out["raw"]["match_ranking"]["ranked_best_match"]
    assert best["id"] == "PUB-GOOD"
    assert out["raw"]["version_polarity_check"]["status"] == "conflict"
    assert any("adjacent" in gap for gap in out["raw"]["not_found_gaps"])