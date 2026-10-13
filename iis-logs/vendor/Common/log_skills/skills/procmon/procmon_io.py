"""Bounded discovery and streaming input for Process Monitor exports."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol


DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ROWS = 5_000_000
HARD_MAX_FILES = 100
HARD_MAX_BYTES = 8 * 1024 * 1024 * 1024
HARD_MAX_ROWS = 20_000_000
DEFAULT_MAX_DISCOVERY_DIRECTORIES = 2_000
DEFAULT_MAX_DISCOVERY_CANDIDATES = 5_000
HARD_MAX_DISCOVERY_DIRECTORIES = 20_000
HARD_MAX_DISCOVERY_CANDIDATES = 50_000
HEADER_PEEK_BYTES = 256 * 1024

_HEADER_ALIASES = {
    "timestamp": {"time of day", "date & time", "date and time"},
    "process_name": {"process name"},
    "parent_pid": {"parent pid"},
    "pid": {"pid"},
    "tid": {"tid"},
    "operation": {"operation"},
    "result": {"result"},
    "path": {"path"},
    "detail": {"detail"},
    "duration": {"duration"},
    "user": {"user"},
    "command_line": {"command line"},
    "session": {"session"},
    "category": {"category"},
}
_REQUIRED_HEADERS = {
    "timestamp", "process_name", "pid", "operation", "result", "path", "detail",
}


def _normalise_header(value: str) -> str:
    return " ".join(value.strip().lstrip("\ufeff").lower().split())


def _canonical_headers(fieldnames: Iterable[str | None]) -> dict[str, str]:
    aliases = {
        alias: canonical
        for canonical, values in _HEADER_ALIASES.items()
        for alias in values
    }
    result: dict[str, str] = {}
    for fieldname in fieldnames:
        if fieldname is None:
            continue
        canonical = aliases.get(_normalise_header(fieldname))
        if canonical:
            result[fieldname] = canonical
    return result


def detect_encoding(path: Path) -> str:
    """Detect encodings emitted by Procmon without reading the full capture."""
    with path.open("rb") as stream:
        head = stream.read(4096)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if head:
        even_nuls = head[0::2].count(0)
        odd_nuls = head[1::2].count(0)
        pairs = max(1, len(head) // 2)
        if odd_nuls / pairs > 0.30:
            return "utf-16-le"
        if even_nuls / pairs > 0.30:
            return "utf-16-be"
    try:
        head.decode("utf-8", errors="strict")
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp1252"


def _dialect(path: Path, encoding: str) -> type[csv.Dialect] | csv.Dialect:
    with path.open("rb") as stream:
        sample_bytes = stream.read(HEADER_PEEK_BYTES)
    sample = sample_bytes.decode(encoding, errors="replace")
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def inspect_csv(path: Path) -> tuple[bool, dict[str, Any]]:
    """Return whether a CSV has the structural shape of a Procmon export."""
    try:
        encoding = detect_encoding(path)
        dialect = _dialect(path, encoding)
        with path.open("r", encoding=encoding, errors="replace", newline="") as stream:
            reader = csv.reader(stream, dialect=dialect)
            headers = next(reader, [])
    except (OSError, csv.Error, UnicodeError) as exc:
        return False, {"status": "read_failed", "error": str(exc)}
    canonical = _canonical_headers(headers)
    present = set(canonical.values())
    missing = sorted(_REQUIRED_HEADERS - present)
    return not missing, {
        "status": "success" if not missing else "unsupported_schema",
        "encoding": encoding,
        "headers": sorted(present),
        "missing_headers": missing,
    }


def classify_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pml":
        return "pml"
    if suffix == ".csv" and path.is_file():
        valid, _ = inspect_csv(path)
        return "csv" if valid else "unknown"
    return "unknown"


def _iter_folder_candidates(
    root: Path,
    *,
    recursive: bool,
    max_directories: int,
    max_candidates: int,
) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    candidates_seen = 0
    if not recursive:
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and entry.suffix.casefold() in {".csv", ".pml"}:
                yield entry
                candidates_seen += 1
                if candidates_seen >= max_candidates:
                    return
        return

    directories_seen = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directories_seen += 1
        if directories_seen > max_directories:
            return
        current_path = Path(current)
        directory_names[:] = sorted(
            (
                name for name in directory_names
                if not (current_path / name).is_symlink()
            ),
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            candidate = current_path / name
            if candidate.suffix.casefold() not in {".csv", ".pml"}:
                continue
            yield candidate
            candidates_seen += 1
            if candidates_seen >= max_candidates:
                return


def discover_sources(
    explicit_paths: Iterable[str | os.PathLike[str]] = (),
    folder: str | os.PathLike[str] | None = None,
    *,
    recursive: bool = True,
    max_files: int = DEFAULT_MAX_FILES,
    max_directories: int = DEFAULT_MAX_DISCOVERY_DIRECTORIES,
    max_candidates: int = DEFAULT_MAX_DISCOVERY_CANDIDATES,
) -> list[Path]:
    """Discover only structurally valid Procmon CSV files and PML captures."""
    limit = max(1, min(int(max_files), HARD_MAX_FILES))
    directory_limit = max(1, min(int(max_directories), HARD_MAX_DISCOVERY_DIRECTORIES))
    candidate_limit = max(1, min(int(max_candidates), HARD_MAX_DISCOVERY_CANDIDATES))
    explicit_candidates = [Path(value) for value in explicit_paths]
    folder_candidates: Iterable[Path] = ()
    if folder:
        root = Path(folder)
        folder_candidates = _iter_folder_candidates(
            root,
            recursive=recursive,
            max_directories=directory_limit,
            max_candidates=candidate_limit,
        )
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in chain(explicit_candidates, folder_candidates):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if classify_source(resolved) in {"csv", "pml"}:
            result.append(resolved)
            if len(result) >= limit:
                break
    return result


class _LimitedRawReader(io.RawIOBase):
    def __init__(self, source: io.BufferedReader, limit: int):
        self._source = source
        self._remaining = max(0, limit)
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        if self._remaining <= 0:
            return 0
        data = self._source.read(min(len(buffer), self._remaining))
        if not data:
            return 0
        size = len(data)
        buffer[:size] = data
        self._remaining -= size
        self.bytes_read += size
        return size


@dataclass
class CsvReadResult:
    source: str
    status: str = "success"
    source_bytes: int = 0
    bytes_read: int = 0
    rows_seen: int = 0
    invalid_rows: int = 0
    truncated: bool = False
    stop_reason: str | None = None
    encoding: str | None = None
    columns: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PmlExportResult:
    source: str
    status: str
    output_csv: str | None = None
    executable_name: str | None = None
    executable_sha256: str | None = None
    exit_code: int | None = None
    elapsed_seconds: float | None = None
    stderr_sha256: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _CompletedProcess(Protocol):
    returncode: int
    stdout: str
    stderr: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_procmon_executable(path: str | os.PathLike[str] | None) -> tuple[Path | None, str | None]:
    if not path:
        return None, "No trusted Procmon executable was supplied."
    executable = Path(path)
    if not executable.is_file():
        return None, "The trusted Procmon executable does not exist."
    if executable.name.casefold() not in {"procmon.exe", "procmon64.exe", "procmon64a.exe"}:
        return None, "The trusted executable must be Procmon.exe, Procmon64.exe, or Procmon64a.exe."
    return executable.resolve(), None


def export_pml(
    pml_path: Path,
    output_csv: Path,
    procmon_exe: str | os.PathLike[str] | None,
    *,
    timeout_seconds: int = 300,
    runner: Callable[..., _CompletedProcess] = subprocess.run,
) -> PmlExportResult:
    """Export PML with an operator-supplied Procmon binary; never invokes a shell."""
    pml_path = Path(pml_path)
    output_csv = Path(output_csv)
    executable, validation_error = validate_procmon_executable(procmon_exe)
    if validation_error:
        return PmlExportResult(
            source=str(pml_path), status="tool_unavailable", error=validation_error,
        )
    if not pml_path.is_file():
        return PmlExportResult(
            source=str(pml_path), status="missing_input", error="PML input does not exist.",
        )
    if pml_path.suffix.casefold() != ".pml":
        return PmlExportResult(
            source=str(pml_path), status="unsupported_schema", error="Input is not a PML capture.",
        )
    assert executable is not None
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    required_bytes = pml_path.stat().st_size * 4 + 64 * 1024 * 1024
    try:
        free_bytes = shutil.disk_usage(output_csv.parent).free
    except OSError:
        free_bytes = required_bytes
    if free_bytes < required_bytes:
        return PmlExportResult(
            source=str(pml_path),
            status="insufficient_disk_space",
            executable_name=executable.name,
            executable_sha256=_sha256_file(executable),
            error="Temporary storage is insufficient for a conservative PML-to-CSV export.",
        )

    started = time.monotonic()
    command = [
        str(executable), "/Quiet", "/OpenLog", str(pml_path),
        "/SaveAs", str(output_csv),
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return PmlExportResult(
            source=str(pml_path),
            status="conversion_timeout",
            executable_name=executable.name,
            executable_sha256=_sha256_file(executable),
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=f"PML export exceeded {timeout_seconds} seconds.",
        )
    except OSError as exc:
        return PmlExportResult(
            source=str(pml_path),
            status="conversion_failed",
            executable_name=executable.name,
            executable_sha256=_sha256_file(executable),
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=f"Procmon could not be started: {exc}",
        )

    stderr = completed.stderr or ""
    result = PmlExportResult(
        source=str(pml_path),
        status="success" if completed.returncode == 0 else "conversion_failed",
        output_csv=str(output_csv) if completed.returncode == 0 else None,
        executable_name=executable.name,
        executable_sha256=_sha256_file(executable),
        exit_code=completed.returncode,
        elapsed_seconds=round(time.monotonic() - started, 3),
        stderr_sha256=(
            hashlib.sha256(stderr.encode("utf-8", errors="replace")).hexdigest()
            if stderr else None
        ),
        error=(None if completed.returncode == 0 else "Procmon returned a non-zero exit code."),
    )
    if result.status == "success":
        valid, inspection = inspect_csv(output_csv)
        if not valid:
            result.status = "unsupported_schema"
            result.output_csv = None
            result.error = (
                "Procmon export did not produce a supported CSV: "
                + ", ".join(inspection.get("missing_headers") or [])
            )
    return result


def stream_csv(
    path: Path,
    consume: Callable[[dict[str, str]], None],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    deadline_monotonic: float | None = None,
) -> CsvReadResult:
    """Stream a Procmon CSV into ``consume`` while enforcing hard budgets."""
    path = Path(path)
    result = CsvReadResult(source=str(path))
    byte_limit = max(1, min(int(max_bytes), HARD_MAX_BYTES))
    row_limit = max(1, min(int(max_rows), HARD_MAX_ROWS))
    try:
        result.source_bytes = path.stat().st_size
        valid, inspection = inspect_csv(path)
        result.encoding = inspection.get("encoding")
        result.columns = list(inspection.get("headers") or [])
        if not valid:
            result.status = str(inspection.get("status") or "unsupported_schema")
            result.error = (
                "Missing required Procmon columns: "
                + ", ".join(inspection.get("missing_headers") or [])
            )
            return result

        encoding = result.encoding or detect_encoding(path)
        dialect = _dialect(path, encoding)
        with path.open("rb") as source:
            limited = _LimitedRawReader(source, min(byte_limit, result.source_bytes))
            buffered = io.BufferedReader(limited, buffer_size=64 * 1024)
            with io.TextIOWrapper(
                buffered, encoding=encoding, errors="replace", newline=""
            ) as text_stream:
                reader = csv.DictReader(text_stream, dialect=dialect)
                canonical = _canonical_headers(reader.fieldnames or [])
                for raw_row in reader:
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        result.truncated = True
                        result.stop_reason = "deadline_exhausted"
                        break
                    if result.rows_seen >= row_limit:
                        result.truncated = True
                        result.stop_reason = "row_budget_exhausted"
                        break
                    if None in raw_row:
                        result.invalid_rows += 1
                        continue
                    row = {
                        canonical[field]: (value or "").strip()
                        for field, value in raw_row.items()
                        if field in canonical
                    }
                    if not row.get("process_name") or not row.get("operation"):
                        result.invalid_rows += 1
                        continue
                    row["source_file"] = path.name
                    row["line_number"] = str(reader.line_num)
                    consume(row)
                    result.rows_seen += 1
            result.bytes_read = limited.bytes_read

        if result.source_bytes > byte_limit:
            result.truncated = True
            result.stop_reason = result.stop_reason or "byte_budget_exhausted"
        if result.truncated:
            result.status = "budget_exhausted"
    except (OSError, csv.Error, UnicodeError, ValueError) as exc:
        result.status = "read_failed"
        result.error = str(exc)
    return result


__all__ = [
    "CsvReadResult", "PmlExportResult", "DEFAULT_MAX_BYTES", "DEFAULT_MAX_FILES", "DEFAULT_MAX_ROWS",
    "DEFAULT_MAX_DISCOVERY_CANDIDATES", "DEFAULT_MAX_DISCOVERY_DIRECTORIES",
    "HARD_MAX_BYTES", "HARD_MAX_FILES", "HARD_MAX_ROWS", "classify_source",
    "detect_encoding", "discover_sources", "export_pml", "inspect_csv", "stream_csv",
    "validate_procmon_executable",
]