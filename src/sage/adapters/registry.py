"""Registry for execution adapters enabled by the application composition root."""

from collections.abc import Mapping
from types import MappingProxyType

from sage.adapters.base import SkillAdapter


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, SkillAdapter]) -> None:
        normalized: dict[str, SkillAdapter] = {}
        for execution_type, adapter in adapters.items():
            key = execution_type.strip()
            if not key:
                raise ValueError("execution type must not be empty")
            if key in normalized:
                raise ValueError(f"duplicate execution adapter: {key}")
            normalized[key] = adapter
        self._adapters = MappingProxyType(normalized)

    @property
    def enabled_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def get(self, execution_type: str) -> SkillAdapter | None:
        return self._adapters.get(execution_type)
