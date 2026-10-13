"""Schema-validated Skill execution."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from jsonschema import ValidationError, validate

from sage.adapters import AdapterRegistry, BuiltinAdapter, SubprocessAdapter
from sage.capabilities import skill_descriptor
from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.models import SkillDefinition
from sage.policy import PolicyEnforcer
from sage.result_validation import ResultValidatorRegistry


class SkillExecutor:
    def __init__(
        self,
        adapter_registry: AdapterRegistry | None = None,
        *,
        builtin_adapter: BuiltinAdapter | None = None,
        subprocess_adapter: SubprocessAdapter | None = None,
        policy_enforcer: PolicyEnforcer | None = None,
        result_validators: ResultValidatorRegistry | None = None,
    ) -> None:
        if adapter_registry is not None and (
            builtin_adapter is not None or subprocess_adapter is not None
        ):
            raise ValueError(
                "adapter_registry cannot be combined with individual adapter overrides"
            )
        self._adapters = adapter_registry or AdapterRegistry(
            {
                "builtin": builtin_adapter or BuiltinAdapter(),
                "python-subprocess": subprocess_adapter or SubprocessAdapter(),
            }
        )
        self._policy = policy_enforcer or PolicyEnforcer()
        self._result_validators = result_validators or ResultValidatorRegistry()

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        spec = definition.manifest.spec
        if not spec.policy.execution_enabled:
            raise ExecutionError(f"Skill execution is disabled: {definition.name}")
        self._policy.require(context, skill_descriptor(definition), "execute")
        if spec.execution.type in {"workflow", "model"}:
            raise ExecutionError("durable_run_required: use POST /v1/durable-runs for this Skill")
        try:
            validate(instance=dict(arguments), schema=spec.input_schema)
        except ValidationError as exc:
            raise ExecutionError(f"Invalid Skill arguments: {exc.message}") from exc

        adapter = self._adapters.get(spec.execution.type)
        if adapter is None:
            raise ExecutionError(f"Execution type is not enabled yet: {spec.execution.type}")

        try:
            async with asyncio.timeout(spec.policy.timeout_seconds):
                result = await adapter.execute(definition, arguments, context=context)
        except TimeoutError as exc:
            raise ExecutionError(f"Skill timed out: {definition.name}") from exc

        return self.validate_result(definition, result)

    def validate_result(
        self, definition: SkillDefinition, result: dict[str, Any]
    ) -> dict[str, Any]:
        spec = definition.manifest.spec
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > spec.policy.max_output_bytes:
            raise ExecutionError(f"Skill output is too large: {definition.name}")
        try:
            validate(instance=result, schema=spec.output_schema)
        except ValidationError as exc:
            raise ExecutionError(f"Invalid Skill result: {exc.message}") from exc
        self._result_validators.validate(definition, result)
        return result
