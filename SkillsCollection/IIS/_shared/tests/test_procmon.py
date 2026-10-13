"""IIS wrapper tests for the Common Procmon analyzer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_iis_procmon_wrapper_preserves_local_skill_identity(project_root: Path, tmp_path: Path):
    trace = tmp_path / "trace.csv"
    trace.write_text(
        "Time of Day,Process Name,Parent PID,PID,TID,Operation,Result,Path,Detail\n"
        + (
            "5:08:40 PM,w3wp.exe,4,100,200,CreateFile,ACCESS DENIED,"
            "C:\\inetpub\\wwwroot\\web.config,Desired Access: Read\n"
        ) * 25,
        encoding="utf-8",
    )
    script = project_root / "procmon" / "scripts" / "procmon_analyzer.py"
    context = {
        "problem_type": "5xx_error",
        "question": "IIS w3wp returns 500 while reading web.config",
        "time_range": {
            "start": "2026-08-20T17:08:39",
            "end": "2026-08-20T17:08:41",
        },
        "extra": {"procmon_paths": [str(trace)]},
    }

    process = subprocess.run(
        [sys.executable, str(script), json.dumps(context)],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
    )
    result = json.loads(process.stdout)

    assert process.returncode == 0, process.stderr
    assert result["skill"] == "procmon"
    assert result["root_cause"] is None
    assert result["raw"]["candidate_cause"]