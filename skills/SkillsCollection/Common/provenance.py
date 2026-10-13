"""Stable finding identity and lightweight source/record provenance."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping


SOURCE_KEYS = (
    "source_file", "source_path", "file", "path", "source", "log",
)
RECORD_KEYS = (
    "record_locator", "line_number", "lineno", "record_id", "event_id",
    "source_id", "pid", "request_id",
)
TIME_KEYS = ("normalized_time_utc", "timestamp", "time", "original_time")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _first(evidence: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for key in keys:
        value = evidence.get(key)
        if value not in (None, "", [], {}):
            return key, value
    return None


def finding_evidence_id(skill: str, finding: Mapping[str, Any]) -> str:
    explicit = finding.get("evidence_id")
    if explicit:
        return str(explicit)
    identity = {
        "skill": skill,
        "summary": finding.get("summary"),
        "severity": finding.get("severity"),
        "evidence": finding.get("evidence") or {},
    }
    digest = hashlib.sha256(_canonical(identity).encode("ascii")).hexdigest()[:20]
    return f"EV-{digest}"


def derive_provenance(evidence: Mapping[str, Any]) -> dict[str, Any]:
    source = _first(evidence, SOURCE_KEYS)
    records = {
        key: evidence[key]
        for key in RECORD_KEYS
        if evidence.get(key) not in (None, "", [], {})
    }
    observed_time = _first(evidence, TIME_KEYS)
    quality = "located" if source and records else "partial" if source or records else "unlocated"
    return {
        "quality": quality,
        "source": (
            {"field": source[0], "value": source[1]} if source else None
        ),
        "record": records,
        "time": (
            {"field": observed_time[0], "value": observed_time[1]}
            if observed_time else None
        ),
    }


def attach_finding_provenance(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with stable IDs/provenance on every valid finding."""
    output = deepcopy(dict(result))
    skill = str(output.get("skill") or "unknown")
    findings = output.get("findings")
    if not isinstance(findings, list):
        return output
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding["evidence_id"] = finding_evidence_id(skill, finding)
        evidence = finding.get("evidence")
        finding["provenance"] = derive_provenance(
            evidence if isinstance(evidence, Mapping) else {}
        )
    return output


__all__ = [
    "attach_finding_provenance", "derive_provenance", "finding_evidence_id",
]