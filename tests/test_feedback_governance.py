from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import DEFAULT_SKILLS_ROOT, create_app
from sage.control_settings import ControlPlaneSettings
from sage.domain import Principal


class SwitchingPrincipalResolver:
    def __init__(self) -> None:
        self.principal = Principal(
            workspace_id="ws_default",
            principal_id="user-a",
            roles=frozenset({"user"}),
        )

    def resolve(self, authorization: str | None) -> Principal:
        del authorization
        return self.principal


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as test_client:
        yield test_client


def _completed_run(client: TestClient, *, solution_id: str | None = None) -> str:
    response = client.post(
        "/v1/messages",
        json={
            "message": "ignored",
            "skill": "echo",
            "arguments": {"text": "review this result"},
            "solution_id": solution_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def _feedback(
    client: TestClient,
    run_id: str,
    payload: dict[str, object],
    *,
    key: str,
) -> dict[str, object]:
    response = client.post(
        f"/v1/runs/{run_id}/feedback",
        headers={"Idempotency-Key": key},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish_echo_solution(client: TestClient) -> str:
    solution_id = "feedback-memory"
    created = client.post(
        "/v1/admin/solutions",
        json={
            "solution_id": solution_id,
            "title": "Feedback memory",
            "owner": "tests",
            "supported_profiles": ["direct"],
        },
    )
    assert created.status_code == 201, created.text
    active = client.app.state.runtime_manager.active.generation
    release = client.post(
        f"/v1/admin/solutions/{solution_id}/releases",
        json={
            "version": "1.0.0",
            "components": [
                {
                    "kind": "skill",
                    "component_id": "echo",
                    "version": active.skill_versions["echo"],
                    "content_digest": active.skill_digests["echo"],
                }
            ],
            "supported_profiles": ["direct"],
        },
    )
    assert release.status_code == 201, release.text
    candidate = client.post(
        "/v1/admin/candidates",
        json={
            "title": "Publish feedback memory",
            "solution_release_ids": [release.json()["release_id"]],
        },
    )
    assert candidate.status_code == 201, candidate.text
    published = client.post(
        f"/v1/admin/candidates/{candidate.json()['candidate_id']}/publish"
    )
    assert published.status_code == 200, published.text
    return solution_id


def test_admin_triages_and_freezes_authorized_feedback(client: TestClient) -> None:
    run_id = _completed_run(client)
    feedback = _feedback(
        client,
        run_id,
        {
            "rating": 2,
            "categories": ["incorrect"],
            "comment": "The answer is wrong.",
            "evaluation_use_allowed": True,
        },
        key="feedback-governance-1",
    )

    listed = client.get("/v1/admin/feedback")
    disposition = client.post(
        f"/v1/admin/feedback/{feedback['feedback_id']}/dispositions",
        json={"action": "needs-human-review", "reason": "Verify the expected answer"},
    )
    sample_payload = {
        "rubric": "The exact corrected answer must be returned.",
        "expected_result": {"text": "corrected answer"},
        "tags": ["correctness", "regression"],
    }
    sample = client.post(
        f"/v1/admin/feedback/{feedback['feedback_id']}/evaluation-candidates",
        json=sample_payload,
    )
    replay = client.post(
        f"/v1/admin/feedback/{feedback['feedback_id']}/evaluation-candidates",
        json=sample_payload,
    )
    changed = client.post(
        f"/v1/admin/feedback/{feedback['feedback_id']}/evaluation-candidates",
        json={**sample_payload, "rubric": "A different rubric"},
    )
    dispositions = client.get(
        f"/v1/admin/feedback/{feedback['feedback_id']}/dispositions"
    )
    samples = client.get("/v1/admin/evaluation-sample-candidates")
    sample_set_payload = {
        "version": "1.0.0",
        "title": "Echo feedback regression",
        "description": "User-authorized correctness examples.",
        "candidate_ids": [sample.json()["candidate_id"]],
    }
    sample_set_without_key = client.post(
        "/v1/admin/evaluation-sample-sets",
        json=sample_set_payload,
    )
    sample_set_missing_candidate = client.post(
        "/v1/admin/evaluation-sample-sets",
        headers={"Idempotency-Key": "missing-sample"},
        json={**sample_set_payload, "version": "2.0.0", "candidate_ids": ["esc_missing"]},
    )
    sample_set = client.post(
        "/v1/admin/evaluation-sample-sets",
        headers={"Idempotency-Key": "sample-set-1"},
        json=sample_set_payload,
    )
    sample_set_replay = client.post(
        "/v1/admin/evaluation-sample-sets",
        headers={"Idempotency-Key": "sample-set-1"},
        json=sample_set_payload,
    )
    sample_set_changed = client.post(
        "/v1/admin/evaluation-sample-sets",
        headers={"Idempotency-Key": "sample-set-2"},
        json={**sample_set_payload, "title": "Changed title"},
    )
    comparison_payload = {
        "baseline_target_digest": "sha256:" + "a" * 64,
        "candidate_target_digest": "sha256:" + "b" * 64,
        "evaluator_id": "human-review",
        "evaluator_version": "1.0.0",
        "outcomes": [
            {
                "candidate_id": sample.json()["candidate_id"],
                "baseline_score": 0.25,
                "candidate_score": 0.75,
                "baseline_result_digest": sample.json()["result_digest"],
                "candidate_result_digest": "sha256:" + "c" * 64,
                "notes": "Candidate returned the corrected answer.",
            }
        ],
    }
    comparison = client.post(
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}/comparisons",
        headers={"Idempotency-Key": "comparison-1"},
        json=comparison_payload,
    )
    comparison_replay = client.post(
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}/comparisons",
        headers={"Idempotency-Key": "comparison-1"},
        json=comparison_payload,
    )
    comparison_changed = client.post(
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}/comparisons",
        headers={"Idempotency-Key": "comparison-1"},
        json={
            **comparison_payload,
            "candidate_target_digest": "sha256:" + "d" * 64,
        },
    )
    sample_sets = client.get("/v1/admin/evaluation-sample-sets")
    sample_set_detail = client.get(
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}"
    )
    comparisons = client.get(
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}/comparisons"
    )
    quality = client.get("/v1/admin/feedback/quality-summary")
    audits = client.get("/v1/admin/audits").json()

    assert listed.status_code == 200
    assert [item["feedback_id"] for item in listed.json()] == [feedback["feedback_id"]]
    assert disposition.status_code == 201
    assert disposition.json()["action"] == "needs-human-review"
    assert sample.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == sample.json()
    assert changed.status_code == 409
    assert sample.json()["feedback_digest"] == feedback["record_digest"]
    assert sample.json()["input_digest"] == feedback["input_digest"]
    assert sample.json()["input_data"]["request"]["arguments"] == {
        "text": "review this result"
    }
    assert sample.json()["result_digest"] == feedback["result_digest"]
    assert [item["action"] for item in dispositions.json()] == [
        "needs-human-review",
        "accepted-as-evaluation-candidate",
    ]
    assert [item["candidate_id"] for item in samples.json()] == [
        sample.json()["candidate_id"]
    ]
    assert sample_set.status_code == 201
    assert sample_set_without_key.status_code == 422
    assert sample_set_missing_candidate.status_code == 404
    assert sample_set_replay.status_code == 200
    assert sample_set_replay.json() == sample_set.json()
    assert sample_set_changed.status_code == 409
    assert sample_set.json()["target_id"] == "skill:echo"
    assert sample_set.json()["samples"][0]["candidate_digest"] == sample.json()[
        "content_digest"
    ]
    assert sample_set.json()["samples"][0]["input_data"] == sample.json()["input_data"]
    assert sample_set.json()["samples"][0]["expected_result"] == {
        "text": "corrected answer"
    }
    assert comparison.status_code == 201
    assert comparison_replay.status_code == 200
    assert comparison_replay.json() == comparison.json()
    assert comparison_changed.status_code == 409
    assert comparison.json()["execution_mode"] == "imported"
    assert comparison.json()["status"] == "improved"
    assert comparison.json()["metrics"] == {
        "sample_count": 1,
        "baseline_average": 0.25,
        "candidate_average": 0.75,
        "score_delta": 0.5,
        "improved_count": 1,
        "regressed_count": 0,
        "unchanged_count": 0,
    }
    assert [item["sample_set_id"] for item in sample_sets.json()] == [
        sample_set.json()["sample_set_id"]
    ]
    assert sample_set_detail.json() == sample_set.json()
    assert [item["comparison_id"] for item in comparisons.json()] == [
        comparison.json()["comparison_id"]
    ]
    assert quality.status_code == 200
    assert quality.json()["overall"] == {
        "feedback_count": 1,
        "rated_count": 1,
        "average_rating": 2.0,
        "negative_rating_count": 1,
        "evaluation_candidate_count": 1,
        "category_counts": {"incorrect": 1},
    }
    assert quality.json()["by_capability"][0]["key"] == "skill:echo"
    audited_resources = {item["resource"] for item in audits}
    assert f"/v1/admin/feedback/{feedback['feedback_id']}/dispositions" in audited_resources
    assert (
        f"/v1/admin/feedback/{feedback['feedback_id']}/evaluation-candidates"
        in audited_resources
    )
    assert "/v1/admin/evaluation-sample-sets" in audited_resources
    assert (
        f"/v1/admin/evaluation-sample-sets/{sample_set.json()['sample_set_id']}/comparisons"
        in audited_resources
    )


