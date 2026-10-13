#!/usr/bin/env python3
"""Thin wrapper to run the common Edge netlog analyzer implementation."""
from __future__ import annotations

import os
import runpy
from pathlib import Path


def _common_script() -> Path:
    # Product root: .../Edge
    product_root = Path(__file__).resolve().parents[2]
    common_script = (
        product_root.parent
        / "Common"
        / "log_skills"
        / "skills"
        / "net-export"
        / "edge_netlog_analyzer_common.py"
    )
    return common_script


if __name__ == "__main__":
    os.environ.setdefault("COMMON_SKILL_PRODUCT_ROOT", str(Path(__file__).resolve().parents[2]))
    runpy.run_path(str(_common_script()), run_name="__main__")
