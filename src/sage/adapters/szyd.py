"""SAGE-owned state coordination around the stateless SZYD subprocess."""

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import ValidationError, validate

from sage.adapters.base import SkillAdapter
from sage.adapters.subprocess import SubprocessAdapter
from sage.attachments import AttachmentStore
from sage.digests import canonical_digest
from sage.domain import RuntimeContext
from sage.errors import ExecutionError
from sage.models import SkillDefinition
from sage.szyd_state import SzydCaseStore

_MUTATIONS = {"evidence", "stage", "review", "final", "resume"}


def runtime_python_adapters(delegate, local_enabled, data_root, attachments) -> dict:
    if delegate is None and not local_enabled:
        return {}
    return {
        "python-subprocess": SzydAdapter(
            delegate if delegate is not None else SubprocessAdapter(),
            SzydCaseStore(data_root / "szyd-cases.sqlite3"),
            attachments,
        )
    }


class SzydAdapter:
    def __init__(
        self,
        delegate: SkillAdapter,
        store: SzydCaseStore,
        attachments: AttachmentStore | None = None,
    ) -> None:
        self._delegate = delegate
        self._store = store
        self._attachments = attachments

    async def execute(
        self,
        definition: SkillDefinition,
        arguments: Mapping[str, Any],
        *,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        if definition.name != "szyd-law":
            return await self._delegate.execute(definition, arguments, context=context)
        if "state" in arguments:
            raise ExecutionError("SZYD state is server-owned")
        operation = arguments["operation"]
        case = self._store.load(context, arguments.get("case_id"))
        response = case.response if case else {}
        artifact_ids = list(
            dict.fromkeys(
                [
                    *response.get("artifact_ids", []),
                    *arguments.get("artifact_ids", []),
                ]
            )
        )
        documents = self._documents(artifact_ids, context)
        if operation in {"open", "inspect"} and case:
            return {**response, "documents": documents}
        if operation == "inspect" or (operation in _MUTATIONS and not case):
            raise ExecutionError("Open a SZYD case first")
        revision = response.get("revision", 0)
        expected = arguments.get("expected_revision")
        turn_id = context.root_run_id or context.run_id
        key = canonical_digest([turn_id, operation, expected])
        digest = canonical_digest(dict(arguments))
        if operation in _MUTATIONS:
            replay = self._store.replay(context, key, digest)
            if replay is not None:
                return {**replay, "documents": documents}
            if expected != revision:
                raise ExecutionError("SZYD revision conflict; inspect before continuing")
            if operation == "resume" and case and case.waiting_run_id == turn_id:
                raise ExecutionError("SZYD supplement requires a later user turn")
        supplied = {
            "operation": operation,
            "message": arguments.get("message", ""),
            "payload": arguments.get("payload", {}),
        }
        if case:
            supplied["state"] = response["state"]
        if operation == "resume":
            supplied["payload"] = {**supplied["payload"], "file_list": artifact_ids}
        result = await self._delegate.execute(definition, supplied, context=context)
        try:
            validate(result, definition.manifest.spec.output_schema)
        except ValidationError as error:
            raise ExecutionError("Invalid SZYD transition response") from error
        if "error" in result:
            raise ExecutionError(f"SZYD transition rejected: {result['reason']}")
        preview = {
            **result,
            "artifact_ids": artifact_ids,
            "documents": documents,
            "case_id": result.get("state", {}).get("run_id", ""),
            "revision": revision + 1,
        }
        if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > (
            definition.manifest.spec.policy.max_output_bytes
        ):
            raise ExecutionError("SZYD output exceeds the limit; transition was not saved")
        if operation in _MUTATIONS or (operation == "open" and not case):
            result["artifact_ids"] = artifact_ids
            waiting_run_id = turn_id if result["decision"]["action"] == "SUPPLEMENT" else None
            result = self._store.save(
                context,
                result,
                expected_revision=revision,
                key=key,
                digest=digest,
                waiting_run_id=waiting_run_id,
            )
        return {**result, "documents": documents}

    def _documents(self, artifact_ids: list[str], context: RuntimeContext) -> list[dict]:
        documents = []
        total_bytes = 0
        for artifact_id in artifact_ids:
            if self._attachments is None:
                raise ExecutionError("SZYD Artifact storage is unavailable")
            record = self._attachments.get(
                artifact_id,
                workspace_id=context.principal.workspace_id,
                solution_id=context.solution_id,
                enforce_scope=True,
            )
            item = {"artifact_id": artifact_id, "filename": record.filename}
            if (
                record.path.is_file()
                and record.suffix in {".txt", ".json", ".csv", ".log"}
                and total_bytes + record.size <= 1024 * 1024
            ):
                try:
                    item["content"] = record.path.read_text(encoding="utf-8-sig")
                    total_bytes += record.size
                except UnicodeError:
                    item["limitation"] = "Provide a UTF-8 text version"
            else:
                item["limitation"] = "Not parsed; provide UTF-8 text under the 1 MiB total limit"
            documents.append(item)
        return documents
