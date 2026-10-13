"""Adapter for administrator-registered remote HTTP Skills."""

from collections.abc import Mapping
from typing import Any

import httpx

from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.gateways.http import HttpEndpointRegistry
from sage.models import SkillDefinition


class HttpAdapter:
    def __init__(
        self,
        endpoints: HttpEndpointRegistry,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._transport = transport

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        alias = definition.manifest.spec.execution.endpoint_alias
        endpoint = self._endpoints.get(alias or "")
        if endpoint is None:
            raise ExecutionError(f"HTTP endpoint alias is not configured: {alias}")
        headers = {
            "X-Sage-Run-Id": context.run_id,
            "X-Sage-Workspace-Id": context.principal.workspace_id,
            "X-Sage-Principal-Id": context.principal.principal_id,
            "X-Sage-Generation-Id": context.generation_id,
        }
        try:
            async with httpx.AsyncClient(
                timeout=endpoint.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    endpoint.url,
                    headers=headers,
                    json={"arguments": dict(arguments)},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError(f"HTTP Skill request failed: {alias}") from exc
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ExecutionError(f"HTTP Skill returned an invalid result envelope: {alias}")
        return result
