import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from sage.agent_model import DemoAgentModel
from sage.app import create_app
from sage.artifacts import S3ArtifactStore
from sage.attachments import AttachmentStore, ContentTypeMatch
from sage.control_settings import ControlPlaneSettings
from sage.errors import AttachmentNotFoundError

SKILL_FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def test_artifact_survives_store_restart_and_preserves_digest(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    first = AttachmentStore(root)

    saved = asyncio.run(
        first.save(
            UploadFile(filename="notes.txt", file=BytesIO(b"durable artifact")),
            workspace_id="ws_team",
            solution_id="enterprise-qa",
        )
    )
    reopened = AttachmentStore(root)
    restored = reopened.get(
        saved.artifact_id,
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        enforce_scope=True,
    )

    assert restored.attachment_id == saved.attachment_id
    assert restored.content_digest == saved.content_digest
    assert restored.path.read_bytes() == b"durable artifact"
    assert saved.content_digest.startswith("sha256:")
    assert saved.content_digest.removeprefix("sha256:") in restored.path.as_posix()


def test_artifact_scope_mismatch_is_hidden_as_not_found(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "artifacts")
    saved = asyncio.run(
        store.save(
            UploadFile(filename="notes.txt", file=BytesIO(b"private")),
            workspace_id="ws_team",
            solution_id="contract-review",
        )
    )

    with pytest.raises(AttachmentNotFoundError, match="not found"):
        store.get(
            saved.artifact_id,
            workspace_id="ws_team",
            solution_id="news-intelligence",
            enforce_scope=True,
        )


def test_artifact_and_run_survive_application_restart(tmp_path: Path) -> None:
    settings = ControlPlaneSettings(data_root=tmp_path / "state")
    first_app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=settings,
    )
    with TestClient(first_app) as client:
        upload = client.post(
            "/v1/artifacts",
            files={"file": ("notes.txt", b"persistent input", "text/plain")},
        )
        run = client.post(
            "/v1/messages",
            json={"message": "echo persistent run"},
        )
        artifact_id = upload.json()["artifact_id"]
        run_id = run.json()["run_id"]

    second_app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=settings,
    )
    with TestClient(second_app) as client:
        artifact = client.get(f"/v1/artifacts/{artifact_id}")
        restored_run = client.get(f"/v1/runs/{run_id}")
        restored_events = client.get(f"/v1/runs/{run_id}/events")

    assert artifact.status_code == 200
    assert artifact.json()["content_digest"] == upload.json()["content_digest"]
    assert restored_run.status_code == 200
    assert restored_run.json()["status"] == "completed"
    assert [event["name"] for event in restored_events.json()] == [
        "run.started",
        "result.available",
        "run.completed",
    ]


def test_artifact_content_download_preserves_bytes_and_digest(tmp_path: Path) -> None:
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/artifacts",
            files={"file": ("notes.txt", b"download exactly", "text/plain")},
        )
        artifact_id = uploaded.json()["artifact_id"]
        content = client.get(f"/v1/artifacts/{artifact_id}/content")
        missing = client.get("/v1/artifacts/art_missing/content")

    assert content.status_code == 200
    assert content.content == b"download exactly"
    assert content.headers["content-type"].startswith("text/plain")
    assert "notes.txt" in content.headers["content-disposition"]
    assert content.headers["cache-control"] == "private, no-store"
    assert (
        content.headers["x-artifact-content-digest"]
        == uploaded.json()["content_digest"]
    )
    assert missing.status_code == 404


def test_directory_artifact_content_download_is_explicitly_unsupported(
    tmp_path: Path,
) -> None:
    from zipfile import ZipFile

    bundle = BytesIO()
    with ZipFile(bundle, "w") as archive:
        archive.writestr(
            "sample.log",
            "#Software: Microsoft Internet Information Services\n"
            "#Fields: date time cs-method cs-uri-stem sc-status\n"
            "2026-09-11 10:00:00 GET / 200\n",
        )
    app = create_app(
        SKILL_FIXTURES,
        agent_model=DemoAgentModel(),
        control_settings=ControlPlaneSettings(data_root=tmp_path / "state"),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/artifacts",
            files={"file": ("logs.zip", bundle.getvalue(), "application/zip")},
        )
        content = client.get(
            f"/v1/artifacts/{uploaded.json()['artifact_id']}/content"
        )

    assert uploaded.status_code == 201
    assert content.status_code == 409
    assert "Directory Artifacts" in content.json()["detail"]


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> None:
        assert ContentType == "application/json"
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[(bucket, key)])

    def list_objects_v2(self, **arguments: object) -> dict[str, object]:
        bucket = str(arguments["Bucket"])
        prefix = str(arguments["Prefix"])
        return {
            "Contents": [
                {"Key": key}
                for stored_bucket, key in self.objects
                if stored_bucket == bucket and key.startswith(prefix)
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> None:
        for item in Delete["Objects"]:  # type: ignore[union-attr]
            self.objects.pop((Bucket, item["Key"]), None)  # type: ignore[index]


def test_s3_artifact_backend_materializes_from_fresh_node_cache(tmp_path: Path) -> None:
    client = FakeS3Client()
    first_backend = S3ArtifactStore(
        client,
        bucket="artifacts",
        prefix="sage",
        cache_root=tmp_path / "cache-a",
    )
    first = AttachmentStore(None, artifact_backend=first_backend)
    saved = asyncio.run(
        first.save(
            UploadFile(filename="notes.txt", file=BytesIO(b"shared artifact")),
            workspace_id="ws_team",
            solution_id="enterprise-qa",
        )
    )
    second_backend = S3ArtifactStore(
        client,
        bucket="artifacts",
        prefix="sage",
        cache_root=tmp_path / "cache-b",
    )
    second = AttachmentStore(None, artifact_backend=second_backend)

    restored = second.get(
        saved.artifact_id,
        workspace_id="ws_team",
        solution_id="enterprise-qa",
        enforce_scope=True,
    )

    assert restored.content_digest == saved.content_digest
    assert restored.path.read_bytes() == b"shared artifact"
    assert restored.path.is_relative_to(tmp_path / "cache-b")


class CustomContentHandler:
    def detect(
        self,
        *,
        filename: str,
        suffix: str,
        declared_media_type: str | None,
        prefix: bytes,
    ) -> ContentTypeMatch | None:
        del filename, suffix, declared_media_type
        return (
            ContentTypeMatch("application/x-custom", ".custom")
            if prefix.startswith(b"CSTM")
            else None
        )


def test_custom_content_handler_extends_artifact_formats(tmp_path: Path) -> None:
    store = AttachmentStore(
        tmp_path / "artifacts",
        content_handlers=(CustomContentHandler(),),
    )

    saved = asyncio.run(
        store.save(
            UploadFile(filename="input.bin", file=BytesIO(b"CSTM payload")),
        )
    )

    assert saved.media_type == "application/x-custom"
    assert saved.path.suffix == ".custom"