def test_admin_respects_quality_and_evaluation_consent(client: TestClient) -> None:
    hidden = _feedback(
        client,
        _completed_run(client),
        {
            "rating": 2,
            "comment": "Do not expose this to quality operations.",
            "quality_use_allowed": False,
        },
        key="feedback-hidden",
    )
    visible = _feedback(
        client,
        _completed_run(client),
        {
            "rating": 3,
            "comment": "Operational review only.",
            "evaluation_use_allowed": False,
        },
        key="feedback-no-evaluation",
    )

    listed = client.get("/v1/admin/feedback")
    quality = client.get("/v1/admin/feedback/quality-summary")
    hidden_disposition = client.post(
        f"/v1/admin/feedback/{hidden['feedback_id']}/dispositions",
        json={"action": "acknowledged", "reason": "Not allowed"},
    )
    sample = client.post(
        f"/v1/admin/feedback/{visible['feedback_id']}/evaluation-candidates",
        json={"rubric": "Should remain unauthorized"},
    )

    assert [item["feedback_id"] for item in listed.json()] == [visible["feedback_id"]]
    assert quality.json()["overall"]["feedback_count"] == 1
    assert quality.json()["overall"]["average_rating"] == 3.0
    assert hidden_disposition.status_code == 404
    assert sample.status_code == 409
    assert "not authorized for evaluation reuse" in sample.json()["detail"]


