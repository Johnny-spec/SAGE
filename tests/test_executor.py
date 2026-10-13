import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from sage.adapters import AdapterRegistry, BuiltinAdapter
from sage.app import DEFAULT_SKILLS_ROOT
from sage.domain import Principal, RuntimeContext
from sage.errors import PolicyDeniedError
from sage.executor import SkillExecutor
from sage.models import SkillPolicy
from sage.registry import SkillRegistry


def test_executor_passes_runtime_context_to_adapter() -> None:
    observed_contexts: list[RuntimeContext] = []

    def record_context(
        arguments: Mapping[str, Any],
        context: RuntimeContext,
    ) -> dict[str, Any]:
        observed_contexts.append(context)
        return {"text": str(arguments["text"])}

    definition = next(
        definition
        for definition in SkillRegistry.load(DEFAULT_SKILLS_ROOT)
        if definition.name == "echo"
    )
    executor = SkillExecutor(builtin_adapter=BuiltinAdapter({"echo": record_context}))
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_team",
            principal_id="usr_123",
            roles=frozenset({"user"}),
            authentication_method="local",
        ),
        thread_id="thr_existing",
        run_id="run_existing",
        generation_id="gen_7",
    )

    result = asyncio.run(executor.execute(definition, {"text": "hello"}, context=context))

    assert result == {"text": "hello"}
    assert observed_contexts == [context]


def test_executor_dispatches_through_injected_adapter_registry() -> None:
    def replacement_handler(
        arguments: Mapping[str, Any],
        context: RuntimeContext,
    ) -> dict[str, Any]:
        return {"text": f"{context.generation_id}:{arguments['text']}"}

    definition = next(
        definition
        for definition in SkillRegistry.load(DEFAULT_SKILLS_ROOT)
        if definition.name == "echo"
    )
    registry = AdapterRegistry({"builtin": BuiltinAdapter({"echo": replacement_handler})})
    executor = SkillExecutor(adapter_registry=registry)
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        generation_id="gen_custom",
    )

    result = asyncio.run(executor.execute(definition, {"text": "hello"}, context=context))

    assert registry.enabled_types == ("builtin",)
    assert result == {"text": "gen_custom:hello"}


def test_executor_rechecks_policy_before_adapter_invocation() -> None:
    called = False

    def forbidden_handler(
        arguments: Mapping[str, Any],
        context: RuntimeContext,
    ) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"text": str(arguments["text"])}

    definition = next(
        definition
        for definition in SkillRegistry.load(DEFAULT_SKILLS_ROOT)
        if definition.name == "echo"
    )
    spec = definition.manifest.spec.model_copy(update={"policy": SkillPolicy(risk="write-low")})
    manifest = definition.manifest.model_copy(update={"spec": spec})
    write_definition = definition.model_copy(update={"manifest": manifest})
    executor = SkillExecutor(builtin_adapter=BuiltinAdapter({"echo": forbidden_handler}))
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal(
            workspace_id="ws_default",
            principal_id="usr_regular",
            roles=frozenset({"user"}),
            authentication_method="local",
        ),
    )

    with pytest.raises(PolicyDeniedError, match="write_capability_denied"):
        asyncio.run(executor.execute(write_definition, {"text": "blocked"}, context=context))

    assert called is False
