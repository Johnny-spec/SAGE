from pathlib import Path

import pytest
from pydantic import ValidationError

from sage.workflows import WorkflowTemplate, load_workflow_template

ROOT = Path(__file__).parents[1]


def _template(steps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "apiVersion": "sage.dev/v1alpha1",
        "kind": "WorkflowTemplate",
        "workflowId": "analyze-document",
        "version": "1.0.0",
        "steps": steps,
    }


def test_workflow_template_supports_deterministic_and_agent_steps() -> None:
    workflow = WorkflowTemplate.model_validate(
        _template(
            [
                {"id": "extract", "kind": "skill", "target": "extract-document"},
                {
                    "id": "analyze",
                    "kind": "agent",
                    "target": "document-agent",
                    "dependsOn": ["extract"],
                    "retry": {"maximum_attempts": 2},
                },
                {
                    "id": "review",
                    "kind": "human-task",
                    "target": "expert-review",
                    "dependsOn": ["analyze"],
                },
            ]
        )
    )

    assert workflow.steps[1].depends_on == ("extract",)
    assert workflow.steps[1].retry.maximum_attempts == 2


def test_beta_workflow_supports_bindings_branches_and_parallelism() -> None:
    value = _template(
        [
            {"id": "classify", "kind": "agent", "target": "router"},
            {
                "id": "analyze",
                "kind": "agent",
                "target": "analyst",
                "dependsOn": ["classify"],
                "bindings": {
                    "document": {"source": "workflow-input", "path": "/document"},
                    "route": {
                        "source": "step-output",
                        "stepId": "classify",
                        "path": "/output/route",
                    },
                },
                "when": {
                    "source": "step-output",
                    "stepId": "classify",
                    "path": "/output/route",
                    "operator": "equals",
                    "value": "security",
                },
                "joinPolicy": "all-settled",
                "failurePolicy": "continue-with-errors",
            },
        ]
    )
    value["apiVersion"] = "sage.dev/v1beta1"
    value["maxConcurrency"] = 4

    workflow = WorkflowTemplate.model_validate(value)

    assert workflow.max_concurrency == 4
    assert workflow.steps[1].bindings["route"].step_id == "classify"
    assert workflow.steps[1].when is not None
    assert workflow.steps[1].join_policy == "all-settled"


def test_workflow_rejects_binding_to_non_dependency() -> None:
    value = _template(
        [
            {"id": "first", "kind": "agent", "target": "first"},
            {
                "id": "second",
                "kind": "agent",
                "target": "second",
                "bindings": {
                    "result": {"source": "step-output", "stepId": "first"}
                },
            },
        ]
    )
    value["apiVersion"] = "sage.dev/v1beta1"

    with pytest.raises(ValidationError, match="references unavailable outputs"):
        WorkflowTemplate.model_validate(value)


def test_workflow_rejects_invalid_output_schema() -> None:
    value = _template(
        [
            {
                "id": "analyze",
                "kind": "agent",
                "target": "analyst",
                "outputSchema": {"type": "not-a-json-schema-type"},
            }
        ]
    )
    value["apiVersion"] = "sage.dev/v1beta1"

    with pytest.raises(ValidationError, match="Invalid Workflow output schema"):
        WorkflowTemplate.model_validate(value)


def test_multi_agent_workflow_example_is_valid() -> None:
    workflow = load_workflow_template(
        ROOT / "docs" / "examples" / "multi-agent-workflow.yaml"
    )

    assert workflow.api_version == "sage.dev/v1beta1"
    assert workflow.max_concurrency == 3
    assert workflow.steps[4].join_policy == "all-settled"
    assert workflow.steps[5].output_schema["properties"]["message"]["enum"] == [
        "PASS",
        "REVISE",
        "ESCALATE",
    ]


def test_workflow_template_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="Unknown Workflow dependencies"):
        WorkflowTemplate.model_validate(
            _template(
                [
                    {
                        "id": "analyze",
                        "kind": "agent",
                        "target": "document-agent",
                        "dependsOn": ["missing"],
                    }
                ]
            )
        )


def test_workflow_template_rejects_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowTemplate.model_validate(
            _template(
                [
                    {
                        "id": "first",
                        "kind": "skill",
                        "target": "first",
                        "dependsOn": ["second"],
                    },
                    {
                        "id": "second",
                        "kind": "skill",
                        "target": "second",
                        "dependsOn": ["first"],
                    },
                ]
            )
        )