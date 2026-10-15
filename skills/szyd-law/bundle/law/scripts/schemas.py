"""Portable contracts and safe workflow transitions, without model calls."""

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


STAGES = ("identification", "legal", "capital", "procedures", "materials")
CHILDREN = {"identification": "smjy", "legal": "facts", "capital": "funds", "procedures": "proc", "materials": "docs"}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Contract):
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    content: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    kind: Literal["document", "law", "rule", "case", "graph"]
    verified: bool = False


class Stage(Contract):
    version: int = Field(ge=1)
    status: Literal["DONE", "INSUFFICIENT", "NON_COMPLIANT", "BLOCKED", "ERROR"]
    data: dict[str, Any]
    reasoning: str = Field(min_length=1)
    evidence_refs: list[str]
    needs: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class Review(Contract):
    version: int = Field(ge=1)
    approval_status: Literal["APPROVED", "REJECTED", "CONDITIONAL_APPROVAL", "NOT_APPLICABLE"]
    final_risk_assessment: Literal["LOW", "MEDIUM", "HIGH", "N/A"]
    reasoning: str = Field(min_length=1)
    evidence_refs: list[str]
    key_findings: list[str]
    recommendations: list[str]


class FinalResult(Contract):
    approval_status: Literal["APPROVED", "REJECTED", "CONDITIONAL_APPROVAL", "NOT_APPLICABLE"]
    final_risk_assessment: Literal["LOW", "MEDIUM", "HIGH", "N/A"]
    key_findings: list[str]
    compliance_summary: str = Field(min_length=1)
    recommendations: list[str]


class State(Contract):
    run_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    input_text: str = Field(min_length=1)
    file_list: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    stages: dict[str, Stage] = Field(default_factory=dict)
    supplement_round: int = Field(default=0, ge=0)
    max_supplement_rounds: int = Field(default=3, ge=1)
    review: Review | None = None
    final_result: FinalResult | None = None


def validate_stage(name, stage):
    if name not in STAGES:
        raise ValueError("Unknown stage")
    parsed = Stage.model_validate(stage)
    data = parsed.data
    if parsed.status in {"ERROR", "BLOCKED"}:
        return parsed
    if parsed.status == "INSUFFICIENT" and not parsed.needs:
        raise ValueError("INSUFFICIENT requires a concrete needs list")
    if name == "identification":
        if "is_major" not in data or (data["is_major"] is not None and type(data["is_major"]) is not bool):
            raise ValueError("is_major must be a boolean or null")
        if parsed.status == "DONE" and data["is_major"] is None:
            raise ValueError("Unknown classification cannot be DONE")
        if parsed.status == "NON_COMPLIANT":
            raise ValueError("Identification does not decide compliance")
        for key in ("matched_categories", "matched_items"):
            if not isinstance(data.get(key), list) or any(not isinstance(item, str) for item in data[key]):
                raise ValueError(f"{key} must be a list of strings")
        if data["is_major"] is True and parsed.status == "DONE" and not data["matched_items"]:
            raise ValueError("Major classification requires checklist IDs")
    elif name == "legal":
        for key in ("legal_facts", "laws_involved", "uncertainty_notes"):
            if not isinstance(data.get(key), list):
                raise ValueError(f"{key} must be a list")
    elif name == "capital":
        for key in ("financial_involvement", "asset_involvement", "equity_structure"):
            if not isinstance(data.get(key), dict):
                raise ValueError(f"{key} must be an object")
        for key in ("risk_points", "insufficient_information"):
            if not isinstance(data.get(key), list):
                raise ValueError(f"{key} must be a list")
    elif name == "procedures":
        for key in ("required_procedures", "detected_procedures", "missing_procedures", "reflecting_documents"):
            if not isinstance(data.get(key), list):
                raise ValueError(f"{key} must be a list")
        if data.get("compliance_status") not in {"COMPLIANT", "NON_COMPLIANT", "INSUFFICIENT_INFO"}:
            raise ValueError("Invalid procedure compliance_status")
        if data["compliance_status"] == "NON_COMPLIANT" and parsed.status != "NON_COMPLIANT":
            raise ValueError("Confirmed noncompliance requires NON_COMPLIANT stage status")
        if data["compliance_status"] == "INSUFFICIENT_INFO" and parsed.status != "INSUFFICIENT":
            raise ValueError("Insufficient procedures require INSUFFICIENT status")
    elif name == "materials":
        for key in ("required_documents", "provided_documents", "missing_documents"):
            if not isinstance(data.get(key), list):
                raise ValueError(f"{key} must be a list")
    return parsed


