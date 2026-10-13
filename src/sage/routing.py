"""Deterministic, explainable message routing."""

from dataclasses import dataclass

from sage.api_models import RouteMethod
from sage.capabilities import CapabilityView
from sage.errors import TargetNotFoundError
from sage.models import SkillDefinition


@dataclass(frozen=True, slots=True)
class RouteDecision:
    definition: SkillDefinition
    method: RouteMethod
    reason: str


class RuleRouter:
    def route(
        self,
        capabilities: CapabilityView,
        message: str,
        explicit_skill: str | None = None,
        attachment_types: tuple[str, ...] = (),
    ) -> RouteDecision | None:
        if explicit_skill is not None:
            definition = capabilities.get_skill(explicit_skill)
            if definition is None:
                raise TargetNotFoundError(f"Skill not found: {explicit_skill}")
            return RouteDecision(
                definition=definition,
                method="explicit",
                reason="The client explicitly selected this Skill.",
            )

        normalized = message.casefold()
        scored: list[tuple[int, SkillDefinition, list[str], list[str]]] = []
        for definition in capabilities.skills:
            matches = [keyword for keyword in definition.route_keywords if keyword in normalized]
            accepted = {
                item.casefold() for item in definition.manifest.spec.routing.attachment_types
            }
            attachment_matches = [item for item in attachment_types if item.casefold() in accepted]
            if matches or attachment_matches:
                keyword_score = sum(len(keyword) * 100 for keyword in matches)
                attachment_score = len(attachment_matches) * 10
                scored.append(
                    (keyword_score + attachment_score, definition, matches, attachment_matches)
                )
        if not scored:
            return None

        scored.sort(key=lambda item: (-item[0], item[1].name))
        best_score, definition, matches, attachment_matches = scored[0]
        if len(scored) > 1 and scored[1][0] == best_score:
            return None
        reasons = []
        if matches:
            reasons.append(f"keywords: {', '.join(matches)}")
        if attachment_matches:
            reasons.append(f"attachments: {', '.join(attachment_matches)}")
        return RouteDecision(
            definition=definition,
            method="rule",
            reason=f"Matched routing {'; '.join(reasons)}",
        )
