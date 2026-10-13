"""Release-bound evaluation reports used as Catalog activation gates."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage.control_models import EvaluationReportRecord
from sage.control_store import LocalControlStore
from sage.digests import canonical_digest


class EvaluationError(ValueError):
    """Raised when an evaluation report does not match a locked suite."""


class EvaluationMetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    threshold: float
    direction: Literal["at-least", "at-most"] = "at-least"


class EvaluationSuiteDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    metrics: tuple[EvaluationMetricDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_metrics(self) -> "EvaluationSuiteDefinition":
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("Evaluation metric names must be unique")
        return self


class EvaluationManager:
    def __init__(self, store: LocalControlStore) -> None:
        self._store = store

    def record(
        self,
        release_id: str,
        *,
        suite_id: str,
        status: str,
        metrics: dict[str, float],
        summary: str,
        actor_fingerprint: str,
    ) -> EvaluationReportRecord:
        state = self._store.snapshot()
        release = state.solution_releases.get(release_id)
        if release is None:
            raise KeyError(release_id)
        suites = [
            component
            for component in release.components
            if component.kind == "evaluation-suite" and component.component_id == suite_id
        ]
        if len(suites) != 1:
            raise EvaluationError(
                f"Evaluation suite is not locked by Solution Release: {suite_id}"
            )
        if status not in {"passed", "failed"}:
            raise EvaluationError("Evaluation status must be passed or failed")
        suite = suites[0]
        payload = {
            "release_id": release.release_id,
            "release_digest": release.content_digest,
            "suite_id": suite.component_id,
            "suite_version": suite.version,
            "suite_digest": suite.content_digest,
            "status": status,
            "metrics": metrics,
            "summary": summary.strip(),
            "actor_fingerprint": actor_fingerprint,
        }
        report = EvaluationReportRecord(
            report_id=f"eval_{uuid4().hex}",
            **payload,
            content_digest=canonical_digest(payload),
        )

        def save(current: object) -> None:
            current.evaluations[report.report_id] = report  # type: ignore[attr-defined]

        self._store.update(save)
        return report

    def list_for_release(self, release_id: str) -> list[EvaluationReportRecord]:
        state = self._store.snapshot()
        if release_id not in state.solution_releases:
            raise KeyError(release_id)
        return sorted(
            (
                report
                for report in state.evaluations.values()
                if report.release_id == release_id
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )


def evaluation_report_digest(report: EvaluationReportRecord) -> str:
    return canonical_digest(
        {
            "release_id": report.release_id,
            "release_digest": report.release_digest,
            "suite_id": report.suite_id,
            "suite_version": report.suite_version,
            "suite_digest": report.suite_digest,
            "status": report.status,
            "metrics": report.metrics,
            "summary": report.summary,
            "actor_fingerprint": report.actor_fingerprint,
        }
    )


def load_evaluation_suite(path: Path) -> EvaluationSuiteDefinition:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise EvaluationError("Evaluation Suite must be an object")
    return EvaluationSuiteDefinition.model_validate(raw)