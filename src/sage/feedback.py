"""Immutable user feedback bound to versioned Run result projections."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sage.digests import canonical_digest
from sage.results import ResultView

FeedbackCategory = Literal[
    "incorrect",
    "incomplete",
    "irrelevant",
    "citation-issue",
    "format",
    "tool-failure",
    "latency",
    "other",
]
FeedbackExpectedAction = Literal[
    "none",
    "regenerate",
    "human-review",
    "propose-memory",
    "evaluation-candidate",
]
FeedbackDispositionAction = Literal[
    "acknowledged",
    "rejected",
    "needs-human-review",
    "accepted-as-evaluation-candidate",
    "proposed-revision",
    "resolved",
]


class FeedbackConflictError(ValueError):
    """Raised when a feedback idempotency key is reused with different content."""


class FeedbackTargetError(ValueError):
    """Raised when feedback targets a missing or changed result block."""


class FeedbackAuthorizationError(ValueError):
    """Raised when feedback reuse was not authorized by its owner."""


class FeedbackAnnotationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(min_length=1, max_length=100)
    block_revision: int = Field(ge=1)
    target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    comment: str = Field(min_length=1, max_length=4000)
    proposed_replacement: str | None = Field(default=None, max_length=20_000)


class FeedbackAnnotation(FeedbackAnnotationInput):
    annotation_id: str = Field(default_factory=lambda: f"fba_{uuid4().hex}")


class FeedbackMemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)
    sensitivity: Literal["normal", "sensitive"] = "normal"
    expires_at: datetime | None = None

    @field_validator("key", "value")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Memory proposal key and value must not be blank")
        return normalized


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rating: int | None = Field(default=None, ge=1, le=5)
    categories: tuple[FeedbackCategory, ...] = Field(default=(), max_length=8)
    comment: str | None = Field(default=None, max_length=4000)
    annotations: tuple[FeedbackAnnotationInput, ...] = Field(default=(), max_length=50)
    expected_action: FeedbackExpectedAction = "none"
    quality_use_allowed: bool = True
    evaluation_use_allowed: bool = False
    memory_proposal: FeedbackMemoryProposal | None = None

    @model_validator(mode="after")
    def require_feedback_content(self) -> "FeedbackInput":
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("Feedback categories must be unique")
        if self.comment is not None and not self.comment.strip():
            raise ValueError("Feedback comment cannot be blank")
        if (
            self.rating is None
            and not self.categories
            and self.comment is None
            and not self.annotations
            and self.memory_proposal is None
        ):
            raise ValueError("Feedback requires a rating, category, comment, or annotation")
        if self.expected_action == "propose-memory" and self.memory_proposal is None:
            raise ValueError("propose-memory feedback requires memory_proposal")
        if self.memory_proposal is not None and self.expected_action != "propose-memory":
            raise ValueError("memory_proposal requires expected_action=propose-memory")
        if self.evaluation_use_allowed and not self.quality_use_allowed:
            raise ValueError("Evaluation reuse requires quality operations consent")
        return self


class FeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str
    workspace_id: str
    principal_id: str
    run_id: str
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    capability_id: str | None = None
    agent_id: str | None = None
    input_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    rating: int | None = Field(default=None, ge=1, le=5)
    categories: tuple[FeedbackCategory, ...] = ()
    comment: str | None = None
    annotations: tuple[FeedbackAnnotation, ...] = ()
    expected_action: FeedbackExpectedAction = "none"
    quality_use_allowed: bool = True
    evaluation_use_allowed: bool = False
    memory_proposal: FeedbackMemoryProposal | None = None
    created_at: datetime
    record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FeedbackDispositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: FeedbackDispositionAction
    reason: str = Field(min_length=1, max_length=4000)


class FeedbackDispositionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition_id: str
    feedback_id: str
    feedback_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_id: str
    action: FeedbackDispositionAction
    reason: str
    actor_id: str
    created_at: datetime
    record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EvaluationSampleCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rubric: str = Field(min_length=1, max_length=4000)
    expected_result: dict[str, Any] | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(tag.strip() for tag in tags)
        if any(not tag for tag in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Evaluation sample tags must be unique and non-blank")
        return normalized


class EvaluationSampleCandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    feedback_id: str
    feedback_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_id: str
    run_id: str
    input_data: dict[str, Any]
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    capability_id: str | None = None
    agent_id: str | None = None
    rubric: str
    expected_result: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()
    actor_id: str
    created_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FeedbackMemoryConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmation_id: str
    feedback_id: str
    feedback_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_id: str
    principal_id: str
    memory_id: str
    memory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmed_at: datetime
    record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FeedbackMetricSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_count: int = Field(ge=0)
    rated_count: int = Field(ge=0)
    average_rating: float | None = Field(default=None, ge=1, le=5)
    negative_rating_count: int = Field(ge=0)
    evaluation_candidate_count: int = Field(ge=0)
    category_counts: dict[FeedbackCategory, int] = Field(default_factory=dict)


class FeedbackQualitySlice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    metrics: FeedbackMetricSet


class FeedbackQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    solution_id: str | None = None
    overall: FeedbackMetricSet
    by_solution_release: tuple[FeedbackQualitySlice, ...] = ()
    by_capability: tuple[FeedbackQualitySlice, ...] = ()


def validate_feedback_targets(payload: FeedbackInput, result: ResultView) -> None:
    blocks = {block.block_id: block for block in result.blocks}
    for annotation in payload.annotations:
        block = blocks.get(annotation.block_id)
        if block is None:
            raise FeedbackTargetError(f"Result block not found: {annotation.block_id}")
        if block.revision != annotation.block_revision:
            raise FeedbackTargetError(f"Result block revision changed: {annotation.block_id}")
        if block.content_digest != annotation.target_digest:
            raise FeedbackTargetError(f"Result block content changed: {annotation.block_id}")


def new_feedback_record(
    *,
    workspace_id: str,
    principal_id: str,
    result: ResultView,
    payload: FeedbackInput,
) -> FeedbackRecord:
    created_at = datetime.now(UTC)
    payload_values = payload.model_dump(mode="json", exclude={"annotations"})
    payload_values["annotations"] = [
        FeedbackAnnotation(**annotation.model_dump(mode="json")).model_dump(mode="json")
        for annotation in payload.annotations
    ]
    values = {
        "feedback_id": f"fb_{uuid4().hex}",
        "workspace_id": workspace_id,
        "principal_id": principal_id,
        "run_id": result.run_id,
        "result_digest": result.result_digest,
        "generation_id": result.generation_id,
        "solution_id": result.solution_id,
        "solution_release_id": result.solution_release_id,
        "capability_id": result.capability_id,
        "agent_id": result.agent_id,
        "input_digest": result.input_digest,
        **payload_values,
        "created_at": created_at.isoformat(),
    }
    return FeedbackRecord(**values, record_digest=canonical_digest(values))


def new_feedback_disposition(
    feedback: FeedbackRecord,
    payload: FeedbackDispositionInput,
    *,
    actor_id: str,
) -> FeedbackDispositionRecord:
    if not feedback.quality_use_allowed:
        raise FeedbackAuthorizationError("Feedback is not authorized for quality operations")
    values = {
        "disposition_id": f"fbd_{uuid4().hex}",
        "feedback_id": feedback.feedback_id,
        "feedback_digest": feedback.record_digest,
        "workspace_id": feedback.workspace_id,
        "action": payload.action,
        "reason": payload.reason.strip(),
        "actor_id": actor_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return FeedbackDispositionRecord(**values, record_digest=canonical_digest(values))


def new_evaluation_sample_candidate(
    feedback: FeedbackRecord,
    payload: EvaluationSampleCandidateInput,
    *,
    actor_id: str,
    input_data: dict[str, Any] | None,
) -> EvaluationSampleCandidateRecord:
    if not feedback.quality_use_allowed or not feedback.evaluation_use_allowed:
        raise FeedbackAuthorizationError("Feedback is not authorized for evaluation reuse")
    if input_data is None or feedback.input_digest is None:
        raise FeedbackTargetError("Run input is unavailable for evaluation replay")
    input_digest = canonical_digest(input_data)
    if input_digest != feedback.input_digest:
        raise FeedbackTargetError("Run input changed after feedback was recorded")
    values = {
        "candidate_id": f"esc_{uuid4().hex}",
        "feedback_id": feedback.feedback_id,
        "feedback_digest": feedback.record_digest,
        "workspace_id": feedback.workspace_id,
        "run_id": feedback.run_id,
        "input_data": input_data,
        "input_digest": input_digest,
        "result_digest": feedback.result_digest,
        "generation_id": feedback.generation_id,
        "solution_id": feedback.solution_id,
        "solution_release_id": feedback.solution_release_id,
        "capability_id": feedback.capability_id,
        "agent_id": feedback.agent_id,
        **payload.model_dump(mode="json"),
        "actor_id": actor_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return EvaluationSampleCandidateRecord(
        **values,
        content_digest=canonical_digest(values),
    )


def new_memory_confirmation(
    feedback: FeedbackRecord,
    *,
    memory_id: str,
    memory_digest: str,
) -> FeedbackMemoryConfirmation:
    values = {
        "confirmation_id": f"fmc_{uuid4().hex}",
        "feedback_id": feedback.feedback_id,
        "feedback_digest": feedback.record_digest,
        "workspace_id": feedback.workspace_id,
        "principal_id": feedback.principal_id,
        "memory_id": memory_id,
        "memory_digest": memory_digest,
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    return FeedbackMemoryConfirmation(**values, record_digest=canonical_digest(values))


class SqliteFeedbackStore:
    """Append-only feedback store for the single-node runtime."""

    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def create(
        self,
        record: FeedbackRecord,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> tuple[FeedbackRecord, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_digest, record_json
                FROM feedback
                WHERE workspace_id = ? AND principal_id = ?
                  AND run_id = ? AND idempotency_key = ?
                """,
                (
                    record.workspace_id,
                    record.principal_id,
                    record.run_id,
                    idempotency_key,
                ),
            ).fetchone()
            if row is not None:
                if str(row[0]) != request_digest:
                    raise FeedbackConflictError(
                        "Idempotency key was already used for different feedback"
                    )
                return FeedbackRecord.model_validate_json(str(row[1])), False
            connection.execute(
                """
                INSERT INTO feedback(
                    feedback_id, workspace_id, principal_id, run_id,
                    idempotency_key, request_digest, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.feedback_id,
                    record.workspace_id,
                    record.principal_id,
                    record.run_id,
                    idempotency_key,
                    request_digest,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
        return record, True

    def get(self, feedback_id: str) -> FeedbackRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        return FeedbackRecord.model_validate_json(str(row[0])) if row is not None else None

    def list_for_run(self, run_id: str, *, workspace_id: str) -> tuple[FeedbackRecord, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feedback
                WHERE run_id = ? AND workspace_id = ?
                ORDER BY created_at, feedback_id
                """,
                (run_id, workspace_id),
            ).fetchall()
        return tuple(FeedbackRecord.model_validate_json(str(row[0])) for row in rows)

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        solution_id: str | None = None,
        category: FeedbackCategory | None = None,
        limit: int = 100,
    ) -> tuple[FeedbackRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Feedback limit must be between 1 and 1000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feedback
                WHERE workspace_id = ?
                ORDER BY created_at DESC, feedback_id DESC
                """,
                (workspace_id,),
            ).fetchall()
        records = (
            FeedbackRecord.model_validate_json(str(row[0]))
            for row in rows
        )
        return tuple(
            record
            for record in records
            if record.quality_use_allowed
            and (solution_id is None or record.solution_id == solution_id)
            and (category is None or category in record.categories)
        )[:limit]

    def append_disposition(
        self,
        record: FeedbackDispositionRecord,
    ) -> FeedbackDispositionRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            feedback_row = connection.execute(
                "SELECT record_json FROM feedback WHERE feedback_id = ? AND workspace_id = ?",
                (record.feedback_id, record.workspace_id),
            ).fetchone()
            if feedback_row is None:
                raise KeyError(record.feedback_id)
            feedback = FeedbackRecord.model_validate_json(str(feedback_row[0]))
            if feedback.record_digest != record.feedback_digest:
                raise FeedbackTargetError("Disposition feedback digest mismatch")
            connection.execute(
                """
                INSERT INTO feedback_dispositions(
                    disposition_id, feedback_id, workspace_id, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.disposition_id,
                    record.feedback_id,
                    record.workspace_id,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
        return record

    def list_dispositions(
        self,
        feedback_id: str,
        *,
        workspace_id: str,
    ) -> tuple[FeedbackDispositionRecord, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feedback_dispositions
                WHERE feedback_id = ? AND workspace_id = ?
                ORDER BY created_at, disposition_id
                """,
                (feedback_id, workspace_id),
            ).fetchall()
        return tuple(FeedbackDispositionRecord.model_validate_json(str(row[0])) for row in rows)

    def create_evaluation_candidate(
        self,
        candidate: EvaluationSampleCandidateRecord,
        disposition: FeedbackDispositionRecord,
        *,
        request_digest: str,
    ) -> tuple[EvaluationSampleCandidateRecord, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            feedback_row = connection.execute(
                "SELECT record_json FROM feedback WHERE feedback_id = ? AND workspace_id = ?",
                (candidate.feedback_id, candidate.workspace_id),
            ).fetchone()
            if feedback_row is None:
                raise KeyError(candidate.feedback_id)
            feedback = FeedbackRecord.model_validate_json(str(feedback_row[0]))
            if (
                feedback.record_digest != candidate.feedback_digest
                or disposition.feedback_digest != candidate.feedback_digest
            ):
                raise FeedbackTargetError("Evaluation candidate feedback digest mismatch")
            existing = connection.execute(
                """
                SELECT request_digest, record_json
                FROM evaluation_sample_candidates WHERE feedback_id = ?
                """,
                (candidate.feedback_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_digest:
                    raise FeedbackConflictError(
                        "Feedback already has a different evaluation sample candidate"
                    )
                return EvaluationSampleCandidateRecord.model_validate_json(
                    str(existing[1])
                ), False
            connection.execute(
                """
                INSERT INTO evaluation_sample_candidates(
                    candidate_id, feedback_id, workspace_id, request_digest,
                    record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.feedback_id,
                    candidate.workspace_id,
                    request_digest,
                    candidate.model_dump_json(),
                    candidate.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO feedback_dispositions(
                    disposition_id, feedback_id, workspace_id, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    disposition.disposition_id,
                    disposition.feedback_id,
                    disposition.workspace_id,
                    disposition.model_dump_json(),
                    disposition.created_at.isoformat(),
                ),
            )
        return candidate, True

    def list_evaluation_candidates(
        self,
        workspace_id: str,
        *,
        solution_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EvaluationSampleCandidateRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Evaluation candidate limit must be between 1 and 1000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM evaluation_sample_candidates
                WHERE workspace_id = ?
                ORDER BY created_at DESC, candidate_id DESC
                """,
                (workspace_id,),
            ).fetchall()
        records = (
            EvaluationSampleCandidateRecord.model_validate_json(str(row[0]))
            for row in rows
        )
        return tuple(
            record
            for record in records
            if solution_id is None or record.solution_id == solution_id
        )[:limit]

    def get_evaluation_candidate(
        self,
        candidate_id: str,
    ) -> EvaluationSampleCandidateRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM evaluation_sample_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
        return (
            EvaluationSampleCandidateRecord.model_validate_json(str(row[0]))
            if row
            else None
        )

    def quality_summary(
        self,
        workspace_id: str,
        *,
        solution_id: str | None = None,
    ) -> FeedbackQualitySummary:
        with self._lock, self._connect() as connection:
            feedback_rows = connection.execute(
                """
                SELECT record_json FROM feedback
                WHERE workspace_id = ?
                ORDER BY created_at, feedback_id
                """,
                (workspace_id,),
            ).fetchall()
            candidate_rows = connection.execute(
                """
                SELECT feedback_id FROM evaluation_sample_candidates
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchall()
        records = tuple(
            record
            for row in feedback_rows
            if (record := FeedbackRecord.model_validate_json(str(row[0]))).quality_use_allowed
            and (solution_id is None or record.solution_id == solution_id)
        )
        candidate_ids = {str(row[0]) for row in candidate_rows}
        release_groups: dict[str, list[FeedbackRecord]] = {}
        capability_groups: dict[str, list[FeedbackRecord]] = {}
        for record in records:
            release_groups.setdefault(record.solution_release_id or "_unreleased", []).append(
                record
            )
            capability_key = record.capability_id or (
                f"agent:{record.agent_id}" if record.agent_id else "_unknown"
            )
            capability_groups.setdefault(capability_key, []).append(record)
        return FeedbackQualitySummary(
            workspace_id=workspace_id,
            solution_id=solution_id,
            overall=_feedback_metrics(records, candidate_ids),
            by_solution_release=tuple(
                FeedbackQualitySlice(
                    key=key,
                    metrics=_feedback_metrics(tuple(group), candidate_ids),
                )
                for key, group in sorted(release_groups.items())
            ),
            by_capability=tuple(
                FeedbackQualitySlice(
                    key=key,
                    metrics=_feedback_metrics(tuple(group), candidate_ids),
                )
                for key, group in sorted(capability_groups.items())
            ),
        )

    def get_memory_confirmation(
        self,
        feedback_id: str,
    ) -> FeedbackMemoryConfirmation | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feedback_memory_confirmations WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        return FeedbackMemoryConfirmation.model_validate_json(str(row[0])) if row else None

    def confirm_memory(
        self,
        record: FeedbackMemoryConfirmation,
    ) -> tuple[FeedbackMemoryConfirmation, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            feedback_row = connection.execute(
                "SELECT record_json FROM feedback WHERE feedback_id = ? AND workspace_id = ?",
                (record.feedback_id, record.workspace_id),
            ).fetchone()
            if feedback_row is None:
                raise KeyError(record.feedback_id)
            feedback = FeedbackRecord.model_validate_json(str(feedback_row[0]))
            if feedback.record_digest != record.feedback_digest:
                raise FeedbackTargetError("Memory confirmation feedback digest mismatch")
            existing = connection.execute(
                "SELECT record_json FROM feedback_memory_confirmations WHERE feedback_id = ?",
                (record.feedback_id,),
            ).fetchone()
            if existing is not None:
                restored = FeedbackMemoryConfirmation.model_validate_json(str(existing[0]))
                if restored.memory_digest != record.memory_digest:
                    raise FeedbackConflictError(
                        "Feedback was already confirmed as a different memory"
                    )
                return restored, False
            connection.execute(
                """
                INSERT INTO feedback_memory_confirmations(
                    confirmation_id, feedback_id, workspace_id,
                    principal_id, record_json, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.confirmation_id,
                    record.feedback_id,
                    record.workspace_id,
                    record.principal_id,
                    record.model_dump_json(),
                    record.confirmed_at.isoformat(),
                ),
            )
        return record, True

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, principal_id, run_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_dispositions (
                    disposition_id TEXT PRIMARY KEY,
                    feedback_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_sample_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    feedback_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_memory_confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    feedback_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _feedback_metrics(
    records: tuple[FeedbackRecord, ...],
    evaluation_candidate_ids: set[str],
) -> FeedbackMetricSet:
    ratings = [record.rating for record in records if record.rating is not None]
    category_counts: dict[FeedbackCategory, int] = {}
    for record in records:
        for category in record.categories:
            category_counts[category] = category_counts.get(category, 0) + 1
    return FeedbackMetricSet(
        feedback_count=len(records),
        rated_count=len(ratings),
        average_rating=round(sum(ratings) / len(ratings), 3) if ratings else None,
        negative_rating_count=sum(rating <= 2 for rating in ratings),
        evaluation_candidate_count=sum(
            record.feedback_id in evaluation_candidate_ids for record in records
        ),
        category_counts=dict(sorted(category_counts.items())),
    )