"""Skill execution adapters."""

from sage.adapters.builtin import BuiltinAdapter
from sage.adapters.http import HttpAdapter
from sage.adapters.mcp import McpAdapter
from sage.adapters.registry import AdapterRegistry
from sage.adapters.runner import HttpRunnerClient, RunnerAdapter
from sage.adapters.subprocess import SubprocessAdapter

__all__ = [
    "AdapterRegistry",
    "BuiltinAdapter",
    "HttpAdapter",
    "McpAdapter",
    "HttpRunnerClient",
    "RunnerAdapter",
    "SubprocessAdapter",
]
