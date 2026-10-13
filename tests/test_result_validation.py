import pytest

from sage.app import DEFAULT_SKILLS_ROOT
from sage.errors import ExecutionError
from sage.reference_solutions.incident_diagnostics import require_nonempty_iis_metrics
from sage.registry import SkillRegistry
from sage.result_validation import ResultValidatorRegistry


def test_iis_manifest_selects_nonempty_metrics_validator() -> None:
    definition = SkillRegistry.load(DEFAULT_SKILLS_ROOT).get("iis-logs")
    assert definition is not None

    with pytest.raises(ExecutionError, match="no parseable IIS W3C"):
        ResultValidatorRegistry(
            {"iis-w3c-nonempty": require_nonempty_iis_metrics}
        ).validate(
            definition,
            {"raw": {"metrics": {"total_requests": 0}}},
        )


def test_skill_without_named_validator_has_no_domain_specific_gate() -> None:
    definition = SkillRegistry.load(DEFAULT_SKILLS_ROOT).get("echo")
    assert definition is not None

    ResultValidatorRegistry().validate(definition, {"text": "ok"})
