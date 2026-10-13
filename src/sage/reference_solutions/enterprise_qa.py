"""Deterministic enterprise-QA reference Skill handler."""

import re
from collections.abc import Mapping
from typing import Any

from sage.domain import RuntimeContext
from sage.errors import ExecutionError


def answer_enterprise_question(
    arguments: Mapping[str, Any],
    context: RuntimeContext,
) -> dict[str, Any]:
    del context
    query = str(arguments.get("query") or "").strip()
    documents = arguments.get("documents")
    if not query or not isinstance(documents, list):
        raise ExecutionError("enterprise-qa requires query and documents")
    terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if term}
    ranked: list[tuple[int, str, str, str]] = []
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        document_id = str(document.get("id") or "").strip()
        title = str(document.get("title") or document_id).strip()
        text = str(document.get("text") or "").strip()
        if not document_id or not text:
            continue
        normalized = text.casefold()
        score = sum(normalized.count(term) for term in terms)
        if score:
            ranked.append((score, document_id, title, text))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    matches = ranked[:3]
    if not matches:
        return {
            "status": "insufficient-evidence",
            "answer": "No supplied document contains enough evidence to answer the query.",
            "citations": [],
        }
    citations = [
        {
            "document_id": document_id,
            "title": title,
            "quote": _matching_excerpt(text, terms),
        }
        for _, document_id, title, text in matches
    ]
    return {
        "status": "answered",
        "answer": " ".join(citation["quote"] for citation in citations),
        "citations": citations,
    }


def _matching_excerpt(text: str, terms: set[str]) -> str:
    normalized = text.casefold()
    positions = [normalized.find(term) for term in terms if normalized.find(term) >= 0]
    start = max(0, min(positions, default=0) - 80)
    end = min(len(text), start + 320)
    return text[start:end].strip()
