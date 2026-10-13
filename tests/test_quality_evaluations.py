from pathlib import Path

import pytest

from sage.digests import canonical_digest
from sage.feedback import EvaluationSampleCandidateRecord
from sage.quality_evaluations import (
    EvaluationComparisonInput,
    EvaluationComparisonOutcomeInput,
    EvaluationSampleSetInput,
    QualityEvaluationConflictError,
    QualityEvaluationError,
    SqliteQualityEvaluationStore,
)


def _candidate(candidate_id: str, *, target: str = "skill:echo") -> EvaluationSampleCandidateRecord:
    values = {
        "candidate_id": candidate_id,
        "feedback_id": f"fb_{candidate_id}",
        "feedback_digest": "sha256:" + "1" * 64,
        "workspace_id": "ws_quality",
        "run_id": f"run_{candidate_id}",
        "input_data": {"kind": "message", "request": {"message": candidate_id}},
        "input_digest": canonical_digest(
            {"kind": "message", "request": {"message": candidate_id}}
        ),
        "result_digest": "sha256:" + "2" * 64,
        "generation_id": "gen_1",
        "solution_id": "solution-1",
        "solution_release_id": "solution-1@1.0.0",
        "capability_id": target,
        "rubric": "Return the expected answer.",
        "expected_result": {"text": f"expected-{candidate_id}"},
        "tags": ("regression",),
        "actor_id": "admin-1",
        "created_at": "2026-09-11T00:00:00+00:00",
    }
    return EvaluationSampleCandidateRecord(
        **values,
        content_digest=canonical_digest(values),
    )


def _comparison(*candidate_ids: str) -> EvaluationComparisonInput:
    return EvaluationComparisonInput(
        baseline_target_digest="sha256:" + "a" * 64,
        candidate_target_digest="sha256:" + "b" * 64,
        evaluator_id="human-review",
        evaluator_version="1.0.0",
        outcomes=tuple(
            EvaluationComparisonOutcomeInput(
                candidate_id=candidate_id,
                baseline_score=0.4 if index == 0 else 0.8,
                candidate_score=0.9 if index == 0 else 0.7,
                baseline_result_digest="sha256:" + str(index + 3) * 64,
                candidate_result_digest="sha256:" + str(index + 5) * 64,
            )
            for index, candidate_id in enumerate(candidate_ids)
        ),
    )


def test_versioned_sample_set_and_comparison_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "quality.sqlite3"
    store = SqliteQualityEvaluationStore(str(database))
    candidates = (_candidate("esc_a"), _candidate("esc_b"))
    payload = EvaluationSampleSetInput(
        version="1.0.0",
        title="Echo regression",
        candidate_ids=("esc_b", "esc_a"),
    )

    sample_set, created = store.create_sample_set(
        payload,
        candidates,
        workspace_id="ws_quality",
        actor_id="admin-1",
        idempotency_key="sample-set-1",
    )
    comparison, compared = store.create_comparison(
        sample_set,
        _comparison("esc_a", "esc_b"),
        actor_id="admin-1",
        idempotency_key="comparison-1",
    )
    reopened = SqliteQualityEvaluationStore(str(database))
    replayed_set, replay_created = reopened.create_sample_set(
        payload,
        candidates,
        workspace_id="ws_quality",
        actor_id="admin-1",
        idempotency_key="sample-set-1",
    )
    replayed_comparison, replay_compared = reopened.create_comparison(
        sample_set,
        _comparison("esc_a", "esc_b"),
        actor_id="admin-1",
        idempotency_key="comparison-1",
    )

    assert created is True
    assert compared is True
    assert replay_created is False
    assert replay_compared is False
    assert replayed_set == sample_set
    assert replayed_comparison == comparison
    assert [sample.candidate_id for sample in sample_set.samples] == ["esc_a", "esc_b"]
    assert sample_set.samples[0].input_data["request"] == {"message": "esc_a"}
    assert sample_set.samples[0].expected_result == {"text": "expected-esc_a"}
    assert sample_set.samples[0].source_generation_id == "gen_1"
    assert sample_set.samples[0].source_solution_release_id == "solution-1@1.0.0"
    assert comparison.metrics.model_dump() == {
        "sample_count": 2,
        "baseline_average": 0.6,
        "candidate_average": 0.8,
        "score_delta": 0.2,
        "improved_count": 1,
        "regressed_count": 1,
        "unchanged_count": 0,
    }
    assert comparison.status == "improved"
    assert reopened.list_sample_sets("ws_quality") == (sample_set,)
    assert reopened.list_comparisons(
        sample_set.sample_set_id,
        workspace_id="ws_quality",
    ) == (comparison,)


def test_sample_set_rejects_mixed_targets_and_version_drift(tmp_path: Path) -> None:
    store = SqliteQualityEvaluationStore(str(tmp_path / "quality.sqlite3"))
    payload = EvaluationSampleSetInput(
        version="1.0.0",
        title="Regression",
        candidate_ids=("esc_a", "esc_b"),
    )
    with pytest.raises(QualityEvaluationError, match="one Solution and one target"):
        store.create_sample_set(
            payload,
            (_candidate("esc_a"), _candidate("esc_b", target="skill:other")),
            workspace_id="ws_quality",
            actor_id="admin-1",
            idempotency_key="mixed",
        )

    store.create_sample_set(
        payload,
        (_candidate("esc_a"), _candidate("esc_b")),
        workspace_id="ws_quality",
        actor_id="admin-1",
        idempotency_key="set-1",
    )
    with pytest.raises(QualityEvaluationConflictError, match="different content"):
        store.create_sample_set(
            payload.model_copy(update={"title": "Changed"}),
            (_candidate("esc_a"), _candidate("esc_b")),
            workspace_id="ws_quality",
            actor_id="admin-1",
            idempotency_key="set-2",
        )


def test_comparison_rejects_partial_samples_and_changed_idempotency(tmp_path: Path) -> None:
    store = SqliteQualityEvaluationStore(str(tmp_path / "quality.sqlite3"))
    candidates = (_candidate("esc_a"), _candidate("esc_b"))
    sample_set, _ = store.create_sample_set(
        EvaluationSampleSetInput(
            version="1.0.0",
            title="Regression",
            candidate_ids=("esc_a", "esc_b"),
        ),
        candidates,
        workspace_id="ws_quality",
        actor_id="admin-1",
        idempotency_key="set-1",
    )

    with pytest.raises(QualityEvaluationError, match="every sample"):
        store.create_comparison(
            sample_set,
            _comparison("esc_a"),
            actor_id="admin-1",
            idempotency_key="comparison-partial",
        )

    store.create_comparison(
        sample_set,
        _comparison("esc_a", "esc_b"),
        actor_id="admin-1",
        idempotency_key="comparison-1",
    )
    changed = _comparison("esc_a", "esc_b").model_copy(
        update={"candidate_target_digest": "sha256:" + "c" * 64}
    )
    with pytest.raises(QualityEvaluationConflictError, match="different comparison"):
        store.create_comparison(
            sample_set,
            changed,
            actor_id="admin-1",
            idempotency_key="comparison-1",
        )