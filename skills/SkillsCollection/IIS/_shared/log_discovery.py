"""
_shared/log_discovery.py

Recursively discover log files under a folder and classify each as one of:
  * iis_w3c   — Microsoft Internet Information Services W3C-formatted logs
  * httperr   — HTTP.SYS error logs (httperr*.log)
  * ftp_w3c   — Microsoft FTP Service W3C-formatted logs
  * evtx      — Windows Event Viewer offline export (*.evtx, binary)
  * netlog    — Chromium net-export JSON (edge://net-export / chrome://net-export)
    * procmon   — Process Monitor PML or structurally validated CSV export
  * unknown   — log files we cannot identify

Classification is driven by the `#Software:` directive (read from the first
~20 non-blank lines of each file) with a filename-based fallback for HTTPERR
logs, which historically may or may not advertise themselves via the header.
`.evtx` files are classified purely by extension (they are binary).
`*.json` files are classified by structural sniff (top of file mentions
`"constants"` and `"logSourceType"`, the Chromium net-export shape).

Pure standard library; importable from any cwd.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

LogKind = str  # "iis_w3c" | "httperr" | "ftp_w3c" | "evtx" | "netlog" | "procmon" | "unknown"

IIS_KIND     = "iis_w3c"
HTTPERR_KIND = "httperr"
FTP_KIND     = "ftp_w3c"
EVTX_KIND    = "evtx"
NETLOG_KIND  = "netlog"
PROCMON_KIND = "procmon"
UNKNOWN_KIND = "unknown"

_HEADER_SCAN_LINES = 20
_DEFAULT_GLOBS    = ("*.log", "*.evtx", "*.json", "*.csv", "*.pml")
# Modern Edge/Chrome net-export files put a large `activeFieldTrialGroups`
# array (often 30–80 KB) inside `constants` before `logSourceType`. A small
# peek is therefore not sufficient; we read up to 1 MB.
_NETLOG_PEEK_BYTES = 1_048_576
_PROCMON_PEEK_BYTES = 262_144
_PROCMON_REQUIRED_FIELDS = {
    "time of day", "process name", "pid", "operation", "result", "path", "detail",
}


@dataclass
class Discovery:
    """Result of scanning a path."""
    by_kind: dict[LogKind, list[Path]] = field(default_factory=dict)

    def get(self, kind: LogKind) -> list[Path]:
        return self.by_kind.get(kind, [])

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_kind.values())


_FTP_FIELDS = frozenset({"x-session", "x-fullpath"})


def _looks_like_netlog_json(path: Path) -> bool:
    """Cheap structural sniff for Chromium net-export JSON files.

    Net-export files start with `{"constants":{...}` and contain
    `logSourceType` and `logEventTypes` somewhere inside `constants`. A
    small peek can miss `logSourceType` because Edge prepends a long
    `activeFieldTrialGroups` array. We read up to 1 MB and accept the file
    when we see *either* marker plus the leading `"constants"` key.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(_NETLOG_PEEK_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return False
    if '"constants"' not in head[:256]:
        # Real net-export always begins with `{"constants":` — bail fast.
        return False
    return ('"logSourceType"' in head
            or '"logEventTypes"' in head
            or '"clientInfo"' in head)


def _looks_like_procmon_csv(path: Path) -> bool:
    """Structurally identify Procmon CSV without accepting arbitrary CSV files."""
    try:
        with path.open("rb") as stream:
            data = stream.read(_PROCMON_PEEK_BYTES)
    except OSError:
        return False
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data[1::2].count(0) > max(2, len(data) // 10):
        encoding = "utf-16-le"
    else:
        encoding = "utf-8-sig"
    text = data.decode(encoding, errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        headers = next(csv.reader(io.StringIO(text), dialect=dialect), [])
    except csv.Error:
        return False
    normalized = {" ".join(value.strip().lstrip("\ufeff").lower().split()) for value in headers}
    has_time = bool(normalized & {"time of day", "date & time", "date and time"})
    return has_time and (_PROCMON_REQUIRED_FIELDS - {"time of day"}) <= normalized


def classify_file(path: Path) -> LogKind:
    """Inspect a single file and return its kind."""
    name_lower = path.name.lower()
    # .evtx is binary; classify by extension only.
    if name_lower.endswith(".evtx"):
        return EVTX_KIND
    if name_lower.endswith(".pml"):
        return PROCMON_KIND
    if name_lower.endswith(".csv"):
        return PROCMON_KIND if _looks_like_procmon_csv(path) else UNKNOWN_KIND
    # .json — only classify as netlog if it looks structurally right.
    if name_lower.endswith(".json"):
        return NETLOG_KIND if _looks_like_netlog_json(path) else UNKNOWN_KIND
    # HTTPERR files almost always match this pattern even when no header is present.
    if name_lower.startswith("httperr"):
        return HTTPERR_KIND

    try:
        sw_kind: LogKind | None = None
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_HEADER_SCAN_LINES):
                line = fh.readline()
                if not line:
                    break
                if not line.startswith("#"):
                    break
                if line.startswith("#Software:"):
                    sw = line[len("#Software:"):].strip().lower()
                    if "ftp" in sw:
                        return FTP_KIND
                    if "internet information services" in sw or "iis" in sw:
                        # Might be IIS HTTP or IIS FTP; defer until we
                        # inspect #Fields for FTP-specific columns.
                        sw_kind = IIS_KIND
                    elif "httpapi" in sw or "http api" in sw:
                        return HTTPERR_KIND
                elif line.startswith("#Fields:"):
                    fields = set(line[len("#Fields:"):].strip().lower().split())
                    if fields & _FTP_FIELDS:
                        return FTP_KIND
        if sw_kind is not None:
            return sw_kind
    except OSError:
        return UNKNOWN_KIND
    return UNKNOWN_KIND


def _iter_log_paths(root: Path, recursive: bool,
                    globs: tuple[str, ...] = _DEFAULT_GLOBS) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    seen: set[Path] = set()
    for g in globs:
        pattern = f"**/{g}" if recursive else g
        for p in sorted(root.glob(pattern)):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def discover_logs(root: Path, recursive: bool = True,
                  extra_globs: tuple[str, ...] | None = None) -> Discovery:
    """
    Walk `root` (file or directory) and classify every log file found.

    By default, `*.log` and `*.evtx` files are scanned. Pass `extra_globs` to
    override (e.g. `("*.log", "*.txt")`).

    Returns a Discovery whose `by_kind` maps each detected kind to a sorted
    list of paths. Empty kinds are omitted.
    """
    globs = extra_globs if extra_globs is not None else _DEFAULT_GLOBS
    out: dict[LogKind, list[Path]] = {}
    for path in _iter_log_paths(root, recursive=recursive, globs=globs):
        kind = classify_file(path)
        out.setdefault(kind, []).append(path)
    # Stable ordering per bucket.
    for k in out:
        out[k].sort()
    return Discovery(by_kind=out)


__all__ = [
    "Discovery", "LogKind",
    "IIS_KIND", "HTTPERR_KIND", "FTP_KIND", "EVTX_KIND", "NETLOG_KIND", "PROCMON_KIND", "UNKNOWN_KIND",
    "classify_file", "discover_logs",
]
