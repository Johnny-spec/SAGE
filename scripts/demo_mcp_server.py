#!/usr/bin/env python3
"""Local Streamable HTTP MCP server for the SAGE control-plane demo."""

import argparse

from mcp.server.fastmcp import FastMCP

_RUNBOOKS = {
    "iis": {
        "owner": "Web Platform",
        "steps": [
            "Confirm the failing status and substatus codes.",
            "Compare time-taken and request volume with the baseline.",
            "Correlate the request window with HTTPERR and event logs.",
        ],
    },
    "edge": {
        "owner": "Browser Support",
        "steps": [
            "Capture a Chromium net-export trace.",
            "Check proxy, DNS, TLS, and authentication events.",
            "Compare policy state with the managed baseline.",
        ],
    },
}


def calculate_total(
    unit_price: float,
    quantity: int,
    discount_percent: float = 0.0,
) -> dict[str, float | int]:
    """Calculate a deterministic order total after a percentage discount."""

    if unit_price < 0:
        raise ValueError("unit_price must be non-negative")
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("discount_percent must be between 0 and 100")
    subtotal = round(unit_price * quantity, 2)
    discount = round(subtotal * discount_percent / 100, 2)
    return {
        "unit_price": unit_price,
        "quantity": quantity,
        "discount_percent": discount_percent,
        "subtotal": subtotal,
        "discount": discount,
        "total": round(subtotal - discount, 2),
    }


def lookup_runbook(service: str) -> dict[str, object]:
    """Return a small deterministic troubleshooting runbook for a demo service."""

    normalized = service.strip().casefold()
    runbook = _RUNBOOKS.get(normalized)
    if runbook is None:
        return {
            "service": normalized,
            "found": False,
            "available_services": sorted(_RUNBOOKS),
        }
    return {"service": normalized, "found": True, **runbook}


def create_demo_server(host: str, port: int) -> FastMCP:
    server = FastMCP(
        "SAGE Demo MCP",
        instructions="Deterministic local tools used to validate SAGE MCP governance.",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    server.tool(structured_output=True)(calculate_total)
    server.tool(structured_output=True)(lookup_runbook)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    create_demo_server(arguments.host, arguments.port).run(transport="streamable-http")


if __name__ == "__main__":
    main()
