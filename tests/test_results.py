from datetime import UTC, datetime

import pytest

from sage.domain import Principal, RuntimeContext
from sage.results import (
    ResultUnavailableError,
    build_result_view,
    build_result_view_from_output,
    result_available_data,
)
from sage.runs import RunEvent, RunSnapshot


def _snapshot(result: dict[str, object]) -> RunSnapshot:
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        run_id="run_result",
        thread_id="thread_result",
        generation_id="gen_result",
        solution_id="contract-review",
        solution_release_id="contract-review@1.0.0",
    )
    return RunSnapshot(
        context=context,
        status="completed",
        input_data={"kind": "message", "request": {"message": "review"}},
        events=(
            RunEvent(1, "run.started", {"run_id": context.run_id}, datetime.now(UTC)),
            RunEvent(
                2,
                "run.completed",
                {
                    "response": {
                        "status": "completed",
                        "message": "Review complete",
                        "result": result,
                        "usage": {"model_calls": 1},
                    }
                },
                datetime.now(UTC),
            ),
        ),
    )


def test_result_view_projects_complex_blocks_and_strict_evidence() -> None:
    result = {
        "status": "needs-review",
        "issues": [
            {
                "check_id": "payment",
                "severity": "high",
                "summary": "Payment timing needs review.",
            }
        ],
        "citations": [
            {"document_id": "legacy-document", "quote": "Legacy display citation"}
        ],
        "artifacts": [
            {"artifact_id": "art_report", "filename": "review.json"}
        ],
        "evidence": {
            "citations": [
                {
                    "citation_id": "cit_payment",
                    "artifact_id": "art_contract",
                    "locator": {"kind": "page", "value": "3"},
                    "quoted_text": "Payment is due in 30 days.",
                }
            ],
            "assessments": [
                {
                    "assessment_id": "asm_payment",
                    "status": "risk",
                    "summary": "Payment timing needs review.",
                    "citation_refs": ["cit_payment"],
                    "provenance": {
                        "produced_by": "contract-review",
                        "implementation_version": "1.0.0",
                        "generation_id": "gen_result",
                        "solution_release_id": "contract-review@1.0.0",
                    },
                }
            ],
        },
    }

    snapshot = _snapshot(result)
    view = build_result_view(snapshot)

    assert view.data == result
    assert view.artifact_ids == ("art_report", "art_contract")
    assert [artifact.content for artifact in view.artifacts] == [
        "/v1/artifacts/art_report/content?solution_id=contract-review",
        "/v1/artifacts/art_contract/content?solution_id=contract-review",
    ]
    assert [block.block_id for block in view.blocks] == [
        "answer",
        "result",
        "issues",
        "citations",
        "artifacts",
        "evidence-citations",
        "assessments",
    ]
    assert view.blocks[1].content == {"status": "needs-review"}
    assert view.blocks[2].content["columns"] == [
        "check_id",
        "severity",
        "summary",
    ]
    assert view.evidence.assessments[0].citation_refs == ("cit_payment",)
    assert build_result_view(snapshot).result_digest == view.result_digest


def test_legacy_citations_are_displayed_without_fabricating_evidence() -> None:
    view = build_result_view(
        _snapshot(
            {
                "status": "answered",
                "answer": "Seven years.",
                "citations": [
                    {"document_id": "policy-1", "quote": "Retain for seven years."}
                ],
            }
        )
    )

    assert view.evidence.citations == ()
    assert next(block for block in view.blocks if block.block_id == "citations").content == {
        "items": [{"document_id": "policy-1", "quote": "Retain for seven years."}]
    }


def test_explicit_invalid_evidence_is_rejected() -> None:
    with pytest.raises(ResultUnavailableError, match="EvidenceEnvelope"):
        build_result_view(
            _snapshot(
                {
                    "status": "invalid",
                    "evidence": {
                        "assessments": [
                            {
                                "assessment_id": "asm_invalid",
                                "status": "risk",
                                "summary": "Missing provenance",
                            }
                        ]
                    },
                }
            )
        )


def test_native_output_projector_matches_available_event_metadata() -> None:
    snapshot = _snapshot({"answer": "found"})
    view = build_result_view_from_output(
        snapshot.context,
        snapshot.input_data,
        {"answer": "found"},
        capability_id="tool:mcp:contracts:search",
        usage={"tool_calls": 1},
    )
    event = result_available_data(view)

    assert view.capability_id == "tool:mcp:contracts:search"
    assert view.usage == {"tool_calls": 1}
    assert event["result_digest"] == view.result_digest
    assert event["artifacts"] == []
    assert event["blocks"][0]["content_digest"] == view.blocks[0].content_digest