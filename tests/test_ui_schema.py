import pytest
from pydantic import ValidationError

from sage.ui_schema import SolutionUiSchema, UiAction, UiWidget


def test_solution_ui_schema_uses_platform_widgets_and_actions() -> None:
    schema = SolutionUiSchema(
        solution_id="contract-review",
        title="Contract review",
        widgets=(
            UiWidget(
                widget_id="input",
                kind="input-form",
                actions=(
                    UiAction(
                        action_id="review",
                        label="Review",
                        kind="start-workflow",
                        target="review-contract",
                    ),
                ),
            ),
            UiWidget(
                widget_id="citations",
                kind="citation-list",
                data_source="/v1/runs/{run_id}/citations",
            ),
        ),
    )

    assert schema.schema_version == 1
    assert [widget.kind for widget in schema.widgets] == [
        "input-form",
        "citation-list",
    ]


def test_solution_ui_schema_rejects_external_data_source() -> None:
    with pytest.raises(ValidationError, match="platform /v1/"):
        UiWidget(
            widget_id="unsafe",
            kind="result-table",
            data_source="https://example.com/data",
        )


def test_solution_ui_schema_rejects_unknown_or_script_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        UiWidget.model_validate(
            {
                "widget_id": "unsafe",
                "kind": "result-detail",
                "script": "alert(document.cookie)",
            }
        )