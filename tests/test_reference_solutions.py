from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(DEFAULT_SKILLS_ROOT, agent_model=DemoAgentModel())) as value:
        yield value


def test_enterprise_qa_returns_only_supplied_document_citations(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/invocations",
        json={
            "capability_id": "skill:enterprise-qa",
            "input": {
                "query": "What is the retention policy?",
                "documents": [
                    {
                        "id": "policy-1",
                        "title": "Retention Policy",
                        "text": "The retention policy keeps audit records for seven years.",
                    },
                    {
                        "id": "unrelated",
                        "title": "Travel",
                        "text": "Travel requests require manager approval.",
                    },
                ],
            },
        },
    )

    assert response.status_code == 200
    output = response.json()["output"]
    assert output["status"] == "answered"
    assert [item["document_id"] for item in output["citations"]] == ["policy-1"]


def test_news_intelligence_deduplicates_tracking_urls(client: TestClient) -> None:
    response = client.post(
        "/v1/invocations",
        json={
            "capability_id": "skill:news-intelligence",
            "input": {
                "articles": [
                    {
                        "title": "Launch",
                        "url": "https://news.example/item?utm_source=a",
                        "source": "wire-a",
                        "published_at": "2026-09-08T09:00:00+00:00",
                    },
                    {
                        "title": "Launch copied",
                        "url": "https://news.example/item?utm_source=b",
                        "source": "wire-b",
                        "published_at": "2026-09-08T10:00:00+00:00",
                    },
                ]
            },
        },
    )

    assert response.status_code == 200
    output = response.json()["output"]
    assert output["story_count"] == 1
    assert output["stories"][0]["source"] == "wire-b"
    assert output["stories"][0]["url"] == "https://news.example/item"


def test_contract_review_returns_draft_and_never_final_legal_determination(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/invocations",
        json={
            "capability_id": "skill:contract-review",
            "input": {
                "contract_text": "甲方向乙方交付服务，乙方应付款。",
                "party_role": "seller",
            },
        },
    )

    assert response.status_code == 200
    output = response.json()["output"]
    assert output["status"] == "needs-review"
    assert any(item["check_id"] == "breach" for item in output["issues"])
    assert "not a final legal determination" in output["disclaimer"]