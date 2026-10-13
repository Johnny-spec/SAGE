"""Content scanning port and ClamAV implementation for uploaded artifacts."""

import socket
from pathlib import Path
from typing import Protocol


class ContentScanError(ValueError):
    """Raised when content is unsafe or cannot be scanned under the active policy."""


class ContentScanner(Protocol):
    def require_clean(self, path: Path) -> None: ...


class AllowAllContentScanner:
    """Development scanner used only when scanning is not required."""

    def require_clean(self, path: Path) -> None:
        del path


class RejectingContentScanner:
    """Fail-closed scanner used when production scanning is required but unavailable."""

    def require_clean(self, path: Path) -> None:
        del path
        raise ContentScanError("Content scanning is required but no scanner is configured")


class ClamAvContentScanner:
    def __init__(
        self,
        host: str,
        *,
        port: int = 3310,
        timeout_seconds: float = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds

    def require_clean(self, path: Path) -> None:
        files = (
            sorted(item for item in path.rglob("*") if item.is_file() and not item.is_symlink())
            if path.is_dir()
            else [path]
        )
        for file_path in files:
            result = self._scan_file(file_path)
            if result.endswith("FOUND"):
                threat = result.rsplit(":", 1)[-1].removesuffix("FOUND").strip()
                raise ContentScanError(f"Malware detected: {threat or 'unknown threat'}")
            if not result.endswith("OK"):
                raise ContentScanError("Content scanner could not verify the upload")

    def _scan_file(self, path: Path) -> str:
        try:
            with socket.create_connection(
                (self._host, self._port),
                timeout=self._timeout_seconds,
            ) as connection:
                connection.settimeout(self._timeout_seconds)
                connection.sendall(b"zINSTREAM\0")
                with path.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        connection.sendall(len(chunk).to_bytes(4, "big"))
                        connection.sendall(chunk)
                connection.sendall((0).to_bytes(4, "big"))
                response = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\0" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise ContentScanError("Content scanner is unavailable") from exc
        return response.rstrip(b"\0\n").decode("utf-8", errors="replace")


def build_content_scanner(
    *,
    host: str | None,
    port: int,
    timeout_seconds: float,
    required: bool,
) -> ContentScanner:
    if host:
        return ClamAvContentScanner(
            host,
            port=port,
            timeout_seconds=timeout_seconds,
        )
    return RejectingContentScanner() if required else AllowAllContentScanner()