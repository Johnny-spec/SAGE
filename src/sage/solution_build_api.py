"""Build reviewed workflow drafts through the existing Solution supply chain."""

import io
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from sage.registry import SkillRegistry
from sage.solution_builder import plan_solution_bundle, solution_bundle_bytes
from sage.workflow_api import _operation, _principal, _revision
from sage.workflow_drafts import DraftConflict


class BuildPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)


class BuildInput(BuildPlanInput):
    build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def create_solution_build_router() -> APIRouter:
    router = APIRouter()

    def plan(request, draft_id, revision, version):
        state = request.app.state.control_store.snapshot()
        registry = SkillRegistry.load(
            Path(state.generations[state.active_generation_id].skills_root)
        )
        return plan_solution_bundle(
            state,
            registry,
            draft_id=draft_id,
            workspace_id=_principal(request).workspace_id,
            revision=revision,
            version=version,
        )

    def confirmed(request, draft_id, revision, payload):
        result = plan(request, draft_id, revision, payload.version)
        if result["build_digest"] != payload.build_digest:
            raise DraftConflict("Build preview is stale; resolve dependencies again")
        return result

    def existing(request, preview):
        state = request.app.state.control_store.snapshot()
        release_id = (
            preview["manifest"]["metadata"]["name"]
            + "@"
            + preview["manifest"]["metadata"]["version"]
        )
        release = state.solution_releases.get(release_id)
        if release is None:
            return None
        if release.package_metadata.get("build_digest") != preview["build_digest"]:
            raise DraftConflict("Release version already exists with different content")
        candidate = next(
            (
                item
                for item in state.candidates.values()
                if item.solution_release_ids == (release_id,)
            ),
            None,
        )
        if candidate is None:
            raise DraftConflict("Release exists without its build Candidate")
        return {"release": release, "candidate": candidate, "replayed": True}

    def discard(request, package_relpath):
        store = request.app.state.control_store
        root = store.artifacts_root / "solutions"
        identifier = store.resolve_path(package_relpath).relative_to(root).parts[0]
        shutil.rmtree(root / identifier, ignore_errors=True)
        shutil.rmtree(store.uploads_root / identifier, ignore_errors=True)

    async def build(request, draft_id, revision, payload, upload=None):
        preview = _operation(lambda: confirmed(request, draft_id, revision, payload))
        previous = _operation(lambda: existing(request, preview))
        if previous:
            if upload:
                await upload.close()
            return previous
        if upload is None:
            upload = UploadFile(
                filename="solution.zip", file=io.BytesIO(solution_bundle_bytes(preview))
            )
        try:
            (
                manifest,
                package_relpath,
                metadata,
            ) = await request.app.state.solution_bundle_packages.import_zip(upload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        retained = False
        try:
            if (
                manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
                != preview["manifest"]
            ):
                raise HTTPException(409, "Uploaded Bundle does not match the reviewed manifest")

            def commit():
                nonlocal retained
                with request.app.state.control_store.transaction():
                    confirmed(request, draft_id, revision, payload)
                    replay = existing(request, preview)
                    if replay:
                        return replay
                    release = request.app.state.solution_manager.create_release(
                        manifest.metadata.name,
                        version=manifest.metadata.version,
                        components=manifest.component_locks(),
                        entry_agent_id=manifest.spec.entry_agent_id,
                        allowed_roles=manifest.spec.allowed_roles,
                        supported_profiles=manifest.spec.supported_profiles,
                        source_generation_id=preview["generation_id"],
                        component_paths=manifest.component_paths(),
                        package_relpath=package_relpath,
                        package_metadata={**metadata, "build_digest": preview["build_digest"]},
                    )
                    catalog = request.app.state.catalog_manager
                    candidate = catalog.create_candidate(
                        title=f"{manifest.metadata.title} {manifest.metadata.version}",
                        description=f"Workflow draft {draft_id}, revision {revision}",
                        agent_draft_ids=(),
                        skill_package_ids=(),
                        solution_release_ids=(release.release_id,),
                        default_agent_id=None,
                    )
                    candidate = catalog.validate(candidate.candidate_id)
                retained = True
                return {"release": release, "candidate": candidate, "replayed": False}

            return _operation(commit)
        finally:
            if not retained:
                discard(request, package_relpath)

    @router.post("/workflow-drafts/{draft_id}/resolve")
    def resolve(
        draft_id: str,
        payload: BuildPlanInput,
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ):
        revision = _revision(if_match)
        preview = _operation(lambda: plan(request, draft_id, revision, payload.version))
        return {
            **preview,
            "signature_required": request.app.state.control_settings.signed_solutions_required,
        }

    @router.post("/workflow-drafts/{draft_id}/bundle")
    def bundle(
        draft_id: str,
        payload: BuildInput,
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ):
        preview = _operation(lambda: confirmed(request, draft_id, _revision(if_match), payload))
        return Response(
            solution_bundle_bytes(preview),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="solution.zip"',
                "X-Bundle-Signed": "false",
            },
        )

    @router.post("/workflow-drafts/{draft_id}/build")
    async def build_generated(
        draft_id: str,
        payload: BuildInput,
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ):
        return await build(request, draft_id, _revision(if_match), payload)

    @router.post("/workflow-drafts/{draft_id}/build-signed")
    async def build_signed(
        draft_id: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        version: Annotated[str, Form(max_length=64)],
        build_digest: Annotated[str, Form(pattern=r"^sha256:[0-9a-f]{64}$")],
        if_match: Annotated[str | None, Header()] = None,
    ):
        try:
            return await build(
                request,
                draft_id,
                _revision(if_match),
                BuildInput(version=version, build_digest=build_digest),
                file,
            )
        finally:
            await file.close()

    return router
