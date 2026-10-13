from __future__ import annotations

import json
from pathlib import Path

import pytest

from result_contract import (
    EVIDENCE_STATES,
    ResultContractError,
    SEVERITIES,
    SUPPORTED_SCHEMA_VERSIONS,
    V24_REQUIRED_FIELDS,
    contract_failure,
    enforce_root_quality,
    normalize_result,
    operational_failure,
    validate_result,
)


def test_normalize_result_accepts_and_marks_legacy_envelope():
    result = normalize_result({"skill": "probe", "ok": True}, expected_skill="probe")

    assert result["schema_version"] == "2.3"
    assert result["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert result["raw"]["contract_validation"]["valid"] is True
    assert len(result["raw"]["contract_validation"]["warnings"]) == 2


def test_validate_result_rejects_invalid_nested_fields_and_skill_mismatch():
    issues = validate_result({
        "skill": "other",
        "ok": "yes",
        "findings": [{"summary": "x", "severity": "fatal"}],
    }, expected_skill="probe")

    paths = {issue.path for issue in issues}
    assert {"skill", "ok", "findings[0].severity"} <= paths


def test_validate_result_preserves_original_array_indexes():
    issues = validate_result({
        "skill": "probe",
        "ok": True,
        "findings": ["invalid", {"summary": "x", "severity": "fatal"}],
    })

    paths = {issue.path for issue in issues}
    assert {"findings[0]", "findings[1].severity"} <= paths


def test_normalize_result_raises_for_invalid_envelope():
    with pytest.raises(ResultContractError):
        normalize_result({"skill": "probe", "ok": True, "raw": []})


def test_explicit_v24_requires_complete_normalized_envelope():
    issues = validate_result({
        "schema_version": "2.4",
        "skill": "probe",
        "ok": True,
    })

    paths = {issue.path for issue in issues}
    assert V24_REQUIRED_FIELDS - {"schema_version", "skill", "ok"} <= paths


def test_root_cause_found_requires_explicit_quality_proof():
    base = operational_failure("probe", "unused", code="unused")
    base.update({
        "ok": True,
        "root_cause": "proven cause",
        "confidence": "high",
        "evidence_state": "ROOT_CAUSE_FOUND",
        "error": None,
    })
    base["raw"].pop("failure")

    issues = validate_result(base)
    assert any(issue.path == "raw.quality" for issue in issues)

    base["raw"]["quality"] = {
        "direct_join": True,
        "independent_sources": 1,
        "sample_sufficient": True,
        "capture_complete": True,
        "time_comparable": True,
        "blocking_gaps": False,
    }
    assert validate_result(base) == []


def test_direct_root_is_downgraded_without_quality_proof():
    result = {
        "skill": "probe",
        "ok": True,
        "findings": [],
        "root_cause": "unproven",
        "confidence": "high",
        "evidence_state": "ROOT_CAUSE_FOUND",
        "raw": {},
    }

    gated = enforce_root_quality(result)

    assert gated["root_cause"] is None
    assert gated["confidence"] == "low"
    assert gated["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert gated["raw"]["candidate_cause"] == "unproven"
    assert gated["raw"]["root_quality_gate"]["promoted"] is False


def test_contract_failure_is_non_promotable_and_bounded():
    issues = validate_result(["not", "an", "object"])
    result = contract_failure("probe", issues, source_excerpt="x" * 1000)

    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["confidence"] == "low"
    assert result["evidence_state"] == "INCONCLUSIVE_EVIDENCE"
    assert result["raw"]["contract_validation"]["valid"] is False
    metadata = result["raw"]["invalid_output"]
    assert metadata["character_count"] == 1000
    assert len(metadata["sha256"]) == 64
    assert "stdout_excerpt" not in result["raw"]


def test_operational_failure_is_complete_and_drops_command_context():
    result = operational_failure(
        "probe",
        "timed out after 10s",
        code="timeout",
        execution={
            "runtime": "python",
            "stage": "execute",
            "timeout_seconds": 10,
            "command": ["python", "probe.py", "secret-context"],
        },
    )

    assert validate_result(result, expected_skill="probe") == []
    assert result["schema_version"] == "2.4"
    assert result["ok"] is False
    assert result["root_cause"] is None
    assert result["confidence"] == "low"
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "timeout"
    assert "command" not in result["raw"]["execution"]


def test_canonical_and_product_schema_mirrors_are_aligned():
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "Common" / "schemas" / "skill_result.schema.json",
        root / "IIS" / "_shared" / "schemas" / "skill_result.schema.json",
        root / "Edge" / "_shared" / "schemas" / "skill_result.schema.json",
    ]
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    assert schemas[0] == schemas[1] == schemas[2]
    properties = schemas[0]["properties"]
    assert set(properties["schema_version"]["enum"]) == SUPPORTED_SCHEMA_VERSIONS
    assert set(properties["evidence_state"]["enum"]) == EVIDENCE_STATES
    assert set(properties["findings"]["items"]["properties"]["severity"]["enum"]) == SEVERITIES
    assert set(schemas[0]["allOf"][0]["then"]["required"]) == V24_REQUIRED_FIELDS