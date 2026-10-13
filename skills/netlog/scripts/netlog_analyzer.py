#!/usr/bin/env python3
"""SAGE wrapper for the vendored Chromium NetLog analyzer."""

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType


def _load_analyzer() -> ModuleType:
    skills_root = Path(__file__).resolve().parents[2]
    analyzer_path = (
        skills_root
        / "iis-logs"
        / "vendor"
        / "Common"
        / "log_skills"
        / "skills"
        / "net-export"
        / "iis_netlog_analyzer_common.py"
    )
    spec = importlib.util.spec_from_file_location("sage_vendored_netlog", analyzer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load the vendored NetLog analyzer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlog_path")
    arguments = parser.parse_args()
    netlog_path = Path(arguments.netlog_path)
    if not netlog_path.is_file():
        parser.error("netlog_path must be an uploaded file")

    analyzer = _load_analyzer()
    result = analyzer.analyse({"extra": {"netlog_paths": [str(netlog_path)]}})
    print(result.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
