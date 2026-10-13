import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile

from sage.control_models import GenerationRecord
from sage.control_store import LocalControlStore
from sage.solution_packages import (
  SolutionBundlePackageManager,
  SolutionPackageError,
  load_solution_bundle,
)


def test_load_solution_bundle_maps_component_locks(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    (tmp_path / "solution.yaml").write_text(
        f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata:
  name: news-intelligence
  version: 1.0.0
  title: News Intelligence
spec:
  owner: news-team
  description: Build a cited daily news brief.
  entryAgentId: news-agent
  allowedRoles: [news-reader]
  supportedProfiles: [interactive, durable, scheduled]
  components:
    - kind: agent
      id: news-agent
      version: 1.0.0
      contentDigest: {digest}
    - kind: workflow
      id: build-news-brief
      version: 1.0.0
      contentDigest: {digest}
""",
        encoding="utf-8",
    )

    manifest = load_solution_bundle(tmp_path)

    assert manifest.metadata.name == "news-intelligence"
    assert manifest.spec.supported_profiles == ("interactive", "durable", "scheduled")
    assert [component.component_id for component in manifest.component_locks()] == [
        "news-agent",
        "build-news-brief",
    ]


def test_solution_bundle_rejects_unlocked_entry_agent(tmp_path: Path) -> None:
    digest = "sha256:" + "b" * 64
    (tmp_path / "solution.yaml").write_text(
        f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: invalid, version: 1.0.0, title: Invalid}}
spec:
  owner: team
  entryAgentId: missing-agent
  components:
    - kind: skill
      id: echo
      version: 1.0.0
      contentDigest: {digest}
""",
        encoding="utf-8",
    )

    with pytest.raises(SolutionPackageError, match="entry Agent"):
        load_solution_bundle(tmp_path)


def _package_manager(tmp_path: Path) -> SolutionBundlePackageManager:
    generation = GenerationRecord(
        generation_id="gen_startup",
        content_digest="sha256:startup",
        skills_root=str(tmp_path),
        skill_versions={},
        agent_definitions={},
    )
    store = LocalControlStore(tmp_path / "state", generation)
    return SolutionBundlePackageManager(store)


def test_solution_zip_verifies_component_file_digest(tmp_path: Path) -> None:
    ui = b"schema_version: 1\nsolution_id: demo\ntitle: Demo\nwidgets: []\n"
    digest = "sha256:" + "0" * 64
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "solution.yaml",
            f"""apiVersion: sage.dev/v1alpha1
kind: SolutionBundle
metadata: {{name: demo, version: 1.0.0, title: Demo}}
spec:
  owner: team
  supportedProfiles: [direct]
  components:
    - kind: ui-schema
      id: demo-ui
      version: 1.0.0
      contentDigest: {digest}
      path: ui.yaml
""",
        )
        archive.writestr("ui.yaml", ui)

    with pytest.raises(SolutionPackageError, match="digest mismatch"):
        asyncio.run(
            _package_manager(tmp_path).import_zip(
                UploadFile(filename="solution.zip", file=BytesIO(stream.getvalue()))
            )
        )


def test_solution_zip_rejects_path_traversal(tmp_path: Path) -> None:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("solution.yaml", "invalid")
        archive.writestr("../outside.yaml", "unsafe")

    with pytest.raises(SolutionPackageError, match="unsafe path"):
        asyncio.run(
            _package_manager(tmp_path).import_zip(
                UploadFile(filename="solution.zip", file=BytesIO(stream.getvalue()))
            )
        )
