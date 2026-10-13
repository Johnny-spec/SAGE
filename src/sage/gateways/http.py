"""Governed aliases for remote HTTP Skill endpoints."""

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class HttpEndpoint:
    alias: str
    url: str
    timeout_seconds: float = 30.0
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if not self.alias or not self.alias.strip():
            raise ValueError("HTTP endpoint alias must not be empty")
        parsed = urlsplit(self.url)
        allowed_schemes = {"https", "http"} if self.allow_insecure_http else {"https"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            raise ValueError("HTTP endpoint must use an allowed absolute URL")
        if parsed.username or parsed.password:
            raise ValueError("HTTP endpoint URL must not contain credentials")
        if parsed.fragment:
            raise ValueError("HTTP endpoint URL must not contain a fragment")
        if self.timeout_seconds <= 0:
            raise ValueError("HTTP endpoint timeout must be positive")


class HttpEndpointRegistry:
    def __init__(self, endpoints: Iterable[HttpEndpoint]) -> None:
        by_alias: dict[str, HttpEndpoint] = {}
        for endpoint in endpoints:
            if endpoint.alias in by_alias:
                raise ValueError(f"duplicate HTTP endpoint alias: {endpoint.alias}")
            by_alias[endpoint.alias] = endpoint
        self._endpoints = MappingProxyType(by_alias)

    def get(self, alias: str) -> HttpEndpoint | None:
        return self._endpoints.get(alias)
