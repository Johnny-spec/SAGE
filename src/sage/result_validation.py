"""Named semantic validators applied after generic output schema validation."""

from collections.abc import Callable, Mapping
from typing import Any

from sage.errors import ExecutionError
from sage.models import SkillDefinition

ResultValidator = Callable[[SkillDefinition, Mapping[str, Any]], None]


class ResultValidatorRegistry:
    def __init__(self, validators: Mapping[str, ResultValidator] | None = None) -> None:
        self._validators = dict(validators or {})

    def validate(
        self,
        definition: SkillDefinition,
        result: Mapping[str, Any],
    ) -> None:
        validator_name = definition.manifest.spec.policy.result_validator
        if validator_name is None:
            return
        validator = self._validators.get(validator_name)
        if validator is None:
            raise ExecutionError(f"Result validator is not enabled: {validator_name}")
        validator(definition, result)
