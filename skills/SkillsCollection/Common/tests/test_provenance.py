from __future__ import annotations

from provenance import attach_finding_provenance


def test_finding_ids_are_stable_across_order():
    findings = [
        {"summary": "a", "severity": "warning", "evidence": {"line_number": 4}},
        {"summary": "b", "severity": "info", "evidence": {"record_id": 9}},
    ]
    first = attach_finding_provenance({"skill": "probe", "findings": findings})
    second = attach_finding_provenance({
        "skill": "probe", "findings": list(reversed(findings))
    })

    assert {item["evidence_id"] for item in first["findings"]} == {
        item["evidence_id"] for item in second["findings"]
    }


def test_provenance_distinguishes_located_partial_and_unlocated():
    result = attach_finding_provenance({
        "skill": "probe",
        "findings": [
            {
                "summary": "located",
                "severity": "warning",
                "evidence": {"source_file": "u_ex.log", "line_number": 42},
            },
            {
                "summary": "partial",
                "severity": "warning",
                "evidence": {"event_id": 1309},
            },
            {"summary": "unlocated", "severity": "info", "evidence": {}},
        ],
    })

    qualities = [item["provenance"]["quality"] for item in result["findings"]]
    assert qualities == ["located", "partial", "unlocated"]
    assert result["findings"][0]["provenance"]["record"]["line_number"] == 42