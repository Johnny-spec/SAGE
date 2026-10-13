from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

COMMON_ROOT = Path(__file__).resolve().parents[1]
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))


def load_script(name: str, relative: str):
    path = COMMON_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def source_discovery_module():
    return load_script(
        "common_source_logging_discovery",
        "log_skills/skills/source-guided-discovery/source_guided_logging_discovery.py",
    )


@pytest.fixture(scope="session")
def validator_module():
    return load_script(
        "common_evidence_validator",
        "log_skills/skills/validator/validator.py",
    )