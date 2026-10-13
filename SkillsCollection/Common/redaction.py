"""Default-safe, shape-preserving redaction for diagnostic results."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|authorization|cookie|set-cookie|client_secret|"
    r"access_token|refresh_token|api_key|private_key|sas_token|credential)"
)
IDENTITY_KEYS = {
    "username", "user_name", "cs-username", "account", "account_name",
    "user", "email", "client_ip", "c-ip", "src_ip", "source_ip", "user_sid",
}
PATH_KEYS = {
    "path", "paths", "source_path", "source_file", "source_files",
    "fullpath", "evtx_path", "evtx_paths", "user_data_dir", "reports_dir",
    "preferences_file", "exe", "executable", "sample_files", "repo_roots",
    "config_roots", "symbols_roots", "registry_path", "registry_key",
    "key_path", "target_path", "image_path",
}
HASHED_TEXT_KEYS = {
    "message", "properties", "snippet", "stack_trace", "commandline",
    "command_line", "detail", "excerpt",
}
ENDPOINT_KEYS = {
    "endpoint", "local_endpoint", "remote_endpoint", "source_endpoint",
    "destination_endpoint", "local_address", "remote_address",
}
PATH_ENDPOINT_RE = re.compile(
    r"(?i)(?P<host>\[[0-9a-f:]+\]|(?:[a-z0-9_-]+\.)*[a-z0-9_-]+):(?P<port>\d{1,5})"
)
QUERY_KEYS = {"cs-uri-query", "uri_query", "query_string"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"(?i)^[a-z]:[\\/]|^\\\\")
REGISTRY_PATH_RE = re.compile(
    r"(?i)^(?:HKLM|HKCU|HKCR|HKU|HKCC|HKEY_[A-Z_]+)[\\/]"
)
EMBEDDED_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s<>\"']+"
)
EMBEDDED_POSIX_PATH_RE = re.compile(
    r"(?<![:/\w])/(?:[^/\s<>\"']+/)*[^/\s<>\"']+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+"
)


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _key_name(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    host = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, host + port, parts.path, "", ""))


def _redact_urls_in_text(value: str) -> str:
    return URL_RE.sub(lambda match: _redact_url(match.group(0).rstrip(".,);]")), value)


def _looks_like_path(value: str) -> bool:
    if not value or value.startswith(("http://", "https://", "<")):
        return False
    return (
        bool(WINDOWS_PATH_RE.search(value))
        or bool(REGISTRY_PATH_RE.search(value))
        or value.startswith("/")
    )


def _path_marker(value: str) -> str:
    if WINDOWS_PATH_RE.search(value) or REGISTRY_PATH_RE.search(value):
        basename = PureWindowsPath(value).name
    else:
        basename = PurePosixPath(value).name
    basename = basename or "unknown"
    return f"<path:{_digest(value)}>/{basename}"


def _hash_marker(kind: str, value: str) -> str:
    return f"<{kind}:{_digest(value)}>"


def _redact_endpoint(value: str) -> str:
    text = value.strip()
    host = text
    port = ""
    if text.startswith("[") and "]:" in text:
        host, port = text.rsplit(":", 1)
    elif text.count(":") == 1:
        candidate_host, candidate_port = text.rsplit(":", 1)
        if candidate_port.isdigit():
            host, port = candidate_host, candidate_port
    marker = _hash_marker("endpoint", host)
    return f"{marker}:{port}" if port else marker


def _redact_path_endpoints(value: str) -> str:
    return PATH_ENDPOINT_RE.sub(
        lambda match: (
            f"{_hash_marker('endpoint', match.group('host'))}:{match.group('port')}"
        ),
        value,
    )


def _redact_embedded_path(match: re.Match[str]) -> str:
    raw = match.group(0)
    path = raw.rstrip(".,;:)]}")
    suffix = raw[len(path):]
    return _path_marker(path) + suffix


def _redact_string(value: str, key: str) -> str:
    normalized_key = _key_name(key)
    if SECRET_KEY_RE.search(normalized_key):
        return "<redacted-secret>"
    if normalized_key in IDENTITY_KEYS:
        return _hash_marker("identity", value)
    if normalized_key in HASHED_TEXT_KEYS:
        return _hash_marker(normalized_key, value)
    if normalized_key in ENDPOINT_KEYS:
        return _redact_endpoint(value)
    if normalized_key in QUERY_KEYS:
        return _hash_marker("query", value) if value else value
    if normalized_key == "query" and ("=" in value or "&" in value):
        return _hash_marker("query", value) if value else value
    if normalized_key in PATH_KEYS:
        if PATH_ENDPOINT_RE.search(value):
            return _redact_path_endpoints(value)
        if _looks_like_path(value):
            return _path_marker(value)
    if WINDOWS_PATH_RE.search(value):
        return _path_marker(value)
    if value.lower().startswith(("http://", "https://")):
        value = _redact_url(value)
    else:
        value = _redact_urls_in_text(value)
    value = EMBEDDED_WINDOWS_PATH_RE.sub(_redact_embedded_path, value)
    value = EMBEDDED_POSIX_PATH_RE.sub(_redact_embedded_path, value)
    value = BEARER_RE.sub("Bearer <redacted>", value)
    value = SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>", value
    )
    return value


def redact_default(value: Any, *, key: str = "") -> Any:
    """Recursively redact values while preserving dict/list/scalar shapes."""
    if isinstance(value, Mapping):
        return {
            str(child_key): redact_default(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_default(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_default(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_string(value, key)
    return value


def redact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached, default-safe result envelope."""
    redacted = redact_default(deepcopy(dict(result)))
    raw = redacted.get("raw")
    if isinstance(raw, dict):
        privacy = raw.setdefault("privacy", {})
        if isinstance(privacy, dict):
            privacy.update({
                "default_redaction": True,
                "shape_preserved": True,
            })
    return redacted


__all__ = ["redact_default", "redact_result"]