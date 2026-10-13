import importlib.util
import sys
from pathlib import Path

WRAPPER_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "skills-collection"
    / "scripts"
    / "orchestrator_wrapper.py"
)
NETLOG_FIXTURE = Path(__file__).parent / "fixtures" / "netlog" / "auth_failure.json"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("test_skills_collection_wrapper", WRAPPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wrapper_maps_uploaded_netlog_and_child_to_manager_context() -> None:
    wrapper = _load_wrapper()

    payload, folder = wrapper._context(
        "Browser authentication failed",
        NETLOG_FIXTURE,
        "edge_netlog",
    )

    assert folder == NETLOG_FIXTURE.parent
    assert payload["question"] == "Browser authentication failed"
    assert payload["extra"]["netlog_paths"] == [str(NETLOG_FIXTURE)]
    assert payload["extra"]["incident_mode"] == "offline_bundle"
    assert payload["extra"]["skip_validation"] is True
    assert payload["skills_to_trigger"] == ["edge_netlog"]


def test_wrapper_rejects_restricted_child() -> None:
    wrapper = _load_wrapper()

    try:
        wrapper._validate_child("IIS", "security_audit")
    except ValueError as exc:
        assert "not enabled" in str(exc)
    else:
        raise AssertionError("restricted child must be rejected")
