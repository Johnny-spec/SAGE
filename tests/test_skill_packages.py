import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile

from sage.control_models import GenerationRecord
from sage.control_store import LocalControlStore
from sage.skill_packages import SkillImportError, SkillPackageManager


def _store(tmp_path: Path) -> LocalControlStore:
    return LocalControlStore(
        tmp_path / "data",
        GenerationRecord(
            generation_id="gen_startup",
            content_digest="sha256:startup",
            skills_root=str(tmp_path),
            skill_versions={},
            agent_definitions={},
        ),
    )


def _zip(files: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _echo_zip() -> bytes:
    return _zip(
        {
            "echo-plus/SKILL.md": (b"---\nname: echo-plus\ndescription: Echo text safely.\n---\n"),
            "echo-plus/skill.yaml": b"""apiVersion: sage.dev/v1alpha1
kind: SkillBinding
metadata: {name: echo-plus, version: 1.0.0}
spec:
  execution: {type: python-subprocess, entrypoint: scripts/main.py}
  inputSchema: {type: object}
  outputSchema: {type: object}
""",
            "echo-plus/scripts/main.py": b"print('{}')\n",
        }
    )


def test_skill_zip_is_normalized_validated_and_downloadable(tmp_path: Path) -> None:
    manager = SkillPackageManager(_store(tmp_path))
    uploaded = asyncio.run(
        manager.upload(UploadFile(filename="echo-plus.zip", file=BytesIO(_echo_zip())))
    )

    validated = manager.validate(uploaded.package_id)

    assert validated.name == "echo-plus"
    assert validated.status == "valid"
    assert validated.executable is False
    assert validated.validation.issues[0].code == "uploaded_code_execution_disabled"
    assert manager.download_path(uploaded.package_id, "original").is_file()
    assert manager.download_path(uploaded.package_id, "normalized").is_file()


def test_python_skill_is_executable_only_with_isolated_runner(tmp_path: Path) -> None:
    manager = SkillPackageManager(_store(tmp_path), python_runner_enabled=True)
    uploaded = asyncio.run(
        manager.upload(UploadFile(filename="echo-plus.zip", file=BytesIO(_echo_zip())))
    )

    validated = manager.validate(uploaded.package_id)

    assert validated.status == "valid"
    assert validated.executable is True
    assert validated.validation.issues[0].code == "isolated_runner_enabled"


def test_skill_zip_rejects_path_traversal(tmp_path: Path) -> None:
    manager = SkillPackageManager(_store(tmp_path))
    payload = _zip({"../SKILL.md": b"bad", "skill.yaml": b"bad"})

    with pytest.raises(SkillImportError, match="unsafe path"):
        asyncio.run(manager.upload(UploadFile(filename="bad.zip", file=BytesIO(payload))))


def test_skill_zip_rejects_dependency_installation(tmp_path: Path) -> None:
    manager = SkillPackageManager(_store(tmp_path))
    payload = _zip(
        {
            "demo/SKILL.md": b"---\nname: demo\ndescription: Demo.\n---\n",
            "demo/skill.yaml": b"bad",
            "demo/requirements.txt": b"requests\n",
        }
    )

    with pytest.raises(SkillImportError, match="Dependency installation"):
        asyncio.run(manager.upload(UploadFile(filename="bad.zip", file=BytesIO(payload))))

    def test_skill_zip_rejects_windows_device_path(tmp_path: Path) -> None:
        manager = SkillPackageManager(_store(tmp_path))
        payload = _zip({"demo/SKILL.md": b"bad", "demo/CON.txt": b"bad"})

        with pytest.raises(SkillImportError, match="unsafe path"):
            asyncio.run(manager.upload(UploadFile(filename="bad.zip", file=BytesIO(payload))))
