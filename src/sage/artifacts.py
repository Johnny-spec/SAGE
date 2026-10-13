"""Persistent local Artifact metadata and content storage contracts."""

import json
import os
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sage.domain.context import DEFAULT_WORKSPACE_ID


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    workspace_id: str = Field(default=DEFAULT_WORKSPACE_ID, min_length=1)
    solution_id: str | None = None
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    suffix: str = ""
    size: int = Field(ge=0)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_kind: Literal["file", "directory"]
    object_relpath: str = Field(min_length=1)
    content_subpath: str = Field(min_length=1)
    original_subpath: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactBackend(Protocol):
    def create_staging(self, artifact_id: str) -> Path: ...

    def commit(self, record: ArtifactRecord, staging_path: Path) -> ArtifactRecord: ...

    def get(self, artifact_id: str) -> ArtifactRecord | None: ...

    def resolve_content(self, record: ArtifactRecord) -> Path: ...

    def discard_staging(self, staging_path: Path) -> None: ...


class LocalArtifactStore:
    """Local development implementation with durable metadata and opaque IDs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.objects_root = self.root / "objects"
        self.records_root = self.root / "records"
        self.staging_root = self.root / ".staging"
        self._lock = RLock()
        for path in (self.root, self.objects_root, self.records_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True)

    def create_staging(self, artifact_id: str) -> Path:
        path = self.staging_root / artifact_id
        with self._lock:
            if path.exists():
                raise ValueError(f"Artifact staging path already exists: {artifact_id}")
            path.mkdir()
        return path

    def commit(self, record: ArtifactRecord, staging_path: Path) -> ArtifactRecord:
        source = staging_path.resolve()
        if not source.is_relative_to(self.staging_root):
            raise ValueError("Artifact staging path escapes the staging root")
        destination = self.resolve_path(record.object_relpath)
        if not destination.is_relative_to(self.objects_root):
            raise ValueError("Artifact object path escapes the objects root")
        content_path = destination / record.content_subpath
        original_path = destination / record.original_subpath
        metadata_path = self._metadata_path(record.artifact_id)
        with self._lock:
            if metadata_path.exists() or destination.exists():
                raise ValueError(f"Artifact already exists: {record.artifact_id}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            try:
                if not content_path.exists() or not original_path.exists():
                    raise ValueError("Artifact content is missing from the committed object")
                self._persist(metadata_path, record)
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        return record

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        try:
            metadata_path = self._metadata_path(artifact_id)
        except ValueError:
            return None
        with self._lock:
            if not metadata_path.is_file():
                return None
            record = ArtifactRecord.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
            if not self.resolve_content(record).exists():
                return None
            return record

    def resolve_content(self, record: ArtifactRecord) -> Path:
        return self.resolve_path(record.object_relpath) / record.content_subpath

    def discard_staging(self, staging_path: Path) -> None:
        resolved = staging_path.resolve()
        if resolved.is_relative_to(self.staging_root):
            shutil.rmtree(resolved, ignore_errors=True)

    def resolve_path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Artifact path escapes the data root")
        return candidate

    def _metadata_path(self, artifact_id: str) -> Path:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
        if not artifact_id or any(character not in allowed for character in artifact_id):
            raise ValueError("Invalid Artifact id")
        return self.records_root / f"{artifact_id}.json"

    @staticmethod
    def _persist(path: Path, record: ArtifactRecord) -> None:
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record.model_dump(mode="json"), stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


class S3ArtifactStore:
    """S3-compatible Artifact backend with a bounded node-local materialization cache."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        cache_root: Path,
        prefix: str = "sage",
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._cache_root = cache_root.resolve()
        self._staging_root = self._cache_root / ".staging"
        self._objects_root = self._cache_root / "objects"
        self._lock = RLock()
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._objects_root.mkdir(parents=True, exist_ok=True)

    def create_staging(self, artifact_id: str) -> Path:
        path = self._staging_root / artifact_id
        with self._lock:
            if path.exists():
                raise ValueError(f"Artifact staging path already exists: {artifact_id}")
            path.mkdir()
        return path

    def commit(self, record: ArtifactRecord, staging_path: Path) -> ArtifactRecord:
        source = staging_path.resolve()
        if not source.is_relative_to(self._staging_root):
            raise ValueError("Artifact staging path escapes the staging root")
        files = sorted(path for path in source.rglob("*") if path.is_file())
        uploaded_keys: list[str] = []
        try:
            for path in files:
                relative = path.relative_to(source).as_posix()
                key = self._key(f"{record.object_relpath}/{relative}")
                self._client.upload_file(str(path), self._bucket, key)
                uploaded_keys.append(key)
            metadata_key = self._metadata_key(record.artifact_id)
            self._client.put_object(
                Bucket=self._bucket,
                Key=metadata_key,
                Body=record.model_dump_json().encode("utf-8"),
                ContentType="application/json",
            )
            uploaded_keys.append(metadata_key)
        except BaseException:
            if uploaded_keys:
                with suppress(Exception):
                    self._client.delete_objects(
                        Bucket=self._bucket,
                        Delete={"Objects": [{"Key": key} for key in uploaded_keys]},
                    )
            raise
        destination = self._cache_path(record.object_relpath)
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            source.replace(destination)
        return record

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=self._metadata_key(artifact_id),
            )
        except ValueError:
            return None
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise
        body = response["Body"].read()
        record = ArtifactRecord.model_validate_json(body)
        self.resolve_content(record)
        return record

    def resolve_content(self, record: ArtifactRecord) -> Path:
        destination_root = self._cache_path(record.object_relpath)
        content = destination_root / record.content_subpath
        if content.exists():
            return content
        with self._lock:
            if content.exists():
                return content
            if record.content_kind == "file":
                content.parent.mkdir(parents=True, exist_ok=True)
                self._client.download_file(
                    self._bucket,
                    self._key(f"{record.object_relpath}/{record.content_subpath}"),
                    str(content),
                )
            else:
                self._download_directory(record, destination_root)
        if not content.exists():
            raise FileNotFoundError(f"Artifact content is unavailable: {record.artifact_id}")
        return content

    def discard_staging(self, staging_path: Path) -> None:
        path = staging_path.resolve()
        if path.is_relative_to(self._staging_root):
            shutil.rmtree(path, ignore_errors=True)

    def _download_directory(self, record: ArtifactRecord, destination_root: Path) -> None:
        prefix = self._key(f"{record.object_relpath}/{record.content_subpath}").rstrip("/") + "/"
        token: str | None = None
        while True:
            arguments: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token is not None:
                arguments["ContinuationToken"] = token
            response = self._client.list_objects_v2(**arguments)
            for item in response.get("Contents", []):
                key = str(item["Key"])
                relative = key.removeprefix(self._key(f"{record.object_relpath}/"))
                destination = (destination_root / relative).resolve()
                if not destination.is_relative_to(destination_root):
                    raise ValueError("S3 Artifact object path escapes the cache root")
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._client.download_file(self._bucket, key, str(destination))
            if not response.get("IsTruncated"):
                break
            token = str(response["NextContinuationToken"])

    def _cache_path(self, relative_path: str) -> Path:
        destination = (self._cache_root / relative_path).resolve()
        if not destination.is_relative_to(self._cache_root):
            raise ValueError("Artifact path escapes the cache root")
        return destination

    def _key(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").lstrip("/")
        if ".." in normalized.split("/"):
            raise ValueError("Artifact key escapes the configured prefix")
        return f"{self._prefix}/{normalized}" if self._prefix else normalized

    def _metadata_key(self, artifact_id: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
        if not artifact_id or any(character not in allowed for character in artifact_id):
            raise ValueError("Invalid Artifact id")
        return self._key(f"records/{artifact_id}.json")

    @staticmethod
    def _not_found(error: Exception) -> bool:
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return False
        details = response.get("Error", {})
        return isinstance(details, dict) and str(details.get("Code")) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }
