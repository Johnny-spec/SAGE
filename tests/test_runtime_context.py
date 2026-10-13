from dataclasses import FrozenInstanceError
from datetime import UTC

import pytest

from sage.domain import Principal, RuntimeContext


def test_runtime_context_generates_scoped_identifiers() -> None:
    principal = Principal.anonymous()

    context = RuntimeContext.create(mode="direct", principal=principal)

    assert context.run_id.startswith("run_")
    assert context.thread_id.startswith("thr_")
    assert context.generation_id == "gen_startup"
    assert context.principal.workspace_id == "ws_default"
    assert context.principal.has_role("user")
    assert context.created_at.tzinfo is UTC


def test_runtime_context_preserves_client_thread_and_server_request_ids() -> None:
    context = RuntimeContext.create(
        mode="agent",
        principal=Principal(
            workspace_id="ws_team",
            principal_id="usr_123",
            roles=frozenset({"user", "capability_admin"}),
            authentication_method="local",
        ),
        thread_id="thread-existing",
        run_id="run-existing",
        generation_id="gen_42",
        solution_id="incident-diagnostics",
        solution_release_id="incident-diagnostics@1.0.0",
        request_id="req_123",
    )

    assert context.run_id == "run-existing"
    assert context.thread_id == "thread-existing"
    assert context.request_id == "req_123"
    assert context.solution_id == "incident-diagnostics"
    assert context.solution_release_id == "incident-diagnostics@1.0.0"
    assert context.principal.has_role("capability_admin")


def test_runtime_context_is_immutable() -> None:
    context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())

    with pytest.raises(FrozenInstanceError):
        context.thread_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["workspace_id", "principal_id"])
def test_principal_rejects_empty_identifiers(field: str) -> None:
    values = {"workspace_id": "ws_default", "principal_id": "usr_123"}
    values[field] = " "

    with pytest.raises(ValueError, match=field):
        Principal(**values)
