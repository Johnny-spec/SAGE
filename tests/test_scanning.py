import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile

from sage.attachments import AttachmentStore
from sage.control_models import GenerationRecord
from sage.control_store import LocalControlStore
from sage.errors import AttachmentRejectedError
from sage.scanning import (
    ClamAvContentScanner,
    ContentScanError,
    RejectingContentScanner,
)
from sage.skill_packages import SkillImportError, SkillPackageManager
from sage.solution_packages import SolutionBundlePackageManager, SolutionPackageError


def _store(tmp_path: Path) -> LocalControlStore:
    generation = GenerationRecord(
        generation_id="gen_startup",
        content_digest="sha256:startup",
        skills_root=str(tmp_path),
        skill_versions={},
        agent_definitions={},
    )
    return LocalControlStore(tmp_path / "state", generation)


def test_rejecting_scanner_blocks_all_upload_surfaces(tmp_path: Path) -> None:
    scanner = RejectingContentScanner()
    with pytest.raises(AttachmentRejectedError, match="scanning is required"):
        asyncio.run(
            AttachmentStore(
                tmp_path / "artifacts",
                content_scanner=scanner,
            ).save(UploadFile(filename="input.txt", file=BytesIO(b"text")))
        )

    skill_zip = BytesIO()
    with ZipFile(skill_zip, "w", ZIP_DEFLATED) as archive:
        archive.writestr("echo/SKILL.md", "unsafe")
    with pytest.raises(SkillImportError, match="scanning is required"):
        asyncio.run(
            SkillPackageManager(_store(tmp_path / "skill"), scanner).upload(
                UploadFile(filename="skill.zip", file=BytesIO(skill_zip.getvalue()))
            )
        )

    solution_zip = BytesIO()
    with ZipFile(solution_zip, "w", ZIP_DEFLATED) as archive:
        archive.writestr("solution.yaml", "unsafe")
    with pytest.raises(SolutionPackageError, match="scanning is required"):
        asyncio.run(
            SolutionBundlePackageManager(_store(tmp_path / "solution"), scanner).import_zip(
                UploadFile(filename="solution.zip", file=BytesIO(solution_zip.getvalue()))
            )
        )


class FakeClamConnection:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()
        self._returned = False

    def __enter__(self) -> "FakeClamConnection":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def recv(self, size: int) -> bytes:
        del size
        if self._returned:
            return b""
        self._returned = True
        return self.response


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (b"stream: OK\0", None),
        (b"stream: Eicar-Test-Signature FOUND\0", "Eicar-Test-Signature"),
    ],
)
def test_clamav_instream_protocol(
    tmp_path: Path,
    monkeypatch,
    response: bytes,
    expected_error: str | None,
) -> None:
    connection = FakeClamConnection(response)
    monkeypatch.setattr(
        "sage.scanning.socket.create_connection",
        lambda address, timeout: connection,
    )
    path = tmp_path / "input.bin"
    path.write_bytes(b"payload")
    scanner = ClamAvContentScanner("clamav")

    if expected_error:
        with pytest.raises(ContentScanError, match=expected_error):
            scanner.require_clean(path)
    else:
        scanner.require_clean(path)

    assert connection.sent.startswith(b"zINSTREAM\0")
    assert connection.sent.endswith(b"\0\0\0\0")