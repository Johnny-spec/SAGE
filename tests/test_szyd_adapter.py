import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from sage.adapters.subprocess import SubprocessAdapter
from sage.adapters.szyd import SzydAdapter
from sage.attachments import AttachmentStore
from sage.domain import Principal, RuntimeContext
from sage.errors import AttachmentNotFoundError, ExecutionError
from sage.registry import SkillRegistry
from sage.szyd_state import SzydCaseStore

ROOT = Path(__file__).parents[1] / "skills/szyd-law"


def call(adapter, context, **arguments):
    return asyncio.run(
        adapter.execute(SkillRegistry.load_definition(ROOT), arguments, context=context)
    )


def test_adapter_persists_wait_and_resumes_after_restart(tmp_path):
    context = RuntimeContext.create(
        mode="agent", principal=Principal.anonymous(), thread_id="thread", agent_id="szyd-agent"
    )
    database = tmp_path / "cases.sqlite3"
    adapter = SzydAdapter(SubprocessAdapter(), SzydCaseStore(database))
    opened = call(adapter, context, operation="open", message="Review investment")
    arguments = {
        "operation": "stage",
        "expected_revision": opened["revision"],
        "payload": {
            "name": "identification",
            "stage": {
                "version": 1,
                "status": "INSUFFICIENT",
                "data": {"is_major": None, "matched_categories": [], "matched_items": []},
                "reasoning": "Need amount",
                "needs": ["Amount"],
                "evidence_refs": [],
            },
        },
    }
    waiting = call(adapter, context, **arguments)
    assert waiting["decision"]["action"] == "SUPPLEMENT"
    assert call(adapter, context, **arguments) == waiting
    with pytest.raises(ExecutionError, match="later user turn"):
        call(adapter, context, operation="resume", expected_revision=2, message="Amount 10")
    restarted = SzydAdapter(SubprocessAdapter(), SzydCaseStore(database))
    next_turn = replace(context, run_id="next", root_run_id="next")
    assert call(restarted, next_turn, operation="open")["case_id"] == opened["case_id"]
    resumed = call(
        restarted, next_turn, operation="resume", expected_revision=2, message="Amount 10 million"
    )
    assert resumed["state"]["supplement_round"] == 1
    assert resumed["state"]["version"] == 2
    assert resumed["revision"] == 3
    with pytest.raises(ExecutionError, match="server-owned"):
        call(restarted, next_turn, operation="open", state={})
    with pytest.raises(ExecutionError, match="not found"):
        call(
            restarted,
            replace(next_turn, thread_id="other"),
            operation="open",
            case_id=opened["case_id"],
        )


def test_adapter_delegates_other_skills_unchanged(tmp_path):
    class Delegate:
        async def execute(self, definition, arguments, *, context):
            return {"arguments": dict(arguments)}

    definition = SkillRegistry.load_definition(ROOT).model_copy(update={"name": "another"})
    adapter = SzydAdapter(Delegate(), SzydCaseStore(tmp_path / "cases.sqlite3"))
    context = RuntimeContext.create(mode="direct", principal=Principal.anonymous())
    assert asyncio.run(adapter.execute(definition, {"input": 1}, context=context)) == {
        "arguments": {"input": 1},
    }


def test_artifacts_are_resolved_again_with_solution_scope(tmp_path):
    context = RuntimeContext.create(
        mode="agent",
        principal=Principal.anonymous(),
        solution_id="szyd-compliance",
    )
    attachments = AttachmentStore(tmp_path / "artifacts")
    record = asyncio.run(
        attachments.save(
            UploadFile(filename="material.txt", file=BytesIO(b"Synthetic business material")),
            workspace_id=context.principal.workspace_id,
            solution_id=context.solution_id,
        )
    )
    database = tmp_path / "cases.sqlite3"
    adapter = SzydAdapter(SubprocessAdapter(), SzydCaseStore(database), attachments)
    opened = call(
        adapter,
        context,
        operation="open",
        message="Review material",
        artifact_ids=[record.artifact_id],
    )
    assert opened["documents"][0]["content"] == "Synthetic business material"
    reopened = SzydAdapter(SubprocessAdapter(), SzydCaseStore(database), attachments)
    assert call(reopened, context, operation="inspect")["documents"] == opened["documents"]
    with pytest.raises(AttachmentNotFoundError):
        call(
            reopened,
            replace(context, solution_id="other"),
            operation="open",
            message="Review",
            artifact_ids=[record.artifact_id],
        )


def test_oversized_transition_does_not_persist(tmp_path):
    context = RuntimeContext.create(mode="agent", principal=Principal.anonymous())
    store = SzydCaseStore(tmp_path / "cases.sqlite3")
    adapter = SzydAdapter(SubprocessAdapter(), store)
    definition = SkillRegistry.load_definition(ROOT).model_copy(deep=True)
    definition.manifest.spec.policy.max_output_bytes = 32
    with pytest.raises(ExecutionError, match="not saved"):
        asyncio.run(
            adapter.execute(
                definition, {"operation": "open", "message": "Material"}, context=context
            )
        )
    assert store.load(context) is None


def test_large_business_input_uses_private_json_files(tmp_path):
    context = RuntimeContext.create(mode="agent", principal=Principal.anonymous())
    adapter = SzydAdapter(SubprocessAdapter(), SzydCaseStore(tmp_path / "cases.sqlite3"))
    message = "Synthetic investment material. " * 2000
    opened = call(adapter, context, operation="open", message=message)
    assert opened["state"]["input_text"] == message
