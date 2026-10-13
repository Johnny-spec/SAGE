"""Bounded upload handling backed by persistent, scoped Artifacts."""

import asyncio
import hashlib
import stat
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from fastapi import UploadFile

from sage.artifacts import ArtifactBackend, ArtifactRecord, LocalArtifactStore
from sage.domain.context import DEFAULT_WORKSPACE_ID
from sage.errors import AttachmentNotFoundError, AttachmentRejectedError
from sage.scanning import AllowAllContentScanner, ContentScanError, ContentScanner

_TEXT_SUFFIXES = {".csv", ".json", ".log", ".txt"}
_SNIFF_BYTES = 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
IIS_BUNDLE_MEDIA_TYPE = "application/vnd.sage.iis-w3c-bundle"
NETLOG_MEDIA_TYPE = "application/x-chromium-netlog"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    attachment_id: str
    filename: str
    path: Path
    size: int
    media_type: str
    suffix: str
    content_digest: str = ""
    workspace_id: str = DEFAULT_WORKSPACE_ID
    solution_id: str | None = None

    @property
    def artifact_id(self) -> str:
        return self.attachment_id

    @property
    def route_types(self) -> tuple[str, str]:
        return self.suffix, self.media_type


@dataclass(frozen=True, slots=True)
class ContentTypeMatch:
    media_type: str
    suffix: str


class ContentTypeHandler(Protocol):
    def detect(
        self,
        *,
        filename: str,
        suffix: str,
        declared_media_type: str | None,
        prefix: bytes,
    ) -> ContentTypeMatch | None: ...


@dataclass(frozen=True, slots=True)
class SignatureContentHandler:
    media_type: str
    suffix: str
    signatures: tuple[bytes, ...]

    def detect(
        self,
        *,
        filename: str,
        suffix: str,
        declared_media_type: str | None,
        prefix: bytes,
    ) -> ContentTypeMatch | None:
        del filename, suffix, declared_media_type
        if any(prefix.startswith(signature) for signature in self.signatures):
            return ContentTypeMatch(self.media_type, self.suffix)
        return None


DEFAULT_CONTENT_HANDLERS: tuple[ContentTypeHandler, ...] = (
    SignatureContentHandler("application/pdf", ".pdf", (b"%PDF-",)),
    SignatureContentHandler("image/png", ".png", (b"\x89PNG\r\n\x1a\n",)),
    SignatureContentHandler("image/jpeg", ".jpg", (b"\xff\xd8\xff",)),
)


