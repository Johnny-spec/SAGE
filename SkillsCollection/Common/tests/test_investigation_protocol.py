from __future__ import annotations

from dataclasses import asdict

from investigation_protocol import (
    EvidenceState,
    aggregate_root_quality,
    build_collection_plan,
    derive_evidence_state,
)


def test_collection_plan_is_readonly_deduped_and_actionable():
    requests = [
        {"log_kind": "evtx", "why": "Need events", "skill": "event_log"},
        {"log_kind": "evtx", "why": "Duplicate", "skill": "event_log"},
        {"log_kind": "netlog", "why": "Need browser evidence", "skill": "netlog"},
    ]
    plan = build_collection_plan(
        requests,
        mode="offline_bundle",
        time_range={"start": "2026-08-11T10:00:00Z", "end": "2026-08-11T10:10:00Z"},
        products=["IIS", "Edge"],
    )
    encoded = asdict(plan)
    assert len(encoded["requests"]) == 2
    assert encoded["safety"]["execute_automatically"] is False
    assert all(command["readonly"] for request in encoded["requests"]
               for command in request["commands"])
    evtx = next(item for item in encoded["requests"] if item["log_kind"] == "evtx")
    assert evtx["requires_admin"] is True
    assert evtx["validation"]["must_cover_time_range"] is True


def test_procmon_collection_is_operator_reviewed_sensitive_and_bounded():
    plan = build_collection_plan([
        {"log_kind": "procmon", "why": "Need process I/O evidence", "skill": "procmon"}
    ])

    request = plan.requests[0]
    assert request.mode == "interactive_reproduction"
    assert request.requires_admin is True
    assert "command_line" in request.sensitivity
    assert request.validation["must_cover_time_range"] is True
    assert request.commands[0].shell == "manual"
    assert request.commands[0].readonly is True
    assert plan.safety["execute_automatically"] is False


def test_evidence_state_precedence_is_explicit():
    assert derive_evidence_state(permission_blocked=True) == EvidenceState.PERMISSION_BLOCKED
    assert derive_evidence_state(tool_unavailable=True) == EvidenceState.TOOL_UNAVAILABLE
    assert derive_evidence_state(time_not_comparable=True) == EvidenceState.TIME_NOT_COMPARABLE
    assert derive_evidence_state(missing_required=True) == EvidenceState.MISSING_REQUIRED_EVIDENCE
    assert derive_evidence_state(
        root_cause="x", confidence="high"
    ) == EvidenceState.INCONCLUSIVE_EVIDENCE
    assert derive_evidence_state() == EvidenceState.INCONCLUSIVE_EVIDENCE


def test_root_cause_requires_traceable_quality_signals():
    common = {
        "root_cause": "x",
        "confidence": "medium",
        "sample_sufficient": True,
        "capture_complete": True,
    }
    assert derive_evidence_state(
        **common, direct_join=True
    ) == EvidenceState.ROOT_CAUSE_FOUND
    assert derive_evidence_state(
        **common, independent_sources=2
    ) == EvidenceState.ROOT_CAUSE_FOUND
    assert derive_evidence_state(
        **common, direct_join=True, blocking_gaps=True
    ) == EvidenceState.INCONCLUSIVE_EVIDENCE
    assert derive_evidence_state(
        root_cause="x",
        confidence="high",
        direct_join=True,
        sample_sufficient=False,
        capture_complete=True,
    ) == EvidenceState.INCONCLUSIVE_EVIDENCE


def test_aggregate_root_quality_ignores_candidates_and_reduces_proof():
    candidate = {
        "ok": True,
        "root_cause": "candidate",
        "evidence_state": EvidenceState.INCONCLUSIVE_EVIDENCE,
        "raw": {"quality": {"direct_join": True}},
    }
    proven = {
        "ok": True,
        "root_cause": "proven",
        "evidence_state": EvidenceState.ROOT_CAUSE_FOUND,
        "raw": {"quality": {
            "direct_join": True,
            "independent_sources": 1,
            "sample_sufficient": True,
            "capture_complete": True,
            "time_comparable": True,
            "blocking_gaps": False,
        }},
    }

    quality = aggregate_root_quality([candidate, proven])

    assert quality["proven_results"] == 1
    assert quality["direct_join"] is True
    assert quality["sample_sufficient"] is True
    assert quality["capture_complete"] is True
    assert quality["blocking_gaps"] is False