from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _children(count: int = 2):
    values = {
        "iis_logs": {
            "ok": True,
            "confidence": "medium",
            "findings": [{
                "summary": "500.19 with win32 status 5 at https://user:secret@app/x?token=secret",
                "severity": "critical",
                "evidence": {"signature": "500.19/5", "source_file": r"C:\private\u_ex.log"},
            }],
        },
        "event_log": {
            "ok": True,
            "confidence": "medium",
            "findings": [{
                "summary": "Configuration access denied",
                "severity": "warning",
                "evidence": {"event_id": 5002},
            }],
        },
    }
    return dict(list(values.items())[:count])


def _analyses(key_b: str = "config-access", refs_b=None):
    refs_b = refs_b or ["E0001"]
    return {
        "model_a": {
            "confidence": 0.8,
            "claims": [{
                "claim_key": "config-access",
                "conclusion": "IIS configuration access is denied",
                "problem_type": "5xx_error",
                "confidence": 0.8,
                "evidence_refs": ["E0001"],
                "falsifiers": ["The same request succeeds under the same identity"],
                "recommended_next_evidence": ["evtx"],
            }],
        },
        "model_b": {
            "confidence": 0.7,
            "claims": [{
                "claim_key": key_b,
                "conclusion": (
                    "IIS configuration access is denied"
                    if key_b == "config-access" else "The application pool crashed"
                ),
                "problem_type": "5xx_error",
                "confidence": 0.7,
                "evidence_refs": refs_b,
                "falsifiers": ["A different component has a direct request-level failure"],
            }],
        },
    }


def test_validator_without_providers_is_honest(validator_module, monkeypatch):
    monkeypatch.delenv(validator_module.PROVIDER_ENV, raising=False)
    result = validator_module.analyze({"extra": {"children": _children()}})
    assert result["cross_validation"]["status"] == "not_configured"
    assert result["root_cause"] is None
    assert result["confidence"] == "low"
    assert result["raw"]["provider_request"]


def test_validator_requires_common_evidence_and_two_sources(validator_module):
    result = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {"children": _children(), "model_analyses": _analyses()},
    })
    assert result["cross_validation"]["status"] == "completed"
    assert result["root_cause"] is None
    assert result["confidence"] == "low"
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert result["raw"]["candidate_cause"] == "IIS configuration access is denied"
    assert result["raw"]["quality"]["capture_complete"] is False
    assert "secret" not in json.dumps(result["raw"]["evidence_pack"])
    evidence = next(
        item["evidence"]
        for item in result["raw"]["evidence_pack"]["items"]
        if "source_file" in item["evidence"]
    )
    assert evidence["source_file"]["basename"] == "u_ex.log"

    one_source = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {"children": _children(1), "model_analyses": _analyses()},
    })
    assert one_source["cross_validation"]["status"] == "insufficient_evidence_sources"
    assert one_source["root_cause"] is None


def test_validator_counts_duplicate_wrappers_as_one_source_group(validator_module):
    result = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {
            "children": _children(),
            "evidence_source_groups": {
                "iis_logs": "shared-capture",
                "event_log": "shared-capture",
            },
            "model_analyses": _analyses(),
        },
    })

    assert result["cross_validation"]["status"] == "insufficient_evidence_sources"
    assert result["cross_validation"]["consensus"]["actual_evidence_sources"] == 1
    assert result["root_cause"] is None


def test_validator_excludes_background_observations_from_claimable_pack(
    validator_module,
):
    children = _children()
    children["iis_logs"]["findings"].append({
        "summary": "Updater connection refused 500 times",
        "severity": "info",
        "evidence_id": "EV-BACKGROUND",
        "evidence": {
            "type": "network_failure",
            "signal_classification": "background",
            "symptom_relevance": "low",
            "count": 500,
        },
    })

    pack = validator_module.build_evidence_pack({
        "extra": {"children": children},
    })

    assert "EV-BACKGROUND" not in {item["id"] for item in pack["items"]}
    assert pack["integrity"]["background_observations_excluded"] == 1
    assert pack["integrity"]["background_excluded_by_skill"] == {"iis_logs": 1}


def test_validator_surfaces_conflict_and_invalid_refs(validator_module):
    conflict = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {"children": _children(), "model_analyses": _analyses("pool-crash")},
    })
    assert conflict["cross_validation"]["status"] == "conflict"
    assert conflict["root_cause"] is None
    assert conflict["evidence_gaps"][0]["code"] == "model_interpretation_conflict"

    invalid = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {
            "children": _children(),
            "model_analyses": _analyses(refs_b=["E9999"]),
        },
    })
    assert invalid["cross_validation"]["status"] != "completed"

    same_key_different_conclusion = _analyses()
    same_key_different_conclusion["model_b"]["claims"][0]["conclusion"] = (
        "The application pool crashed because of memory exhaustion"
    )
    divergent = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {
            "children": _children(),
            "model_analyses": same_key_different_conclusion,
        },
    })
    assert divergent["cross_validation"]["status"] == "conflict"
    assert divergent["root_cause"] is None


