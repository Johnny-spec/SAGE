import asyncio
from pathlib import Path

import httpx
import pytest

from sage.adapters import AdapterRegistry, HttpAdapter
from sage.domain import Principal, RuntimeContext
from sage.executor import SkillExecutor
from sage.gateways import HttpEndpoint, HttpEndpointRegistry
from sage.models import SkillDefinition, SkillManifest


def _definition(tmp_path: Path) -> SkillDefinition:
    manifest = SkillManifest.model_validate(
        {
            "apiVersion": "sage.dev/v1alpha1",
            "kind": "SkillBinding",
            "metadata": {"name": "remote-summary", "version": "1.0.0"},
            "spec": {
                "execution": {"type": "http", "endpointAlias": "summary-service"},
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
        }
    )
    return SkillDefinition(
        name="remote-summary",
        description="Summarize text remotely",
        version="1.0.0",
        package_root=tmp_path,
        manifest=manifest,
    )


def test_http_adapter_uses_registered_endpoint_and_run_scope(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {"summary": "done"}})

    endpoints = HttpEndpointRegistry(
        (HttpEndpoint(alias="summary-service", url="https://tools.example/run"),)
    )
    executor = SkillExecutor(
        adapter_registry=AdapterRegistry(
            {
                "http": HttpAdapter(
                    endpoints,
                    transport=httpx.MockTransport(handle),
                )
            }
        )
    )
    context = RuntimeContext.create(
        mode="direct",
        principal=Principal.anonymous(),
        run_id="run_http",
    )

    result = asyncio.run(
        executor.execute(_definition(tmp_path), {"text": "hello"}, context=context)
    )

    assert result == {"summary": "done"}
    assert requests[0].url == "https://tools.example/run"
    assert requests[0].headers["X-Sage-Run-Id"] == "run_http"
    assert requests[0].content == b'{"arguments":{"text":"hello"}}'


def test_http_endpoint_rejects_insecure_url_by_default() -> None:
    with pytest.raises(ValueError, match="allowed absolute URL"):
        HttpEndpoint(alias="unsafe", url="http://example.test/run")


def test_http_binding_requires_endpoint_alias() -> None:
    with pytest.raises(ValueError, match="endpointAlias"):
        SkillManifest.model_validate(
            {
                "apiVersion": "sage.dev/v1alpha1",
                "kind": "SkillBinding",
                "metadata": {"name": "invalid", "version": "1.0.0"},
                "spec": {
                    "execution": {"type": "http"},
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                },
            }
        )
