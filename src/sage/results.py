"""Stable client-facing projections of completed Run results."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, TypeGuard
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from sage.assessments import EvidenceEnvelope
from sage.digests import canonical_digest
from sage.domain import RuntimeContext
from sage.runs import RunSnapshot


class ResultUnavailableError(ValueError):
    """Raised when a Run has no complete client-facing result yet."""


ResultBlockType = Literal[
    "markdown",
    "table",
    "key-value",
    "chart-spec",
    "artifact",
    "citations",
    "assessments",
    "timeline",
    "form",
    "error",
]


class ResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(min_length=1, max_length=100)
    type: ResultBlockType
    revision: int = Field(default=1, ge=1)
    content: Any
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResultArtifactLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    metadata: str
    content: str


class ResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    thread_id: str
    status: Literal["completed"] = "completed"
    generation_id: str
    solution_id: str | None = None
    solution_release_id: str | None = None
    capability_id: str | None = None
    agent_id: str | None = None
    input_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    data: dict[str, Any] = Field(default_factory=dict)
    blocks: tuple[ResultBlock, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    artifacts: tuple[ResultArtifactLink, ...] = ()
    evidence: EvidenceEnvelope = Field(default_factory=EvidenceEnvelope)
    usage: dict[str, int | float] = Field(default_factory=dict)
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def build_result_view(snapshot: RunSnapshot) -> ResultView:
    if snapshot.status != "completed":
        raise ResultUnavailableError(f"Run result is not complete: {snapshot.status}")

    completed = next(
        (event for event in reversed(snapshot.events) if event.name == "run.completed"),
        None,
    )
    orchestration = next(
        (
            event
            for event in reversed(snapshot.events)
            if event.name == "skill.orchestration.result"
        ),
        None,
    )
    if completed is None:
        raise ResultUnavailableError("Run has no completion event")

    response = completed.data.get("response")
    if isinstance(response, Mapping):
        return build_result_view_from_response(
            snapshot.context,
            snapshot.input_data,
            response,
        )
    orchestration_output = orchestration.data.get("output") if orchestration else None
    if isinstance(orchestration_output, Mapping):
        return build_result_view_from_output(
            snapshot.context,
            snapshot.input_data,
            orchestration_output,
            capability_id=(
                f"skill:{snapshot.context.skill_id}" if snapshot.context.skill_id else None
            ),
        )
    completed_output = completed.data.get("output")
    if isinstance(completed_output, Mapping):
        raw_capability_id = completed.data.get("capability_id")
        raw_usage = completed.data.get("usage")
        return build_result_view_from_output(
            snapshot.context,
            snapshot.input_data,
            completed_output,
            capability_id=(
                raw_capability_id if isinstance(raw_capability_id, str) else None
            ),
            usage=raw_usage if isinstance(raw_usage, Mapping) else None,
        )
    raise ResultUnavailableError("Run completed without a materialized result")


def build_result_view_from_response(
    context: RuntimeContext,
    input_data: Mapping[str, object] | None,
    response: Mapping[str, Any],
) -> ResultView:
    raw_message = response.get("message")
    message = raw_message if isinstance(raw_message, str) else None
    raw_result = response.get("result")
    data = dict(raw_result) if isinstance(raw_result, Mapping) else {}
    raw_route = response.get("route")
    capability_id = None
    if isinstance(raw_route, Mapping) and isinstance(raw_route.get("skill"), str):
        capability_id = f"skill:{raw_route['skill']}"
    if context.agent_id is not None:
        capability_id = f"agent:{context.agent_id}"
    raw_artifact_ids = response.get("artifact_ids")
    artifact_ids = (
        tuple(item for item in raw_artifact_ids if isinstance(item, str))
        if isinstance(raw_artifact_ids, list)
        else ()
    )
    raw_usage = response.get("usage")
    return build_result_view_from_output(
        context,
        input_data,
        data,
        capability_id=capability_id,
        message=message,
        usage=raw_usage if isinstance(raw_usage, Mapping) else None,
        artifact_ids=artifact_ids,
    )


def build_result_view_from_output(
    context: RuntimeContext,
    input_data: Mapping[str, object] | None,
    output: Mapping[str, Any],
    *,
    capability_id: str | None,
    message: str | None = None,
    usage: Mapping[str, object] | None = None,
    artifact_ids: tuple[str, ...] = (),
) -> ResultView:
    data = dict(output)
    normalized_usage = {
        str(key): value
        for key, value in (usage or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }

    evidence = _evidence(data, context.created_at)
    artifact_ids = _artifact_ids(data, evidence, artifact_ids)
    artifacts = _artifact_links(artifact_ids, context.solution_id)
    blocks = _result_blocks(message, data, evidence)

    projection = {
        "schema_version": "1.0",
        "run_id": context.run_id,
        "thread_id": context.thread_id,
        "status": "completed",
        "generation_id": context.generation_id,
        "solution_id": context.solution_id,
        "solution_release_id": context.solution_release_id,
        "capability_id": capability_id,
        "agent_id": context.agent_id,
        "input_digest": (
            canonical_digest(input_data) if input_data is not None else None
        ),
        "data": data,
        "blocks": [block.model_dump(mode="json") for block in blocks],
        "artifact_ids": artifact_ids,
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "evidence": evidence.model_dump(mode="json"),
        "usage": normalized_usage,
    }
    return ResultView(**projection, result_digest=canonical_digest(projection))


def result_available_data(view: ResultView) -> dict[str, object]:
    return {
        "run_id": view.run_id,
        "result_digest": view.result_digest,
        "blocks": [
            {
                "block_id": block.block_id,
                "type": block.type,
                "revision": block.revision,
                "content_digest": block.content_digest,
            }
            for block in view.blocks
        ],
        "artifact_ids": list(view.artifact_ids),
        "artifacts": [artifact.model_dump(mode="json") for artifact in view.artifacts],
        "links": {
            "result": f"/v1/runs/{view.run_id}/result",
            "feedback": f"/v1/runs/{view.run_id}/feedback",
        },
    }


def _evidence(data: Mapping[str, Any], run_created_at: datetime) -> EvidenceEnvelope:
    raw = data.get("evidence")
    if raw is None:
        return EvidenceEnvelope()
    if not isinstance(raw, Mapping):
        raise ResultUnavailableError("Run evidence must be an EvidenceEnvelope object")
    normalized = deepcopy(dict(raw))
    assessments = normalized.get("assessments")
    if _is_sequence(assessments):
        for assessment in assessments:
            if not isinstance(assessment, dict):
                continue
            provenance = assessment.get("provenance")
            if isinstance(provenance, dict):
                provenance.setdefault("created_at", run_created_at.isoformat())
    try:
        return EvidenceEnvelope.model_validate(normalized)
    except ValueError as exc:
        raise ResultUnavailableError("Run evidence does not match EvidenceEnvelope") from exc


def _artifact_ids(
    data: Mapping[str, Any],
    evidence: EvidenceEnvelope,
    response_artifact_ids: tuple[str, ...],
) -> tuple[str, ...]:
    values = list(response_artifact_ids)
    raw_artifacts = data.get("artifacts")
    if _is_sequence(raw_artifacts):
        for item in raw_artifacts:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("artifact_id"), str):
                values.append(item["artifact_id"])
    values.extend(
        citation.artifact_id
        for citation in evidence.citations
        if citation.artifact_id is not None
    )
    return tuple(dict.fromkeys(value for value in values if value))


def _artifact_links(
    artifact_ids: tuple[str, ...],
    solution_id: str | None,
) -> tuple[ResultArtifactLink, ...]:
    suffix = f"?solution_id={quote(solution_id, safe='')}" if solution_id else ""
    return tuple(
        ResultArtifactLink(
            artifact_id=artifact_id,
            metadata=f"/v1/artifacts/{quote(artifact_id, safe='')}{suffix}",
            content=f"/v1/artifacts/{quote(artifact_id, safe='')}/content{suffix}",
        )
        for artifact_id in artifact_ids
    )


def _result_blocks(
    message: str | None,
    data: Mapping[str, Any],
    evidence: EvidenceEnvelope,
) -> list[ResultBlock]:
    blocks: list[ResultBlock] = []
    if message:
        blocks.append(_block("answer", "markdown", {"markdown": message}))

    specialized = {"issues", "findings", "stories", "citations", "artifacts", "evidence"}
    summary = {key: value for key, value in data.items() if key not in specialized}
    if summary or data:
        blocks.append(_block("result", "key-value", summary or dict(data)))

    for key in ("issues", "findings", "stories"):
        rows = data.get(key)
        if _is_sequence(rows) and rows and all(isinstance(row, Mapping) for row in rows):
            normalized_rows = [dict(row) for row in rows]
            columns = tuple(dict.fromkeys(key for row in normalized_rows for key in row))
            blocks.append(
                _block(
                    key,
                    "table",
                    {"columns": columns, "rows": normalized_rows},
                )
            )

    legacy_citations = data.get("citations")
    if _is_sequence(legacy_citations) and legacy_citations:
        blocks.append(
            _block("citations", "citations", {"items": list(legacy_citations)})
        )

    raw_artifacts = data.get("artifacts")
    if _is_sequence(raw_artifacts) and raw_artifacts:
        blocks.append(_block("artifacts", "artifact", {"items": list(raw_artifacts)}))

    if evidence.citations:
        blocks.append(
            _block(
                "evidence-citations",
                "citations",
                {
                    "items": [
                        citation.model_dump(mode="json") for citation in evidence.citations
                    ]
                },
            )
        )
    if evidence.assessments:
        blocks.append(
            _block(
                "assessments",
                "assessments",
                {
                    "items": [
                        assessment.model_dump(mode="json")
                        for assessment in evidence.assessments
                    ]
                },
            )
        )
    return blocks


def _block(block_id: str, block_type: ResultBlockType, content: Any) -> ResultBlock:
    return ResultBlock(
        block_id=block_id,
        type=block_type,
        content=content,
        content_digest=canonical_digest(content),
    )


def _is_sequence(value: object) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))