def validate_state(value):
    state = State.model_validate(value)
    evidence_ids = [item.id for item in state.evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Duplicate evidence IDs")
    known = set(evidence_ids)
    for name, stage in state.stages.items():
        validate_stage(name, stage.model_dump())
        if stage.version != state.version:
            raise ValueError(f"Stale stage: {name}")
        if set(stage.evidence_refs) - known:
            raise ValueError(f"Unknown evidence references: {name}")
    if state.review:
        if state.review.version != state.version or set(state.review.evidence_refs) - known:
            raise ValueError("Stale review or unknown review evidence")
    return state


def gate(value):
    state = validate_state(value)
    stages = state.stages
    for name, stage in stages.items():
        if stage.status in {"ERROR", "BLOCKED"}:
            return {"action": stage.status, "stage": name, "reason": stage.reasoning}
    if "identification" not in stages:
        return {"action": "RUN_SKILLS", "skills": ["smjy"]}
    identification = stages["identification"]
    if identification.status == "DONE" and identification.data["is_major"] is False:
        verified = {item.id for item in state.evidence if item.verified}
        if not identification.evidence_refs or not set(identification.evidence_refs).issubset(verified):
            return {"action": "MANUAL_REVIEW", "reason": "Classification lacks verified evidence"}
        return {"action": "FINAL_SKIP", "skills": ["final"]}
    if identification.status == "DONE":
        from rules import lookup_rules
        lookup_rules({"item_ids": identification.data["matched_items"]})
        missing = [CHILDREN[name] for name in ("legal", "capital", "procedures") if name not in stages]
        if missing:
            return {"action": "RUN_SKILLS", "skills": missing}
        if "materials" not in stages:
            return {"action": "RUN_SKILLS", "skills": ["docs"]}
    needs = list(dict.fromkeys(need for stage in stages.values() for need in stage.needs))
    for name in ("capital", "procedures", "materials"):
        if name in stages:
            key = {"capital": "insufficient_information", "procedures": "missing_procedures", "materials": "missing_documents"}[name]
            if stages[name].status != "NON_COMPLIANT":
                needs.extend(str(item) for item in stages[name].data.get(key, []) if str(item) not in needs)
    insufficient = any(stage.status == "INSUFFICIENT" for stage in stages.values())
    violation = any(stage.status == "NON_COMPLIANT" for stage in stages.values())
    if needs or insufficient:
        if violation:
            return {"action": "MANUAL_REVIEW", "reason": "Confirmed violation with unresolved evidence", "needs": needs}
        if state.supplement_round >= state.max_supplement_rounds:
            return {"action": "MANUAL_REVIEW", "reason": "Supplement limit reached", "needs": needs}
        return {"action": "SUPPLEMENT", "skills": ["supp"], "needs": needs}
    verified = {item.id for item in state.evidence if item.verified}
    for name, stage in stages.items():
        if not stage.evidence_refs or not set(stage.evidence_refs).issubset(verified):
            return {"action": "MANUAL_REVIEW", "reason": f"Unverified evidence in {name}"}
    legal = stages["legal"]
    if legal.data["uncertainty_notes"]:
        return {"action": "MANUAL_REVIEW", "reason": "Unresolved legal uncertainties"}
    legal_sources = {item.id for item in state.evidence if item.verified and item.kind in {"law", "rule"}}
    if not legal.data["legal_facts"] or not legal.data["laws_involved"] or not legal_sources.intersection(legal.evidence_refs):
        return {"action": "MANUAL_REVIEW", "reason": "Legal analysis lacks verified legal or institutional basis"}
    if not state.review:
        return {"action": "REVIEW", "reason": "Agent must record an evidence-grounded review, not automatic approval"}
    review = state.review
    used_refs = {ref for stage in stages.values() for ref in stage.evidence_refs}
    if not review.evidence_refs or not set(review.evidence_refs).issubset(used_refs & verified):
        return {"action": "MANUAL_REVIEW", "reason": "Review lacks verified upstream evidence"}
    if review.approval_status == "NOT_APPLICABLE" or review.final_risk_assessment == "N/A":
        raise ValueError("Major review cannot be NOT_APPLICABLE or N/A")
    if violation and review.approval_status != "REJECTED":
        raise ValueError("Confirmed violation cannot be approved")
    allowed_advice = {advice for stage in stages.values() for advice in stage.recommendations}
    if not set(review.recommendations).issubset(allowed_advice):
        raise ValueError("Review recommendations must come from earlier stages")
    if review.approval_status == "CONDITIONAL_APPROVAL" and not review.recommendations:
        raise ValueError("Conditional approval requires upstream conditions")
    return {"action": "FINAL", "skills": ["final"], "approval_status": review.approval_status}


def validate_final(value):
    state = validate_state(value["state"])
    result = FinalResult.model_validate(value["final_result"])
    decision = gate(state.model_dump())
    if decision["action"] == "FINAL_SKIP":
        if result.approval_status != "NOT_APPLICABLE" or result.final_risk_assessment != "N/A":
            raise ValueError("Nonmajor output must be NOT_APPLICABLE and N/A")
        if not set(result.recommendations).issubset(state.stages["identification"].recommendations):
            raise ValueError("Skip output cannot introduce recommendations")
    elif decision["action"] == "FINAL":
        review = state.review
        for key in ("approval_status", "final_risk_assessment", "key_findings", "recommendations"):
            if getattr(result, key) != getattr(review, key):
                raise ValueError(f"Final changed upstream {key}")
        for heading in ("合法合规性审核意见书", "一、决策事项基本情况", "二、主要依据", "三、法律分析", "四、审核结论", "五、建议"):
            if heading not in result.compliance_summary:
                raise ValueError(f"Missing report heading: {heading}")
    else:
        raise ValueError("Gate does not authorize a final result")
    return {"valid": True, "semantic_review_required": True}


def resume(value):
    state = validate_state(value["state"])
    if gate(state.model_dump())["action"] != "SUPPLEMENT":
        raise ValueError("Only SUPPLEMENT state can resume")
    addition = value["addition"]
    if set(addition) - {"input_text", "file_list", "evidence"}:
        raise ValueError("Unsupported addition fields")
    text = addition.get("input_text", "").strip()
    files = [name for name in addition.get("file_list", []) if name not in state.file_list]
    existing = {item.id: item.model_dump() for item in state.evidence}
    evidence = []
    for raw in addition.get("evidence", []):
        item = Evidence.model_validate(raw).model_dump()
        if item["id"] in existing and existing[item["id"]] != item:
            raise ValueError("Evidence is immutable; use a new ID for a revision")
        if item["id"] not in existing:
            evidence.append(item)
            existing[item["id"]] = item
    if not files and not evidence and (not text or text in state.input_text):
        raise ValueError("No new information; remain waiting without rerunning")
    updated = deepcopy(state.model_dump())
    updated["version"] += 1
    updated["supplement_round"] += 1
    if text and text not in state.input_text:
        updated["input_text"] += "\n\n" + text
    updated["file_list"].extend(files)
    updated["evidence"].extend(evidence)
    updated["stages"] = {}
    updated["review"] = None
    updated["final_result"] = None
    return {"state": validate_state(updated).model_dump(), "invalidated": list(STAGES), "next": {"action": "RUN_SKILLS", "skills": ["smjy"]}}