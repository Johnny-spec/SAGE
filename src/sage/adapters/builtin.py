"""In-process handlers owned and reviewed by the SAGE application."""

from collections.abc import Callable, Mapping
from typing import Any

from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.models import SkillDefinition

BuiltinHandler = Callable[[Mapping[str, Any], RuntimeContext], dict[str, Any]]


def _echo(arguments: Mapping[str, Any], context: RuntimeContext) -> dict[str, Any]:
    del context
    return {"text": str(arguments["text"])}


class BuiltinAdapter:
    def __init__(self, handlers: Mapping[str, BuiltinHandler] | None = None) -> None:
        self._handlers = {"echo": _echo}
        if handlers is not None:
            self._handlers.update(handlers)

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        handler_name = definition.manifest.spec.execution.handler
        if handler_name is None or handler_name not in self._handlers:
            raise ExecutionError(f"Unknown built-in handler: {handler_name}")
        return self._handlers[handler_name](arguments, context)