class AttachmentStore:
    def __init__(
        self,
        root: Path | None,
        max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
        artifact_backend: ArtifactBackend | None = None,
        content_handlers: Sequence[ContentTypeHandler] = DEFAULT_CONTENT_HANDLERS,
        content_scanner: ContentScanner | None = None,
    ) -> None:
        if artifact_backend is None and root is None:
            raise ValueError("AttachmentStore requires a root or Artifact backend")
        self._artifacts = artifact_backend or LocalArtifactStore(root)  # type: ignore[arg-type]
        self._max_bytes = max_bytes
        self._max_extracted_bytes = max_extracted_bytes
        self._content_handlers = tuple(content_handlers)
        self._content_scanner = content_scanner or AllowAllContentScanner()

    async def save(
        self,
        upload: UploadFile,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        solution_id: str | None = None,
    ) -> AttachmentRecord:
        filename = Path(upload.filename or "upload").name
        suffix = Path(filename).suffix.casefold()
        declared_media_type = upload.content_type

        attachment_id = f"art_{uuid4().hex}"
        folder = self._artifacts.create_staging(attachment_id)
        uploaded_path = folder / "original.bin"
        size = 0
        prefix = b""
        digest = hashlib.sha256()
        try:
            with uploaded_path.open("wb") as stream:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > self._max_bytes:
                        limit_mib = self._max_bytes / (1024 * 1024)
                        raise AttachmentRejectedError(
                            f"Attachment exceeds the {limit_mib:g} MiB limit"
                        )
                    if len(prefix) < _SNIFF_BYTES:
                        prefix += chunk[: _SNIFF_BYTES - len(prefix)]
                    digest.update(chunk)
                    stream.write(chunk)

            try:
                await asyncio.to_thread(self._content_scanner.require_clean, uploaded_path)
            except ContentScanError as exc:
                raise AttachmentRejectedError(str(exc)) from exc

            if zipfile.is_zipfile(uploaded_path):
                destination = self._extract_iis_archive(uploaded_path, folder)
                if destination is not None:
                    media_type = IIS_BUNDLE_MEDIA_TYPE
                    content_kind = "directory"
                    content_subpath = destination.relative_to(folder).as_posix()
                    original_subpath = uploaded_path.relative_to(folder).as_posix()
                else:
                    normalized_suffix = ".docx" if suffix == ".docx" else ".zip"
                    media_type = DOCX_MEDIA_TYPE if suffix == ".docx" else "application/zip"
                    destination = folder / f"input{normalized_suffix}"
                    uploaded_path.replace(destination)
                    content_kind = "file"
                    content_subpath = destination.relative_to(folder).as_posix()
                    original_subpath = content_subpath
            else:
                match = next(
                    (
                        match
                        for handler in self._content_handlers
                        if (
                            match := handler.detect(
                                filename=filename,
                                suffix=suffix,
                                declared_media_type=declared_media_type,
                                prefix=prefix,
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if match is not None:
                    media_type = match.media_type
                    normalized_suffix = match.suffix
                else:
                    media_type = self._detect_text_media_type(prefix, declared_media_type)
                    if media_type is None:
                        raise AttachmentRejectedError(
                            "Unsupported attachment content; install a ContentTypeHandler "
                            "for this format"
                        )
                    if media_type == "text/iis-w3c":
                        normalized_suffix = ".log"
                    elif media_type == NETLOG_MEDIA_TYPE:
                        normalized_suffix = ".json"
                    else:
                        normalized_suffix = suffix
                if match is None and normalized_suffix not in _TEXT_SUFFIXES:
                    normalized_suffix = ".txt"
                destination = folder / f"input{normalized_suffix}"
                uploaded_path.replace(destination)
                content_kind = "file"
                content_subpath = destination.relative_to(folder).as_posix()
                original_subpath = content_subpath
            content_digest = f"sha256:{digest.hexdigest()}"
            object_relpath = (
                f"objects/{digest.hexdigest()}/{attachment_id}"
            )
            artifact = ArtifactRecord(
                artifact_id=attachment_id,
                workspace_id=workspace_id,
                solution_id=solution_id,
                filename=filename,
                media_type=media_type,
                suffix=suffix,
                size=size,
                content_digest=content_digest,
                content_kind=content_kind,
                object_relpath=object_relpath,
                content_subpath=content_subpath,
                original_subpath=original_subpath,
            )
            self._artifacts.commit(artifact, folder)
        except BaseException:
            self._artifacts.discard_staging(folder)
            raise
        finally:
            await upload.close()

        return self._attachment_record(artifact)

    def _extract_iis_archive(self, archive_path: Path, folder: Path) -> Path | None:
        extracted_root = folder / "logs"
        extracted_root.mkdir()
        extracted_bytes = 0
        accepted_files = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
                if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise AttachmentRejectedError(
                        f"ZIP bundle must contain between 1 and {_MAX_ARCHIVE_MEMBERS} files"
                    )
                for member in members:
                    self._validate_archive_member(member)
                    destination = extracted_root / f"{accepted_files + 1:04d}.log"
                    prefix = b""
                    with archive.open(member) as source, destination.open("wb") as stream:
                        while chunk := source.read(64 * 1024):
                            extracted_bytes += len(chunk)
                            if extracted_bytes > self._max_extracted_bytes:
                                limit_mib = self._max_extracted_bytes / (1024 * 1024)
                                raise AttachmentRejectedError(
                                    f"ZIP bundle exceeds the {limit_mib:g} MiB extracted limit"
                                )
                            if len(prefix) < _SNIFF_BYTES:
                                prefix += chunk[: _SNIFF_BYTES - len(prefix)]
                            stream.write(chunk)
                    if self._looks_like_iis_w3c(prefix):
                        accepted_files += 1
                    else:
                        destination.unlink()
        except AttachmentRejectedError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise AttachmentRejectedError(f"Invalid ZIP bundle: {exc}") from exc

        if accepted_files == 0:
            extracted_root.rmdir()
            return None
        return extracted_root

    @staticmethod
    def _validate_archive_member(member: zipfile.ZipInfo) -> None:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or stat.S_ISLNK(mode)
        ):
            raise AttachmentRejectedError(f"ZIP bundle contains an unsafe path: {member.filename}")
        if member.flag_bits & 0x1:
            raise AttachmentRejectedError("Encrypted ZIP bundles are not supported")

    def get(
        self,
        attachment_id: str,
        *,
        workspace_id: str | None = None,
        solution_id: str | None = None,
        enforce_scope: bool = False,
    ) -> AttachmentRecord:
        artifact = self._artifacts.get(attachment_id)
        if artifact is None:
            raise AttachmentNotFoundError(f"Attachment not found: {attachment_id}")
        if enforce_scope and (
            artifact.workspace_id != workspace_id or artifact.solution_id != solution_id
        ):
            raise AttachmentNotFoundError(f"Attachment not found: {attachment_id}")
        return self._attachment_record(artifact)

    def get_artifact(
        self,
        artifact_id: str,
        *,
        workspace_id: str | None = None,
        solution_id: str | None = None,
        enforce_scope: bool = False,
    ) -> ArtifactRecord:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None or (
            enforce_scope
            and (artifact.workspace_id != workspace_id or artifact.solution_id != solution_id)
        ):
            raise AttachmentNotFoundError(f"Artifact not found: {artifact_id}")
        return artifact

    def _attachment_record(self, artifact: ArtifactRecord) -> AttachmentRecord:
        return AttachmentRecord(
            attachment_id=artifact.artifact_id,
            filename=artifact.filename,
            path=self._artifacts.resolve_content(artifact),
            size=artifact.size,
            media_type=artifact.media_type,
            suffix=artifact.suffix,
            content_digest=artifact.content_digest,
            workspace_id=artifact.workspace_id,
            solution_id=artifact.solution_id,
        )

    @staticmethod
    def _detect_text_media_type(prefix: bytes, declared: str | None) -> str | None:
        if AttachmentStore._looks_like_iis_w3c(prefix):
            return "text/iis-w3c"
        if AttachmentStore._looks_like_netlog(prefix):
            return NETLOG_MEDIA_TYPE
        if not AttachmentStore._looks_like_text(prefix):
            return None
        if declared and declared.casefold().startswith("text/"):
            return declared
        return "text/plain"

    @staticmethod
    def _looks_like_iis_w3c(prefix: bytes) -> bool:
        text = prefix.decode("utf-8-sig", errors="replace").casefold()
        if "#fields:" not in text:
            return False
        if "internet information services" in text or "#software: iis" in text:
            return True
        fields = text.split("#fields:", 1)[1].splitlines()[0].split()
        return {"date", "time", "cs-method", "cs-uri-stem", "sc-status"} <= set(fields)

    @staticmethod
    def _looks_like_netlog(prefix: bytes) -> bool:
        text = prefix.decode("utf-8-sig", errors="replace")
        if '"constants"' not in text[:1024]:
            return False
        return any(
            marker in text for marker in ('"logSourceType"', '"logEventTypes"', '"clientInfo"')
        )

    @staticmethod
    def _looks_like_text(prefix: bytes) -> bool:
        if not prefix or b"\x00" in prefix:
            return False
        text = prefix.decode("utf-8-sig", errors="replace")
        controls = sum(ord(character) < 32 and character not in "\n\r\t" for character in text)
        return controls <= max(1, len(text) // 100)
