from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_module(project_root: Path):
    path = project_root / "edge_crash" / "scripts" / "edge_crash_analyzer.py"
    spec = importlib.util.spec_from_file_location("edge_crash_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _context(user_data: Path) -> str:
    now = datetime.now()
    return json.dumps({
        "time_range": {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        "extra": {"user_data_dir": str(user_data)},
    })


def test_repeated_module_hint_is_not_promoted_to_root_cause(
    project_root: Path, tmp_path: Path, capsys
):
    module = _load_module(project_root)
    reports = tmp_path / "Crashpad" / "reports"
    reports.mkdir(parents=True)
    for index in range(3):
        dump = reports / f"crash-{index}.dmp"
        dump.write_bytes(b"header d3d11.dll tail")
        os.utime(dump, None)

    assert module.main([_context(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["root_cause"] is None
    assert result["evidence_state"] != "ROOT_CAUSE_FOUND"
    assert "d3d11" in result["raw"]["candidate_cause"]
    assert result["raw"]["quality"]["direct_join"] is False
    assert any("hypothesis" in item["summary"] for item in result["findings"])


def test_missing_reports_directory_is_missing_evidence(
    project_root: Path, tmp_path: Path, capsys
):
    module = _load_module(project_root)

    assert module.main([_context(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["root_cause"] is None
    assert result["evidence_state"] in {
        "MISSING_REQUIRED_EVIDENCE", "COLLECTION_PLAN_READY"
    }
    assert result["raw"]["quality"]["capture_complete"] is False
    assert "No Edge crashes" not in json.dumps(result)