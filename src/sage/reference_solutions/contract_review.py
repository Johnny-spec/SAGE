"""Deterministic contract-review reference Skill handler."""

from collections.abc import Mapping
from typing import Any

from sage.domain import RuntimeContext
from sage.errors import ExecutionError

_DEFAULT_CHECKS = (
    ("parties", ("甲方", "乙方"), "The contract should identify both parties."),
    ("payment", ("付款", "支付"), "The contract should define payment obligations."),
    ("delivery", ("交付", "履行"), "The contract should define delivery or performance."),
    ("breach", ("违约",), "The contract should define breach liability."),
    ("dispute", ("争议", "管辖", "仲裁"), "The contract should define dispute resolution."),
)


def review_contract(
    arguments: Mapping[str, Any],
    context: RuntimeContext,
) -> dict[str, Any]:
    del context
    text = str(arguments.get("contract_text") or "").strip()
    party_role = str(arguments.get("party_role") or "neutral")
    if not text:
        raise ExecutionError("contract-review requires contract_text")
    issues: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for check_id, keywords, recommendation in _DEFAULT_CHECKS:
        matches = [(keyword, text.find(keyword)) for keyword in keywords if keyword in text]
        if not matches:
            issues.append(
                {
                    "check_id": check_id,
                    "status": "missing",
                    "severity": "high" if check_id in {"parties", "breach"} else "medium",
                    "recommendation": recommendation,
                }
            )
            continue
        keyword, position = min(matches, key=lambda item: item[1])
        citation_id = f"cit_{check_id}"
        citations.append(
            {
                "citation_id": citation_id,
                "locator": {"kind": "character-offset", "start": position},
                "quote": _excerpt(text, position, len(keyword)),
            }
        )
        issues.append(
            {
                "check_id": check_id,
                "status": "present-needs-review",
                "severity": "info",
                "citation_refs": [citation_id],
                "recommendation": "A legal reviewer must assess wording and applicability.",
            }
        )
    missing = sum(issue["status"] == "missing" for issue in issues)
    return {
        "status": "needs-review" if missing else "draft-complete",
        "party_role": party_role,
        "issues": issues,
        "citations": citations,
        "disclaimer": "This is a review draft and not a final legal determination.",
    }


def _excerpt(text: str, start: int, length: int) -> str:
    excerpt_start = max(0, start - 60)
    excerpt_end = min(len(text), start + length + 160)
    return text[excerpt_start:excerpt_end].strip()
