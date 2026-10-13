from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def manager_module():
    path = ROOT / "orchestrator" / "scripts" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("root_manager_orchestrator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module