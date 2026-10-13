"""Common execution adapter contract."""

from collections.abc import Mapping
from typing import Any, Protocol

from sage.domain import RuntimeContext
from sage.models import SkillDefinition


class SkillAdapter(Protocol):
    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]: ...
