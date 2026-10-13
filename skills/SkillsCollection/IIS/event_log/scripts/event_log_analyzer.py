#!/usr/bin/env python3
"""Compatibility wrapper for the common IIS event log analyzer implementation."""
from __future__ import annotations

import importlib.util
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
        / "event_log"
        / "iis_event_log_analyzer_common.py"
    )


def _load_common_module() -> object:
    product_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("COMMON_SKILL_PRODUCT_ROOT", str(product_root))
    script = _common_script()
    spec = importlib.util.spec_from_file_location("common_iis_event_log_analyzer", str(script))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load common analyzer module: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COMMON = _load_common_module()
for _name, _value in vars(_COMMON).items():
    if _name.startswith("__") and _name not in {"__all__"}:
        continue
    globals()[_name] = _value


if __name__ == "__main__":
    if hasattr(_COMMON, "main"):
        raise SystemExit(_COMMON.main())
    runpy.run_path(str(_common_script()), run_name="__main__")
