from __future__ import annotations

import json

from redaction import redact_default, redact_result


def test_default_redaction_removes_paths_identity_messages_and_url_secrets():
    source = {
        "source_file": r"C:\Users\alice\private\u_ex.log",
        "username": "alice@example.test",
        "message": r"password=hunter2 at C:\secret\app.dll",
        "url": "https://alice:hunter2@example.test/path?q=secret#fragment",
        "query": "token=secret",
        "CommandLine": "msedge.exe --profile-directory=Secret",
    }

    redacted = redact_default(source)
    body = json.dumps(redacted)

    for secret in ("alice", "hunter2", "q=secret", "token=secret", "Secret"):
        assert secret not in body
    assert "u_ex.log" in redacted["source_file"]
    assert redacted["url"] == "https://example.test/path"
    assert redacted["username"].startswith("<identity:")
    assert redacted["message"].startswith("<message:")


def test_identity_pseudonyms_are_stable_and_shapes_are_preserved():
    value = {"users": [{"username": "alice"}, {"username": "alice"}], "count": 2}
    redacted = redact_default(value)

    assert isinstance(redacted["users"], list)
    assert isinstance(redacted["users"][0], dict)
    assert redacted["users"][0]["username"] == redacted["users"][1]["username"]
    assert redacted["count"] == 2


def test_result_marks_privacy_and_preserves_collection_commands():
    result = {
        "skill": "probe",
        "ok": True,
        "raw": {"path": r"C:\private\capture.etl"},
        "collection_plan": {
            "requests": [{
                "commands": [{
                    "command": "wevtutil epl Application <bundle>\\Application.evtx"
                }]
            }]
        },
    }

    redacted = redact_result(result)

    assert redacted["raw"]["privacy"]["default_redaction"] is True
    command = redacted["collection_plan"]["requests"][0]["commands"][0]["command"]
    assert command == "wevtutil epl Application <bundle>\\Application.evtx"
    assert result["raw"]["path"] == r"C:\private\capture.etl"


def test_embedded_local_paths_are_redacted_without_damaging_urls():
    redacted = redact_default({
        "error": r"failed C:\Users\alice\secret.log and /var/private/app.log",
        "summary": "see https://example.test/public/path for help",
    })
    body = json.dumps(redacted)

    assert r"C:\Users\alice" not in body
    assert "/var/private/app.log" not in body
    assert "secret.log" in body
    assert "app.log" in body
    assert redacted["summary"] == "see https://example.test/public/path for help"


def test_procmon_user_registry_detail_and_endpoint_are_redacted():
    source = {
        "user": r"DOMAIN\alice",
        "path": r"HKCU\Software\Contoso\SecretKey",
        "detail": "Desired Access: Read, password=hunter2",
        "command_line": r"C:\app\tool.exe --token abc123",
        "remote_endpoint": "10.20.30.40:443",
    }

    redacted = redact_default(source)
    body = json.dumps(redacted)

    for secret in ("alice", "Contoso", "hunter2", "abc123", "10.20.30.40"):
        assert secret not in body
    assert redacted["user"].startswith("<identity:")
    assert redacted["path"].endswith("/SecretKey")
    assert redacted["remote_endpoint"].endswith(":443")


def test_procmon_network_path_hashes_both_hosts_but_preserves_ports():
    redacted = redact_default({
        "path": "client.internal:54321 -> 10.20.30.40:443"
    })["path"]

    assert "client.internal" not in redacted
    assert "10.20.30.40" not in redacted
    assert ":54321" in redacted
    assert ":443" in redacted