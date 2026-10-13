from pathlib import Path

import pytest

from sage.digests import canonical_digest
from sage.feedback import (
    EvaluationSampleCandidateInput,
    FeedbackAuthorizationError,
    FeedbackConflictError,
    FeedbackDispositionInput,
    FeedbackInput,
    SqliteFeedbackStore,
    new_evaluation_sample_candidate,
    new_feedback_disposition,
    new_feedback_record,
)
from sage.results import ResultBlock, ResultView


def _result() -> ResultView:
    block = ResultBlock(
        block_id="answer",
        type="markdown",
        content={"markdown": "Original answer"},
        content_digest="sha256:" + "1" * 64,
    )
    return ResultView(
        run_id="run_1",
        thread_id="thread_1",
        generation_id="gen_1",
        solution_id="solution-1",
        solution_release_id="release-1",
        capability_id="skill:answer",
        input_digest=canonical_digest({"message": "test"}),
        blocks=(block,),
        result_digest="sha256:" + "2" * 64,
    )


def test_feedback_store_replays_same_request_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "feedback.sqlite3"
    payload = FeedbackInput(rating=4, categories=("incomplete",))
    record = new_feedback_record(
        workspace_id="workspace-1",
        principal_id="user-1",
        result=_result(),
        payload=payload,
    )
    stored, created = SqliteFeedbackStore(str(database)).create(
        record,
        idempotency_key="feedback-1",
        request_digest="sha256:request-1",
    )
    replayed, replay_created = SqliteFeedbackStore(str(database)).create(
        new_feedback_record(
            workspace_id="workspace-1",
            principal_id="user-1",
            result=_result(),
            payload=payload,
        ),
        idempotency_key="feedback-1",
        request_digest="sha256:request-1",
    )

    assert created is True
    assert replay_created is False
    assert replayed == stored
    assert SqliteFeedbackStore(str(database)).get(stored.feedback_id) == stored


def test_feedback_store_rejects_changed_idempotent_request(tmp_path: Path) -> None:
    store = SqliteFeedbackStore(str(tmp_path / "feedback.sqlite3"))
    payload = FeedbackInput(rating=4)
    record = new_feedback_record(
        workspace_id="workspace-1",
        principal_id="user-1",
        result=_result(),
        payload=payload,
    )
    store.create(
        record,
        idempotency_key="feedback-1",
        request_digest="sha256:request-1",
    )

    with pytest.raises(FeedbackConflictError, match="different feedback"):
        store.create(
            record,
            idempotency_key="feedback-1",
            request_digest="sha256:request-2",
        )


def test_feedback_store_appends_dispositions_without_mutating_feedback(tmp_path: Path) -> None:
    database = tmp_path / "feedback.sqlite3"
    store = SqliteFeedbackStore(str(database))
    feedback = new_feedback_record(
        workspace_id="workspace-1",
        principal_id="user-1",
        result=_result(),
        payload=FeedbackInput(rating=2, categories=("incorrect",)),
    )
    store.create(
        feedback,
        idempotency_key="feedback-1",
        request_digest="sha256:request-1",
    )
    disposition = new_feedback_disposition(
        feedback,
        FeedbackDispositionInput(action="needs-human-review", reason="Verify the answer"),
        actor_id="admin-1",
    )

    store.append_disposition(disposition)
    reopened = SqliteFeedbackStore(str(database))

    assert reopened.get(feedback.feedback_id) == feedback
    assert reopened.list_dispositions(
        feedback.feedback_id,
        workspace_id="workspace-1",
    ) == (disposition,)


def test_feedback_store_freezes_one_idempotent_evaluation_candidate(tmp_path: Path) -> None:
    database = tmp_path / "feedback.sqlite3"
    store = SqliteFeedbackStore(str(database))
    feedback = new_feedback_record(
        workspace_id="workspace-1",
        principal_id="user-1",
        result=_result(),
        payload=FeedbackInput(
            rating=2,
            categories=("incorrect",),
            evaluation_use_allowed=True,
        ),
    )
    store.create(
        feedback,
        idempotency_key="feedback-1",
        request_digest="sha256:request-1",
    )
    payload = EvaluationSampleCandidateInput(
        rubric="The answer must contain the corrected value.",
        expected_result={"answer": "Corrected"},
        tags=("regression",),
    )
    candidate = new_evaluation_sample_candidate(
        feedback,
        payload,
        actor_id="admin-1",
        input_data={"message": "test"},
    )
    disposition = new_feedback_disposition(
        feedback,
        FeedbackDispositionInput(
            action="accepted-as-evaluation-candidate",
            reason="Approved for regression coverage",
        ),
        actor_id="admin-1",
    )

    created, was_created = store.create_evaluation_candidate(
        candidate,
        disposition,
        request_digest="sha256:candidate-request",
    )
    replayed, replay_created = SqliteFeedbackStore(str(database)).create_evaluation_candidate(
        new_evaluation_sample_candidate(
            feedback,
            payload,
            actor_id="admin-1",
            input_data={"message": "test"},
        ),
        new_feedback_disposition(
            feedback,
            FeedbackDispositionInput(
                action="accepted-as-evaluation-candidate",
                reason="Approved for regression coverage",
            ),
            actor_id="admin-1",
        ),
        request_digest="sha256:candidate-request",
    )

    assert was_created is True
    assert replay_created is False
    assert replayed == created
    assert store.list_evaluation_candidates("workspace-1") == (created,)
    assert len(store.list_dispositions(feedback.feedback_id, workspace_id="workspace-1")) == 1


def test_evaluation_candidate_requires_explicit_feedback_authorization() -> None:
    feedback = new_feedback_record(
        workspace_id="workspace-1",
        principal_id="user-1",
        result=_result(),
        payload=FeedbackInput(rating=2),
    )

    with pytest.raises(FeedbackAuthorizationError, match="evaluation reuse"):
        new_evaluation_sample_candidate(
            feedback,
            EvaluationSampleCandidateInput(rubric="Check correctness"),
            actor_id="admin-1",
            input_data={"message": "test"},
        )


def test_feedback_rejects_evaluation_without_quality_consent() -> None:
    with pytest.raises(ValueError, match="quality operations consent"):
        FeedbackInput(
            rating=2,
            quality_use_allowed=False,
            evaluation_use_allowed=True,
        )