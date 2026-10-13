"""Native MCP tool contracts and policy-scoped execution snapshots."""

import asyncio
import json
from collections.abc import Collection, Mapping
from typing import Any, Literal
from urllib.parse import quote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage.digests import canonical_digest
from sage.domain import CapabilityDescriptor, RuntimeContext
from sage.errors import ExecutionError
from sage.gateways import McpGatewayClient, McpGatewayError, McpToolCall
from sage.policy import PolicyEnforcer


class McpToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = Field(min_length=1)
    server_alias: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    server_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def tool_id(self) -> str:
        return f"tool:mcp:{quote(self.server_id, safe='')}:{quote(self.tool_name, safe='')}"


class McpToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    binding: McpToolBinding
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risk: Literal["read-only", "write-low", "write-high", "destructive"] = "read-only"
    timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    max_output_bytes: int = Field(default=262_144, ge=1)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schemas(self) -> "McpToolDefinition":
        try:
            Draft202012Validator.check_schema(self.input_schema)
            if self.output_schema is not None:
                Draft202012Validator.check_schema(self.output_schema)
        except SchemaError as exc:
            raise ValueError(f"Invalid tool schema: {exc.message}") from exc
        return self

    @property
    def tool_id(self) -> str:
        return self.binding.tool_id

    @property
    def content_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=self.tool_id,
            name=self.binding.tool_name,
            kind="mcp-tool",
            version=self.version,
            description=self.description,
            risk=self.risk,
        )


class ToolExecutor:
    def __init__(
        self,
        definitions: Collection[McpToolDefinition],
        *,
        generation_id: str,
        gateway: McpGatewayClient,
        allowed_tool_ids: Collection[str],
        policy: PolicyEnforcer | None = None,
    ) -> None:
        self._definitions: dict[str, McpToolDefinition] = {}
        for definition in definitions:
            if definition.tool_id in self._definitions:
                raise ValueError(f"Duplicate tool: {definition.tool_id}")
            self._definitions[definition.tool_id] = definition.model_copy(deep=True)
        self._generation_id = generation_id
        self._allowed_ids = frozenset(allowed_tool_ids)
        if not self._allowed_ids <= self._definitions.keys():
            raise ValueError("Allowed tools must be present in the execution snapshot")
        self._gateway = gateway
        self._policy = policy or PolicyEnforcer()

    async def execute(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        if context.generation_id != self._generation_id:
            raise ExecutionError("Tool generation does not match the run")
        if tool_id not in self._allowed_ids:
            raise ExecutionError("Tool is not in the run capability view")
        definition = self._definitions[tool_id]
        if not definition.enabled:
            raise ExecutionError("Tool execution is disabled")
        self._policy.require(context, definition.descriptor, "execute")
        try:
            Draft202012Validator(definition.input_schema).validate(dict(arguments))
        except ValidationError as exc:
            raise ExecutionError(f"Invalid Tool arguments: {exc.message}") from exc
        binding = definition.binding
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                result = await self._gateway.invoke(
                    McpToolCall(
                        server_alias=binding.server_alias,
                        tool_name=binding.tool_name,
                        arguments=dict(arguments),
                        context=context,
                        expected_schema_digest=binding.schema_digest,
                        expected_server_config_digest=binding.server_config_digest,
                    )
                )
        except TimeoutError as exc:
            raise ExecutionError("Tool timed out") from exc
        except McpGatewayError as exc:
            raise ExecutionError(f"MCP Gateway invocation failed: {exc}") from exc
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ExecutionError("Tool result is not valid JSON") from exc
        if len(encoded) > definition.max_output_bytes:
            raise ExecutionError("Tool output is too large")
        if not isinstance(result, dict):
            raise ExecutionError("Tool result must be an object")
        if definition.output_schema is not None:
            try:
                Draft202012Validator(definition.output_schema).validate(result)
            except ValidationError as exc:
                raise ExecutionError(f"Invalid Tool result: {exc.message}") from exc
        return result