def test_incident_input_cannot_supply_provider_command(
    validator_module, monkeypatch, tmp_path: Path
):
    marker = tmp_path / "executed.txt"
    monkeypatch.delenv(validator_module.PROVIDER_ENV, raising=False)
    result = validator_module.analyze({
        "extra": {
            "children": _children(),
            "provider_commands": [[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]],
        }
    })
    assert result["cross_validation"]["status"] == "not_configured"
    assert not marker.exists()
    assert result["raw"]["provider_configuration"]["command_from_incident_input_allowed"] is False


def test_trusted_environment_provider_adapter_runs_without_shell(
    validator_module, monkeypatch
):
    provider_code = (
        "import json,sys; r=json.load(sys.stdin); "
        "print(json.dumps({'confidence':0.6,'claims':[{'claim_key':'k','conclusion':'c',"
        "'evidence_refs':['E0001'],'falsifiers':['not-c'],'confidence':0.6}]}))"
    )
    configs = [
        {"name": "provider_a", "command": [sys.executable, "-c", provider_code]},
        {"name": "provider_b", "command": [sys.executable, "-c", provider_code]},
    ]
    monkeypatch.setenv(validator_module.PROVIDER_ENV, json.dumps(configs))
    result = validator_module.analyze({"extra": {"children": _children()}})
    assert result["cross_validation"]["status"] == "completed"
    assert result["raw"]["provider_configuration"]["shell_execution_used"] is False


def test_failed_provider_is_reported_as_tool_unavailable(
    validator_module, monkeypatch
):
    monkeypatch.setenv(
        validator_module.PROVIDER_ENV,
        json.dumps([{"name": "broken", "command": ["does-not-exist-provider"]}]),
    )
    result = validator_module.analyze({"extra": {"children": _children()}})
    assert result["cross_validation"]["status"] == "provider_error"
    assert result["ok"] is False
    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["evidence_gaps"][0]["code"] == "validation_provider_error"
    assert result["evidence_gaps"][0]["blocking"] is True


def test_validator_reports_agent_model_diversity_and_rejects_shared_context(
    validator_module,
):
    analyses = _analyses()
    analyses["model_a"].update({
        "model": "same-model",
        "agent_id": "agent-a",
        "independence_group": "run-a",
    })
    analyses["model_b"].update({
        "model": "same-model",
        "agent_id": "agent-b",
        "independence_group": "run-b",
    })
    result = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {"children": _children(), "model_analyses": analyses},
    })
    diversity = result["cross_validation"]["consensus"]["diversity"]
    assert diversity["mode"] == "multi_agent_single_model"
    assert diversity["confidence_factor"] == 0.85

    analyses["model_b"]["independence_group"] = "run-a"
    duplicate_context = validator_module.analyze({
        "problem_type": "5xx_error",
        "extra": {"children": _children(), "model_analyses": analyses},
    })
    assert duplicate_context["cross_validation"]["status"] == "insufficient_independence"
    assert duplicate_context["root_cause"] is None


def test_validator_rejects_ungrounded_and_unfalsifiable_claims(validator_module):
    evidence_pack = validator_module.build_evidence_pack({
        "extra": {"children": _children()}
    })
    normalized = validator_module.normalize_analyses([{
        "analyzer": "model_a",
        "claims": [
            {"conclusion": "no refs", "falsifiers": ["x"]},
            {
                "conclusion": "invalid refs",
                "evidence_refs": ["E9999"],
                "falsifiers": ["x"],
            },
            {
                "conclusion": "no falsifier",
                "evidence_refs": ["E0001"],
            },
        ],
    }], evidence_pack)

    assert normalized[0]["claims"] == []
    assert {item["reason"] for item in normalized[0]["rejected_claims"]} == {
        "no_valid_evidence_refs", "missing_falsifiers"
    }


def test_evidence_ids_are_stable_across_finding_order_and_accept_legacy_aliases(
    validator_module,
):
    left = _children()
    left["iis_logs"]["findings"].append({
        "summary": "second finding",
        "severity": "info",
        "evidence": {"record_locator": "line:42"},
    })
    right = json.loads(json.dumps(left))
    right["iis_logs"]["findings"].reverse()

    first_pack = validator_module.build_evidence_pack({"extra": {"children": left}})
    second_pack = validator_module.build_evidence_pack({"extra": {"children": right}})
    assert {item["id"] for item in first_pack["items"]} == {
        item["id"] for item in second_pack["items"]
    }
    assert all(item["id"].startswith("E-") for item in first_pack["items"])

    normalized = validator_module.normalize_analyses([{
        "analyzer": "model_a",
        "claims": [{
            "conclusion": "legacy alias remains supported",
            "evidence_refs": ["E0001"],
            "falsifiers": ["the referenced evidence is unrelated"],
        }],
    }], first_pack)
    assert normalized[0]["claims"][0]["evidence_refs"] == [
        first_pack["items"][0]["id"]
    ]


def test_validator_reuses_producer_evidence_id(validator_module):
    children = _children(1)
    children["iis_logs"]["findings"][0]["evidence_id"] = "EV-producer-123"

    evidence_pack = validator_module.build_evidence_pack({
        "extra": {"children": children}
    })

    assert evidence_pack["items"][0]["id"] == "EV-producer-123"
    assert evidence_pack["items"][0]["producer_evidence_id"] == "EV-producer-123"