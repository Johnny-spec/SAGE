"""Compatibility validation for diagnostic skill result envelopes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

from investigation_protocol import EvidenceState
from provenance import attach_finding_provenance


LEGACY_SCHEMA_VERSION = "2.3"
CURRENT_SCHEMA_VERSION = "2.4"
SUPPORTED_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
SEVERITIES = {"critical", "warning", "info"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
V24_REQUIRED_FIELDS = {
    "schema_version", "skill", "ok", "findings", "root_cause", "confidence",
    "recommendations", "solutions", "next_steps", "additional_logs_needed",
    "cross_validation", "evidence_state", "evidence_gaps", "collection_plan",
    "raw", "error",
}
EVIDENCE_STATES = {
    EvidenceState.ROOT_CAUSE_FOUND,
    EvidenceState.INCONCLUSIVE_EVIDENCE,
    EvidenceState.MISSING_REQUIRED_EVIDENCE,
    EvidenceState.TIME_NOT_COMPARABLE,
    EvidenceState.PERMISSION_BLOCKED,
    EvidenceState.TOOL_UNAVAILABLE,
    EvidenceState.UNKNOWN_LOG_TYPE,
    EvidenceState.LIVE_COLLECTION_REQUIRED,
    EvidenceState.COLLECTION_PLAN_READY,
}


@dataclass(frozen=True)
class ContractIssue:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class ResultContractError(ValueError):
    def __init__(self, issues: list[ContractIssue]):
        self.issues = issues
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        super().__init__(detail)


def _check_optional_type(
    result: Mapping[str, Any],
    key: str,
    expected_type: type | tuple[type, ...],
    issues: list[ContractIssue],
) -> None:
    if key in result and not isinstance(result[key], expected_type):
        issues.append(ContractIssue(key, f"must be {expected_type}"))


def _validate_object_list(
    result: Mapping[str, Any], key: str, issues: list[ContractIssue]
) -> list[tuple[int, Mapping[str, Any]]]:
    value = result.get(key, [])
    if not isinstance(value, list):
        issues.append(ContractIssue(key, "must be an array"))
        return []
    objects: list[tuple[int, Mapping[str, Any]]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            issues.append(ContractIssue(f"{key}[{index}]", "must be an object"))
        else:
            objects.append((index, item))
    return objects


def validate_result(
    result: object, *, expected_skill: str | None = None
) -> list[ContractIssue]:
    """Return structural contract violations without mutating the result."""
    if not isinstance(result, Mapping):
        return [ContractIssue("$", "must be an object")]

    issues: list[ContractIssue] = []
    skill = result.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        issues.append(ContractIssue("skill", "must be a non-empty string"))
    elif expected_skill is not None and skill != expected_skill:
        issues.append(ContractIssue("skill", f"must equal {expected_skill!r}"))

    if not isinstance(result.get("ok"), bool):
        issues.append(ContractIssue("ok", "must be a boolean"))

    schema_version = result.get("schema_version")
    if schema_version is not None:
        if not isinstance(schema_version, str):
            issues.append(ContractIssue("schema_version", "must be a string"))
        elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            issues.append(ContractIssue(
                "schema_version", f"unsupported version {schema_version!r}"
            ))
        elif schema_version == CURRENT_SCHEMA_VERSION:
            for key in sorted(V24_REQUIRED_FIELDS - result.keys()):
                issues.append(ContractIssue(key, "is required by schema version 2.4"))

    confidence = result.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_LEVELS:
        issues.append(ContractIssue("confidence", "must be high, medium, or low"))

    evidence_state = result.get("evidence_state")
    if evidence_state is not None and evidence_state not in EVIDENCE_STATES:
        issues.append(ContractIssue("evidence_state", "contains an unknown state"))
    elif evidence_state == EvidenceState.ROOT_CAUSE_FOUND:
        raw = result.get("raw")
        quality = raw.get("quality") if isinstance(raw, Mapping) else None
        if not isinstance(result.get("root_cause"), str) or not result["root_cause"].strip():
            issues.append(ContractIssue(
                "root_cause", "must be a non-empty string for ROOT_CAUSE_FOUND"
            ))
        if result.get("confidence") not in {"medium", "high"}:
            issues.append(ContractIssue(
                "confidence", "must be medium or high for ROOT_CAUSE_FOUND"
            ))
        if not isinstance(quality, Mapping):
            issues.append(ContractIssue(
                "raw.quality", "is required for ROOT_CAUSE_FOUND"
            ))
        else:
            independent_sources = quality.get("independent_sources", 0)
            supported = quality.get("direct_join") is True or (
                isinstance(independent_sources, int)
                and not isinstance(independent_sources, bool)
                and independent_sources >= 2
            )
            if not supported:
                issues.append(ContractIssue(
                    "raw.quality",
                    "requires direct_join=true or at least two independent_sources",
                ))
            for key in ("sample_sufficient", "capture_complete", "time_comparable"):
                if quality.get(key) is not True:
                    issues.append(ContractIssue(
                        f"raw.quality.{key}", "must be true for ROOT_CAUSE_FOUND"
                    ))
            if quality.get("blocking_gaps") is not False:
                issues.append(ContractIssue(
                    "raw.quality.blocking_gaps",
                    "must be false for ROOT_CAUSE_FOUND",
                ))

    _check_optional_type(result, "root_cause", (str, type(None)), issues)
    _check_optional_type(result, "cross_validation", (Mapping, type(None)), issues)
    _check_optional_type(result, "collection_plan", (Mapping, type(None)), issues)
    _check_optional_type(result, "raw", Mapping, issues)
    _check_optional_type(result, "error", (str, type(None)), issues)
    _check_optional_type(result, "generated_at", str, issues)

    findings = _validate_object_list(result, "findings", issues)
    for index, finding in findings:
        if not isinstance(finding.get("summary"), str):
            issues.append(ContractIssue(
                f"findings[{index}].summary", "must be a string"
            ))
        if finding.get("severity") not in SEVERITIES:
            issues.append(ContractIssue(
                f"findings[{index}].severity",
                "must be critical, warning, or info",
            ))
        if "evidence" in finding and not isinstance(finding["evidence"], Mapping):
            issues.append(ContractIssue(
                f"findings[{index}].evidence", "must be an object"
            ))

    for key in ("solutions", "next_steps", "additional_logs_needed", "evidence_gaps"):
        _validate_object_list(result, key, issues)

    recommendations = result.get("recommendations", [])
    if not isinstance(recommendations, list):
        issues.append(ContractIssue("recommendations", "must be an array"))
    else:
        for index, item in enumerate(recommendations):
            if not isinstance(item, str):
                issues.append(ContractIssue(
                    f"recommendations[{index}]", "must be a string"
                ))
    return issues


def normalize_result(
    result: object, *, expected_skill: str | None = None
) -> dict[str, Any]:
    """Validate and add v2.3-compatible defaults to a child result."""
    issues = validate_result(result, expected_skill=expected_skill)
    if issues:
        raise ResultContractError(issues)

    normalized = deepcopy(dict(result))
    warnings: list[str] = []
    if "schema_version" not in normalized:
        normalized["schema_version"] = LEGACY_SCHEMA_VERSION
        warnings.append("schema_version missing; interpreted as legacy v2.3")
    if "evidence_state" not in normalized:
        normalized["evidence_state"] = EvidenceState.INCONCLUSIVE_EVIDENCE
        warnings.append("evidence_state missing; defaulted to INCONCLUSIVE_EVIDENCE")

    normalized.setdefault("findings", [])
    normalized.setdefault("root_cause", None)
    normalized.setdefault("confidence", "low")
    normalized.setdefault("recommendations", [])
    normalized.setdefault("solutions", [])
    normalized.setdefault("next_steps", [])
    normalized.setdefault("additional_logs_needed", [])
    normalized.setdefault("cross_validation", None)
    normalized.setdefault("evidence_gaps", [])
    normalized.setdefault("collection_plan", None)
    normalized.setdefault("raw", {})
    normalized.setdefault("error", None)
    normalized = attach_finding_provenance(normalized)

    contract_validation = normalized["raw"].setdefault("contract_validation", {})
    contract_validation.update({
        "valid": True,
        "interpreted_version": normalized["schema_version"],
        "warnings": warnings,
    })
    return normalized


def enforce_root_quality(result: Mapping[str, Any]) -> dict[str, Any]:
    """Reserve root_cause for envelopes that satisfy the semantic proof gate."""
    output = deepcopy(dict(result))
    root_cause = output.get("root_cause")
    if not root_cause:
        return output
    if output.get("evidence_state") != EvidenceState.ROOT_CAUSE_FOUND:
        output.setdefault("raw", {}).setdefault("candidate_cause", root_cause)
        output["root_cause"] = None
        output["confidence"] = "low"
        return output

    proof_issues = [
        issue for issue in validate_result(output)
        if issue.path == "root_cause"
        or issue.path == "confidence"
        or issue.path.startswith("raw.quality")
    ]
    if not proof_issues:
        output.setdefault("raw", {}).setdefault("root_quality_gate", {
            "promoted": True,
            "issues": [],
        })
        return output

    output.setdefault("raw", {}).setdefault("candidate_cause", root_cause)
    output["raw"]["root_quality_gate"] = {
        "promoted": False,
        "issues": [issue.to_dict() for issue in proof_issues],
    }
    output["root_cause"] = None
    output["confidence"] = "low"
    plan = output.get("collection_plan")
    output["evidence_state"] = (
        EvidenceState.COLLECTION_PLAN_READY
        if isinstance(plan, Mapping) and plan.get("requests")
        else EvidenceState.INCONCLUSIVE_EVIDENCE
    )
    return output


def contract_failure(
    skill: str, issues: list[ContractIssue], *, source_excerpt: str | None = None
) -> dict[str, Any]:
    """Build a bounded failure envelope for malformed child output."""
    raw: dict[str, Any] = {
        "contract_validation": {
            "valid": False,
            "issues": [issue.to_dict() for issue in issues],
        }
    }
    if source_excerpt is not None:
        encoded = source_excerpt.encode("utf-8", errors="replace")
        raw["invalid_output"] = {
            "character_count": len(source_excerpt),
            "sha256": sha256(encoded).hexdigest(),
        }
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "skill": skill,
        "ok": False,
        "findings": [],
        "root_cause": None,
        "confidence": "low",
        "recommendations": [],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": [],
        "cross_validation": None,
        "evidence_state": EvidenceState.INCONCLUSIVE_EVIDENCE,
        "evidence_gaps": [],
        "collection_plan": None,
        "raw": raw,
        "error": "child result failed contract validation",
    }


def operational_failure(
    skill: str,
    error: str,
    *,
    code: str,
    evidence_state: str = EvidenceState.TOOL_UNAVAILABLE,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete failure envelope without retaining command context."""
    if evidence_state not in EVIDENCE_STATES:
        raise ValueError(f"unknown evidence state: {evidence_state}")
    raw: dict[str, Any] = {
        "failure": {"code": code},
        "contract_validation": {
            "valid": True,
            "interpreted_version": CURRENT_SCHEMA_VERSION,
            "warnings": [],
        },
    }
    if execution:
        raw["execution"] = {
            str(key): value
            for key, value in execution.items()
            if key in {"runtime", "stage", "timeout_seconds", "exit_code"}
        }
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "skill": skill,
        "ok": False,
        "findings": [],
        "root_cause": None,
        "confidence": "low",
        "recommendations": [],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": [],
        "cross_validation": None,
        "evidence_state": evidence_state,
        "evidence_gaps": [],
        "collection_plan": None,
        "raw": raw,
        "error": error,
    }


__all__ = [
    "CONFIDENCE_LEVELS", "ContractIssue", "CURRENT_SCHEMA_VERSION",
    "EVIDENCE_STATES", "LEGACY_SCHEMA_VERSION", "ResultContractError",
    "SEVERITIES", "SUPPORTED_SCHEMA_VERSIONS", "contract_failure",
    "V24_REQUIRED_FIELDS", "enforce_root_quality", "normalize_result",
    "operational_failure",
    "validate_result",
]