def test_memory_proposal_requires_confirmation_and_never_overwrites(client: TestClient) -> None:
    solution_id = _publish_echo_solution(client)
    first_feedback = _feedback(
        client,
        _completed_run(client, solution_id=solution_id),
        {
            "expected_action": "propose-memory",
            "memory_proposal": {
                "key": "answer-style",
                "value": "Use concise answers",
            },
        },
        key="memory-proposal-1",
    )

    before = client.get("/v1/memories", params={"solution_id": solution_id})
    confirmed = client.post(
        f"/v1/feedback/{first_feedback['feedback_id']}/memory/confirm"
    )
    replay = client.post(
        f"/v1/feedback/{first_feedback['feedback_id']}/memory/confirm"
    )
    after = client.get("/v1/memories", params={"solution_id": solution_id})

    assert before.json() == []
    assert confirmed.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == confirmed.json()
    assert [(item["key"], item["value"]) for item in after.json()] == [
        ("answer-style", "Use concise answers")
    ]

    conflicting_feedback = _feedback(
        client,
        _completed_run(client, solution_id=solution_id),
        {
            "expected_action": "propose-memory",
            "memory_proposal": {
                "key": "answer-style",
                "value": "Use detailed answers",
            },
        },
        key="memory-proposal-2",
    )
    conflict = client.post(
        f"/v1/feedback/{conflicting_feedback['feedback_id']}/memory/confirm"
    )

    assert conflict.status_code == 409
    assert "different user memory" in conflict.json()["detail"]
    unchanged = client.get("/v1/memories", params={"solution_id": solution_id}).json()
    assert [(item["key"], item["value"]) for item in unchanged] == [
        ("answer-style", "Use concise answers")
    ]


def test_feedback_detail_and_memory_confirmation_are_owner_only(tmp_path: Path) -> None:
    resolver = SwitchingPrincipalResolver()
    app = create_app(
        DEFAULT_SKILLS_ROOT,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "owner-state"),
        principal_resolver=resolver,
    )
    with TestClient(app) as owner_client:
        run_id = _completed_run(owner_client)
        feedback = _feedback(
            owner_client,
            run_id,
            {"rating": 4, "comment": "Owner feedback"},
            key="owner-feedback",
        )
        assert owner_client.get(f"/v1/feedback/{feedback['feedback_id']}").status_code == 200

        resolver.principal = Principal(
            workspace_id="ws_default",
            principal_id="user-b",
            roles=frozenset({"user"}),
        )
        detail = owner_client.get(f"/v1/feedback/{feedback['feedback_id']}")
        confirmation = owner_client.post(
            f"/v1/feedback/{feedback['feedback_id']}/memory/confirm"
        )

    assert detail.status_code == 404
    assert confirmation.status_code == 404