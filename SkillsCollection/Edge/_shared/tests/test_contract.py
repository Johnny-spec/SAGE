"""Smoke tests for Edge contract envelope (v2.3)."""
from __future__ import annotations

import json

import pytest

from _shared.contract import (
    Finding, Solution, NextStep, LogRequest, SkillResult, registry, skill_entry,
)


def test_envelope_has_structured_fields():
    r = SkillResult(skill="probe")
    d = json.loads(r.to_json())
    assert "solutions" in d
    assert "next_steps" in d
    assert "additional_logs_needed" in d
    assert d["solutions"] == []
    assert d["next_steps"] == []
    assert d["additional_logs_needed"] == []
    assert d["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert d["evidence_gaps"] == []
    assert d["collection_plan"] is None


def test_to_json_is_ascii_safe():
    """ensure_ascii=True so output never breaks Windows cp1252 consumers."""
    r = SkillResult(
        skill="probe",
        findings=[Finding(summary="contains arrow \u2192 and em-dash \u2014",
                          severity="critical")],
    )
    body = r.to_json()
    body.encode("ascii")  # must not raise


def test_recommendations_autoflatten_when_empty():
    r = SkillResult(
        skill="probe",
        solutions=[Solution(title="Reset profile", problem_ref="crash",
                            steps=["delete user data dir"])],
        next_steps=[NextStep(action="Check crashpad", skill="edge_crash")],
        additional_logs_needed=[LogRequest(log_kind="crashpad",
                                            why="need recent dumps")],
    )
    d = json.loads(r.to_json())
    recs = d["recommendations"]
    assert len(recs) == 3
    assert recs[0].startswith("[fix:crash]") and "Reset profile" in recs[0]
    assert recs[1].startswith("[next:edge_crash]")
    assert recs[2].startswith("[logs:crashpad]")


def test_recommendations_preserved_when_provided():
    r = SkillResult(
        skill="probe",
        recommendations=["explicit"],
        solutions=[Solution(title="x")],
    )
    d = json.loads(r.to_json())
    assert d["recommendations"] == ["explicit"]


def test_finding_severity_default():
    f = Finding(summary="x")
    assert f.severity == "info"
    assert f.evidence == {}


def test_edge_procmon_entry_resolves_to_local_wrapper():
    assert skill_entry("edge_procmon", "python").name == "procmon_analyzer.py"


def test_edge_authentication_problem_types_route_netlog():
    problem_types = registry()["problem_types"]

    for problem_type in (
        "auth_error", "kerberos_spn_mismatch", "auth_handshake_loop",
    ):
        assert problem_type in problem_types
        assert "edge_netlog" in problem_types[problem_type]["follow_ups"]


def test_contract_auto_derives_investigation_state_and_plan():
    rooted = json.loads(SkillResult(
        skill="probe", root_cause="proven", confidence="medium"
    ).to_json())
    assert rooted["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert rooted["root_cause"] is None
    assert rooted["raw"]["candidate_cause"] == "proven"

    proven = json.loads(SkillResult(
        skill="probe",
        root_cause="direct proof",
        confidence="high",
        evidence_state="ROOT_CAUSE_FOUND",
        raw={"quality": {
            "direct_join": True,
            "independent_sources": 1,
            "sample_sufficient": True,
            "capture_complete": True,
            "time_comparable": True,
            "blocking_gaps": False,
        }},
    ).to_json())
    assert proven["root_cause"] == "direct proof"
    assert proven["evidence_state"] == "ROOT_CAUSE_FOUND"

    collecting = json.loads(SkillResult(
        skill="probe",
        additional_logs_needed=[LogRequest(
            log_kind="netlog", why="Need browser trace", skill="edge_netlog"
        )],
    ).to_json())
    assert collecting["evidence_state"] == "COLLECTION_PLAN_READY"
    assert collecting["collection_plan"]["requests"][0]["mode"] == "interactive_reproduction"
