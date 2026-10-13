import pytest

from scripts.demo_mcp_server import calculate_total, lookup_runbook


def test_calculate_total_returns_structured_result() -> None:
    assert calculate_total(25.0, 4, 10.0) == {
        "unit_price": 25.0,
        "quantity": 4,
        "discount_percent": 10.0,
        "subtotal": 100.0,
        "discount": 10.0,
        "total": 90.0,
    }


def test_calculate_total_rejects_invalid_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        calculate_total(10.0, 0)


def test_lookup_runbook_lists_unknown_services() -> None:
    result = lookup_runbook("unknown")

    assert result["found"] is False
    assert result["available_services"] == ["edge", "iis"]
