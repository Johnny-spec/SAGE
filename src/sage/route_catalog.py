"""Domain-neutral route choices for registered Skills."""

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from sage.models import SkillDefinition

RouteAvailability = Literal["available", "restricted", "internal"]

class RouteChoice(BaseModel):
    id: str
    label: str
    path: list[str]
    target_skill: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    selectable: bool = True
    availability: RouteAvailability = "available"
    description: str
    risk: str = "read-only"


def build_route_catalog(
    definitions: Iterable[SkillDefinition],
) -> list[RouteChoice]:
    return [
        RouteChoice(
            id=definition.name,
            label=definition.name,
            path=[definition.name],
            target_skill=definition.name,
            description=definition.description,
            risk=definition.manifest.spec.policy.risk,
        )
        for definition in definitions
    ]
