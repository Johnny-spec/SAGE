from __future__ import annotations

from quality_gate import run_static_checks


def test_repository_static_quality_gate_passes():
    result = run_static_checks()

    assert result["ok"] is True, result["errors"]
    assert result["checks"]["schema_mirrors"] == []
    assert result["checks"]["product_contracts"] == []
    assert result["checks"]["manager_registry"] == []
    assert result["checks"]["manager_report_runtime"] == []