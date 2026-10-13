import pytest

from sage.control_settings import ControlPlaneSettings
from sage.mcp_control import McpControlError, OfficialMcpConnector


def test_local_http_mcp_requires_explicit_dev_mode() -> None:
    connector = OfficialMcpConnector(
        ControlPlaneSettings(
            mcp_allowed_hosts=frozenset({"127.0.0.1:8765"}),
        )
    )

    with pytest.raises(McpControlError, match="loopback DEV mode"):
        connector._validated_url("http://127.0.0.1:8765/mcp")


def test_local_http_mcp_accepts_allowlisted_loopback_in_dev_mode() -> None:
    connector = OfficialMcpConnector(
        ControlPlaneSettings(
            allow_insecure_local_mcp=True,
            mcp_allowed_hosts=frozenset({"127.0.0.1:8765"}),
        )
    )

    assert connector._validated_url("http://127.0.0.1:8765/mcp") == "http://127.0.0.1:8765/mcp"


def test_https_mcp_can_skip_host_allowlist_in_development() -> None:
    connector = OfficialMcpConnector(
        ControlPlaneSettings(mcp_enforce_allowed_hosts=False)
    )

    assert connector._validated_url("https://example.com/mcp") == "https://example.com/mcp"
