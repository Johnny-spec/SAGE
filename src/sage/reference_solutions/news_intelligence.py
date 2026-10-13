"""Deterministic news-intelligence reference Skill handler."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sage.domain import RuntimeContext
from sage.errors import ExecutionError

_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def analyze_news(
    arguments: Mapping[str, Any],
    context: RuntimeContext,
) -> dict[str, Any]:
    del context
    articles = arguments.get("articles")
    limit = int(arguments.get("limit", 10))
    if not isinstance(articles, list):
        raise ExecutionError("news-intelligence requires an articles array")
    unique: dict[str, dict[str, str]] = {}
    for raw in articles:
        if not isinstance(raw, Mapping):
            continue
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not url:
            continue
        canonical_url = _canonical_url(url)
        key = canonical_url or title.casefold()
        candidate = {
            "title": title,
            "url": canonical_url,
            "source": str(raw.get("source") or "unknown"),
            "published_at": str(raw.get("published_at") or ""),
            "summary": str(raw.get("summary") or "").strip(),
        }
        existing = unique.get(key)
        if existing is None or _timestamp(candidate) > _timestamp(existing):
            unique[key] = candidate
    stories = sorted(
        unique.values(),
        key=lambda item: (_timestamp(item), item["title"]),
        reverse=True,
    )[:limit]
    return {
        "status": "completed",
        "story_count": len(stories),
        "stories": [
            {
                **story,
                "citation": {
                    "source_uri": story["url"],
                    "captured_from": story["source"],
                },
            }
            for story in stories
        ],
    }


def _canonical_url(value: str) -> str:
    parts = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path, urlencode(query), "")
    )


def _timestamp(article: Mapping[str, str]) -> datetime:
    try:
        return datetime.fromisoformat(article.get("published_at", "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min
