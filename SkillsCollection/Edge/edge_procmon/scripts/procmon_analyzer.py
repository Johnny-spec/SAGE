#!/usr/bin/env python3
"""Thin Edge wrapper for the Common Procmon analyzer."""
from __future__ import annotations

import os
import runpy
from pathlib import Path


def _common_script() -> Path:
    product_root = Path(__file__).resolve().parents[2]
    return (
        product_root.parent
        / "Common"
        / "log_skills"
        / "skills"
        / "procmon"
        / "procmon_analyzer_common.py"
    )


if __name__ == "__main__":
    product_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("COMMON_SKILL_PRODUCT_ROOT", str(product_root))
    os.environ.setdefault("COMMON_PROCMON_SKILL_ID", "edge_procmon")
    runpy.run_path(str(_common_script()), run_name="__main__")