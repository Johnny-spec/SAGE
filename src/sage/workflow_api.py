"""Admin-only workflow design APIs; no draft execution or publication bypass."""

from collections.abc import Callable
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sage.domain import Principal
from sage.solution_assist import AssistInput, AssistUnavailable
from sage.workflow_draft_models import WorkflowDraftInput
from sage.workflow_drafts import DraftConflict, WorkflowDraftManager
from sage.workflows import WorkflowTemplate


class WorkflowImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=512 * 1024)


class WorkflowYamlLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("Workflow mapping keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    return Principal(workspace_id="ws_default", principal_id=request.state.admin_actor,
                     roles=frozenset({"platform_admin"}), authentication_method="local")


def _revision(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(428, "If-Match revision is required")
    value = if_match.strip('"')
    if not value.isascii() or not value.isdigit() or len(value) > 12 or int(value) < 1:
        raise HTTPException(422, "If-Match must contain a positive revision")
    return int(value)


def _operation(callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(404, "Workflow draft not found") from exc
    except DraftConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def create_workflow_router() -> APIRouter:
    router = APIRouter()

    def manager(request: Request) -> WorkflowDraftManager:
        unavailable = frozenset(
            skill.name for skill in request.app.state.runtime_manager.active.registry
            if not skill.manifest.spec.policy.execution_enabled
            or skill.manifest.spec.execution.type in {"workflow", "model"}
        )
        return WorkflowDraftManager(request.app.state.control_store, unavailable)

    @router.get("/workflow-targets")
    def targets(request: Request):
        active = request.app.state.runtime_manager.active
        return {
            "generation_id": active.generation.generation_id,
            "targets": [
                {"kind": "agent", "target": agent_id, "name": definition["name"],
                 "version": definition["version"], "enabled": True,
                 "input_schema": {"type": "object", "properties": {
                     "message": {"type": "string"}}}, "output_schema": {
                     "type": "object", "properties": {"output": {"type": "object",
                     "properties": {"message": {"type": "string"}}}}}}
                for agent_id, definition in active.generation.agent_definitions.items()
            ] + [
                {"kind": "skill", "target": skill.name, "name": skill.name,
                 "version": skill.version,
                 "enabled": skill.manifest.spec.policy.execution_enabled
                 and skill.manifest.spec.execution.type not in {"workflow", "model"},
                 "input_schema": skill.manifest.spec.input_schema,
                 "output_schema": {"type": "object", "properties": {
                     "output": skill.manifest.spec.output_schema}}}
                for skill in active.registry
            ] + [
                {"kind": "tool", "target": tool.tool_id, "name": tool.binding.tool_name,
                 "version": tool.version, "enabled": tool.enabled, "risk": tool.risk,
                 "description": tool.description,
                 "server_id": tool.binding.server_id,
                 "server_alias": tool.binding.server_alias,
                 "content_digest": tool.content_digest,
                 "schema_digest": tool.binding.schema_digest,
                 "tool_timeout_seconds": tool.timeout_seconds,
                 "max_output_bytes": tool.max_output_bytes,
                 "input_schema": tool.input_schema,
                 "tool_output_schema": tool.output_schema,
                 "output_schema": {"type": "object", "properties": {
                     "output": tool.output_schema or {}}}}
                for tool in active.generation.tool_definitions.values()
            ],
        }

    @router.post("/solution-assist")
    async def assist(payload: AssistInput, request: Request):
        active = request.app.state.runtime_manager.active
        state = request.app.state.control_store.snapshot()
        if active.generation.generation_id != state.active_generation_id:
            raise HTTPException(409, "Active runtime is changing; retry the suggestion")
        try:
            return await request.app.state.solution_assistant.suggest(
                payload, state, active.registry, targets(request)["targets"],
                _principal(request).workspace_id,
            )
        except AssistUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/workflow-drafts/import")
    def preview_import(payload: WorkflowImportInput):
        try:
            depth = 0
            for event in yaml.parse(payload.content):
                if isinstance(event, yaml.AliasEvent):
                    raise ValueError("YAML aliases are not supported")
                if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                    depth += 1
                if isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                    depth -= 1
                if depth > 32:
                    raise ValueError("Workflow nesting exceeds 32 levels")
            raw = yaml.load(payload.content, Loader=WorkflowYamlLoader)
            if not isinstance(raw, dict):
                raise ValueError("Import must contain a Workflow object")
            if raw.get("format") == "sage-workflow-design-v1":
                if set(raw) != {"format", "draft"}:
                    raise ValueError("Unsupported design package fields")
                draft = WorkflowDraftInput.model_validate(raw["draft"])
                WorkflowTemplate.model_validate(draft.definition)
            else:
                WorkflowTemplate.model_validate(raw)
                draft = WorkflowDraftInput(title=str(raw["workflowId"]),
                                           solution_id="import", definition=raw)
            return draft
        except (ValueError, KeyError, yaml.YAMLError, RecursionError) as exc:
            if isinstance(exc, ValidationError):
                detail = [{"field_path": ".".join(map(str, error["loc"])),
                           "message": error["msg"]}
                          for error in exc.errors(include_input=False, include_context=False)]
            else:
                detail = "Invalid or unsupported workflow import"
            raise HTTPException(422, detail) from exc

    @router.get("/workflow-drafts")
    def drafts(
        request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    ):
        records = WorkflowDraftManager(request.app.state.control_store).list(
            _principal(request).workspace_id,
        )
        return {"items": records[offset:offset + limit], "total": len(records)}

    @router.post("/workflow-drafts", status_code=201)
    def create(payload: WorkflowDraftInput, request: Request, response: Response):
        principal = _principal(request)
        record = _operation(lambda: WorkflowDraftManager(request.app.state.control_store).create(
            payload, principal.workspace_id, principal.principal_id,
        ))
        response.headers["ETag"] = f'"{record.revision}"'
        return record

    @router.get("/workflow-drafts/{draft_id}")
    def get(draft_id: str, request: Request, response: Response):
        record = _operation(lambda: WorkflowDraftManager(request.app.state.control_store).get(
            draft_id, _principal(request).workspace_id,
        ))
        response.headers["ETag"] = f'"{record.revision}"'
        return record

    @router.put("/workflow-drafts/{draft_id}")
    def update(draft_id: str, payload: WorkflowDraftInput, request: Request, response: Response,
               if_match: Annotated[str | None, Header()] = None):
        revision = _revision(if_match)
        record = _operation(lambda: WorkflowDraftManager(request.app.state.control_store).update(
            draft_id, _principal(request).workspace_id, revision, payload,
        ))
        response.headers["ETag"] = f'"{record.revision}"'
        return record

    @router.delete("/workflow-drafts/{draft_id}", status_code=204)
    def delete(draft_id: str, request: Request,
               if_match: Annotated[str | None, Header()] = None):
        revision = _revision(if_match)
        _operation(lambda: WorkflowDraftManager(request.app.state.control_store).delete(
            draft_id, _principal(request).workspace_id, revision,
        ))
        return Response(status_code=204)

    @router.post("/workflow-drafts/{draft_id}/validate")
    def validate(draft_id: str, request: Request,
                 if_match: Annotated[str | None, Header()] = None):
        revision = _revision(if_match)
        return _operation(lambda: manager(request).validate(
            draft_id, _principal(request).workspace_id, revision,
        ))

    @router.get("/workflow-drafts/{draft_id}/export")
    def export(draft_id: str, request: Request,
               if_match: Annotated[str | None, Header()] = None):
        revision = _revision(if_match)
        content = _operation(lambda: manager(request).export(
            draft_id, _principal(request).workspace_id, revision,
        ))
        return Response(content, media_type="application/yaml", headers={
            "Content-Disposition": 'attachment; filename="workflow.yaml"',
            "ETag": f'"{revision}"',
        })

    return router