"""Versioned regression sample sets and imported comparison evidence."""

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sage.digests import canonical_digest
from sage.feedback import EvaluationSampleCandidateRecord


class QualityEvaluationError(ValueError):
    """Raised when regression evidence is inconsistent or cannot be reused safely."""


class QualityEvaluationConflictError(QualityEvaluationError):
    """Raised when an idempotency key or version is reused with different content."""


class EvaluationSampleSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("version", "title", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("candidate_ids")
    @classmethod
    def normalize_candidate_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(value.strip() for value in values))
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Evaluation sample candidate ids must be unique and non-blank")
        return normalized


class EvaluationSampleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    feedback_id: str
    feedback_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_data: dict[str, object]
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_generation_id: str
    source_solution_release_id: str | None = None
    rubric: str
    expected_result: dict[str, object] | None = None
    expected_result_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    tags: tuple[str, ...] = ()


class EvaluationSampleSetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_set_id: str
    workspace_id: str
    solution_id: str | None = None
    target_id: str
    version: str
    title: str
    description: str = ""
    samples: tuple[EvaluationSampleRef, ...] = Field(min_length=1)
    created_by: str
    created_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EvaluationComparisonOutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    baseline_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    candidate_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    baseline_result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    notes: str = Field(default="", max_length=2000)


class EvaluationComparisonInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_id: str = Field(min_length=1, max_length=200)
    evaluator_version: str = Field(min_length=1, max_length=64)
    outcomes: tuple[EvaluationComparisonOutcomeInput, ...] = Field(
        min_length=1,
        max_length=500,
    )

    @model_validator(mode="after")
    def require_distinct_targets_and_outcomes(self) -> "EvaluationComparisonInput":
        if self.baseline_target_digest == self.candidate_target_digest:
            raise ValueError("Baseline and candidate target digests must differ")
        candidate_ids = [outcome.candidate_id for outcome in self.outcomes]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Evaluation comparison outcomes must be unique per sample")
        return self


class EvaluationComparisonMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=1)
    baseline_average: float = Field(ge=0, le=1)
    candidate_average: float = Field(ge=0, le=1)
    score_delta: float = Field(ge=-1, le=1)
    improved_count: int = Field(ge=0)
    regressed_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)


class EvaluationComparisonRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_id: str
    workspace_id: str
    sample_set_id: str
    sample_set_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    solution_id: str | None = None
    target_id: str
    baseline_target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_id: str
    evaluator_version: str
    execution_mode: Literal["imported"] = "imported"
    outcomes: tuple[EvaluationComparisonOutcomeInput, ...]
    metrics: EvaluationComparisonMetrics
    status: Literal["improved", "regressed", "unchanged"]
    created_by: str
    created_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SqliteQualityEvaluationStore:
    """Append-only quality evidence for the declared single-node runtime."""

    def __init__(self, path: str) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path)
        self._lock = RLock()
        self._initialize()

    def create_sample_set(
        self,
        payload: EvaluationSampleSetInput,
        candidates: Sequence[EvaluationSampleCandidateRecord],
        *,
        workspace_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> tuple[EvaluationSampleSetRecord, bool]:
        requested_ids = payload.candidate_ids
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        if tuple(sorted(by_id)) != requested_ids:
            raise QualityEvaluationError("Evaluation sample candidates are incomplete")
        if any(candidate.workspace_id != workspace_id for candidate in candidates):
            raise QualityEvaluationError("Evaluation sample candidates cross workspace scope")
        solution_ids = {candidate.solution_id for candidate in candidates}
        target_ids = {_candidate_target(candidate) for candidate in candidates}
        if len(solution_ids) != 1 or len(target_ids) != 1:
            raise QualityEvaluationError(
                "Evaluation sample sets require one Solution and one target capability"
            )
        solution_id = next(iter(solution_ids))
        target_id = next(iter(target_ids))
        ordered = tuple(by_id[candidate_id] for candidate_id in requested_ids)
        sample_refs = tuple(_sample_ref(candidate) for candidate in ordered)
        request_payload = {
            "workspace_id": workspace_id,
            "solution_id": solution_id,
            "target_id": target_id,
            "version": payload.version,
            "title": payload.title,
            "description": payload.description,
            "samples": [sample.model_dump(mode="json") for sample in sample_refs],
        }
        request_digest = canonical_digest(request_payload)
        scope_key = canonical_digest(
            {
                "workspace_id": workspace_id,
                "solution_id": solution_id,
                "target_id": target_id,
                "version": payload.version,
            }
        )
        values = {
            "sample_set_id": f"ess_{uuid4().hex}",
            **request_payload,
            "created_by": actor_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        record = EvaluationSampleSetRecord(
            **values,
            content_digest=canonical_digest(values),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_digest, record_json FROM evaluation_sample_sets
                WHERE (workspace_id = ? AND idempotency_key = ?) OR scope_key = ?
                ORDER BY CASE WHEN idempotency_key = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (workspace_id, idempotency_key, scope_key, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_digest:
                    raise QualityEvaluationConflictError(
                        "Evaluation sample set key or version has different content"
                    )
                return EvaluationSampleSetRecord.model_validate_json(str(existing[1])), False
            connection.execute(
                """
                INSERT INTO evaluation_sample_sets(
                    sample_set_id, workspace_id, solution_id, target_id, version,
                    scope_key, idempotency_key, request_digest, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.sample_set_id,
                    record.workspace_id,
                    record.solution_id,
                    record.target_id,
                    record.version,
                    scope_key,
                    idempotency_key,
                    request_digest,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
        return record, True

    def get_sample_set(self, sample_set_id: str) -> EvaluationSampleSetRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM evaluation_sample_sets WHERE sample_set_id = ?",
                (sample_set_id,),
            ).fetchone()
        return EvaluationSampleSetRecord.model_validate_json(str(row[0])) if row else None

    def list_sample_sets(
        self,
        workspace_id: str,
        *,
        solution_id: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EvaluationSampleSetRecord, ...]:
        _require_limit(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM evaluation_sample_sets
                WHERE workspace_id = ? ORDER BY created_at DESC, sample_set_id DESC
                """,
                (workspace_id,),
            ).fetchall()
        records = (
            EvaluationSampleSetRecord.model_validate_json(str(row[0])) for row in rows
        )
        return tuple(
            record
            for record in records
            if (solution_id is None or record.solution_id == solution_id)
            and (target_id is None or record.target_id == target_id)
        )[:limit]

    def create_comparison(
        self,
        sample_set: EvaluationSampleSetRecord,
        payload: EvaluationComparisonInput,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> tuple[EvaluationComparisonRecord, bool]:
        expected_ids = tuple(sample.candidate_id for sample in sample_set.samples)
        by_id = {outcome.candidate_id: outcome for outcome in payload.outcomes}
        if tuple(sorted(by_id)) != tuple(sorted(expected_ids)):
            raise QualityEvaluationError(
                "Evaluation comparison must contain every sample exactly once"
            )
        outcomes = tuple(by_id[candidate_id] for candidate_id in expected_ids)
        metrics = _comparison_metrics(outcomes)
        epsilon = 1e-9
        status = (
            "improved"
            if metrics.score_delta > epsilon
            else "regressed"
            if metrics.score_delta < -epsilon
            else "unchanged"
        )
        request_payload = {
            "workspace_id": sample_set.workspace_id,
            "sample_set_id": sample_set.sample_set_id,
            "sample_set_digest": sample_set.content_digest,
            "solution_id": sample_set.solution_id,
            "target_id": sample_set.target_id,
            "baseline_target_digest": payload.baseline_target_digest,
            "candidate_target_digest": payload.candidate_target_digest,
            "evaluator_id": payload.evaluator_id.strip(),
            "evaluator_version": payload.evaluator_version.strip(),
            "execution_mode": "imported",
            "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
            "metrics": metrics.model_dump(mode="json"),
            "status": status,
        }
        request_digest = canonical_digest(request_payload)
        values = {
            "comparison_id": f"ecmp_{uuid4().hex}",
            **request_payload,
            "created_by": actor_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        record = EvaluationComparisonRecord(
            **values,
            content_digest=canonical_digest(values),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_set = connection.execute(
                "SELECT record_json FROM evaluation_sample_sets WHERE sample_set_id = ?",
                (sample_set.sample_set_id,),
            ).fetchone()
            if existing_set is None:
                raise KeyError(sample_set.sample_set_id)
            persisted_set = EvaluationSampleSetRecord.model_validate_json(str(existing_set[0]))
            if persisted_set.content_digest != sample_set.content_digest:
                raise QualityEvaluationError("Evaluation sample set digest mismatch")
            existing = connection.execute(
                """
                SELECT request_digest, record_json FROM evaluation_comparisons
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (sample_set.workspace_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_digest:
                    raise QualityEvaluationConflictError(
                        "Idempotency key was already used for a different comparison"
                    )
                return EvaluationComparisonRecord.model_validate_json(str(existing[1])), False
            connection.execute(
                """
                INSERT INTO evaluation_comparisons(
                    comparison_id, workspace_id, sample_set_id, idempotency_key,
                    request_digest, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.comparison_id,
                    record.workspace_id,
                    record.sample_set_id,
                    idempotency_key,
                    request_digest,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
        return record, True

    def list_comparisons(
        self,
        sample_set_id: str,
        *,
        workspace_id: str,
        limit: int = 100,
    ) -> tuple[EvaluationComparisonRecord, ...]:
        _require_limit(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM evaluation_comparisons
                WHERE sample_set_id = ? AND workspace_id = ?
                ORDER BY created_at DESC, comparison_id DESC LIMIT ?
                """,
                (sample_set_id, workspace_id, limit),
            ).fetchall()
        return tuple(EvaluationComparisonRecord.model_validate_json(str(row[0])) for row in rows)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_sample_sets (
                    sample_set_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    solution_id TEXT,
                    target_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    scope_key TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_comparisons (
                    comparison_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    sample_set_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, idempotency_key),
                    FOREIGN KEY(sample_set_id) REFERENCES evaluation_sample_sets(sample_set_id)
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _candidate_target(candidate: EvaluationSampleCandidateRecord) -> str:
    target_id = candidate.capability_id or (
        f"agent:{candidate.agent_id}" if candidate.agent_id else None
    )
    if target_id is None:
        raise QualityEvaluationError("Evaluation sample candidate has no target capability")
    return target_id


def _sample_ref(candidate: EvaluationSampleCandidateRecord) -> EvaluationSampleRef:
    return EvaluationSampleRef(
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate.content_digest,
        feedback_id=candidate.feedback_id,
        feedback_digest=candidate.feedback_digest,
        input_data=candidate.input_data,
        input_digest=candidate.input_digest,
        result_digest=candidate.result_digest,
        source_generation_id=candidate.generation_id,
        source_solution_release_id=candidate.solution_release_id,
        rubric=candidate.rubric,
        expected_result=candidate.expected_result,
        expected_result_digest=(
            canonical_digest(candidate.expected_result)
            if candidate.expected_result is not None
            else None
        ),
        tags=candidate.tags,
    )


def _comparison_metrics(
    outcomes: tuple[EvaluationComparisonOutcomeInput, ...],
) -> EvaluationComparisonMetrics:
    sample_count = len(outcomes)
    baseline_average = round(
        sum(outcome.baseline_score for outcome in outcomes) / sample_count,
        6,
    )
    candidate_average = round(
        sum(outcome.candidate_score for outcome in outcomes) / sample_count,
        6,
    )
    epsilon = 1e-9
    return EvaluationComparisonMetrics(
        sample_count=sample_count,
        baseline_average=baseline_average,
        candidate_average=candidate_average,
        score_delta=round(candidate_average - baseline_average, 6),
        improved_count=sum(
            outcome.candidate_score - outcome.baseline_score > epsilon
            for outcome in outcomes
        ),
        regressed_count=sum(
            outcome.baseline_score - outcome.candidate_score > epsilon
            for outcome in outcomes
        ),
        unchanged_count=sum(
            abs(outcome.candidate_score - outcome.baseline_score) <= epsilon
            for outcome in outcomes
        ),
    )


def _require_limit(limit: int) -> None:
    if limit < 1 or limit > 1000:
        raise ValueError("Evaluation limit must be between 1 and 1000")