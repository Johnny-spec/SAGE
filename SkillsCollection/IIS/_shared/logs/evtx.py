"""
_shared/logs/evtx.py — canonical reader for Windows Event Logs.

Wraps Get-WinEvent (live + .evtx file) and exposes a filter dataclass.
Interpretation tables (EVENT_ID_MAP, ROOT_CAUSE_HINTS) stay in the skill;
this module only does I/O + light projection + filter.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "EvtxFilter", "EvtxReadResult",
    "discover", "iter_entries",
    "summarise", "apply_filter", "around_window", "query",
    "query_live", "query_file", "query_live_result", "query_file_result",
    "has_valid_evtx_header",
]

EVTX_MAGIC = b"ElfFile\x00"


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

@dataclass
class EvtxFilter:
    """Filter spec for Windows event log entries."""
    log_names: list[str] = field(default_factory=list)       # only used in live mode
    event_ids: list[int] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    levels: list[int] = field(default_factory=lambda: [1, 2, 3])  # 1=critical 2=error 3=warning
    keywords: list[str] = field(default_factory=list)         # substring on Message

    def is_empty(self) -> bool:
        return not any((self.event_ids, self.providers, self.keywords))

    def matches(self, e: dict[str, Any]) -> bool:
        if self.event_ids and int(e.get("event_id", 0) or 0) not in self.event_ids:
            return False
        if self.providers and not any(
            p.lower() in str(e.get("provider", "")).lower() for p in self.providers
        ):
            return False
        if self.keywords and not any(
            k.lower() in str(e.get("message", "")).lower() for k in self.keywords
        ):
            return False
        return True


@dataclass
class EvtxReadResult:
    status: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Discovery (.evtx files only)
# ---------------------------------------------------------------------------

def discover(folder: Path | str, recursive: bool = True) -> list[Path]:
    folder = Path(folder)
    if folder.is_file():
        return [folder] if folder.suffix.lower() == ".evtx" else []
    pattern = "**/*.evtx" if recursive else "*.evtx"
    return sorted(folder.glob(pattern))


# ---------------------------------------------------------------------------
# Low-level PowerShell shell-out
# ---------------------------------------------------------------------------

def _ps_quote(value: str) -> str:
    """Return a PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def has_valid_evtx_header(path: Path) -> bool:
    try:
        with Path(path).open("rb") as stream:
            return stream.read(len(EVTX_MAGIC)) == EVTX_MAGIC
    except OSError:
        return False


def _run_ps_result(ps: str, timeout: int) -> EvtxReadResult:
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        + ps
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        return EvtxReadResult(status="tool_unavailable", error="PowerShell was not found.")
    except subprocess.TimeoutExpired:
        return EvtxReadResult(status="timeout", error=f"Event query timed out after {timeout}s.")
    except OSError as exc:
        return EvtxReadResult(status="tool_unavailable", error=f"PowerShell failed to start: {exc}")
    stdout = proc.stdout or ""
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        status = (
            "permission_blocked"
            if any(token in stderr for token in (
                "access is denied", "access denied", "unauthorized", "permission"
            ))
            else "query_failed"
        )
        return EvtxReadResult(
            status=status,
            error=(
                "Event Log access was denied."
                if status == "permission_blocked"
                else f"PowerShell event query exited with code {proc.returncode}."
            ),
        )
    if not stdout.strip():
        return EvtxReadResult(status="success", entries=[])
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return EvtxReadResult(status="parse_failed", error="PowerShell returned invalid JSON.")
    entries = data if isinstance(data, list) else [data]
    if not all(isinstance(entry, dict) for entry in entries):
        return EvtxReadResult(status="parse_failed", error="Event JSON rows must be objects.")
    return EvtxReadResult(status="success", entries=entries)


def _run_ps(ps: str, timeout: int) -> list[dict[str, Any]]:
    """Legacy list API; diagnostics should use the status-preserving helpers."""
    return _run_ps_result(ps, timeout).entries


def _project(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project Get-WinEvent objects to a stable schema."""
    out: list[dict[str, Any]] = []
    for ev in events:
        out.append({
            "timestamp":     ev.get("TimeCreated"),
            "event_id":      int(ev.get("Id", 0) or 0),
            "provider":      ev.get("ProviderName"),
            "level_display": ev.get("LevelDisplayName"),
            "message":       (ev.get("Message") or "")[:1000],
            "machine":       ev.get("MachineName"),
            "record_id":     ev.get("RecordId"),
            "properties":    ev.get("Properties") or "",
            "source":        ev.get("SourcePath") or ev.get("LogName"),
        })
    return out


def _select_projection() -> str:
    return (
        " Select-Object @{n='TimeCreated';e={$_.TimeCreated.ToString('o')}},"
        "                Id, ProviderName, LevelDisplayName, RecordId,"
        "                @{n='MachineName';e={$_.MachineName}},"
        "                @{n='Properties';e={($_.Properties | ForEach-Object { [string]$_.Value }) -join ' || ' }},"
        "                @{n='Message';e={$_.Message -replace \"`r`n\",' ' }}"
    )


def query_live(log_name: str,
               start: datetime,
               end: datetime,
               *,
               levels: Sequence[int] = (1, 2, 3),
               timeout: int = 45) -> list[dict[str, Any]]:
    level_csv = ",".join(str(int(l)) for l in levels)
    ps = (
        "$ErrorActionPreference='Stop';try {"
        "$f=@{LogName=%(log)s; StartTime=[datetime]'%(start)s';"
        " EndTime=[datetime]'%(end)s'; Level=%(levels)s};"
        "$events=@(Get-WinEvent -FilterHashtable $f -ErrorAction Stop);"
        "if(-not $events){'[]'}else{$events |"
        + _select_projection() + " |"
        " ConvertTo-Json -Depth 3 -Compress}"
        "}catch{if($_.Exception.Message -match 'No events were found'){'[]';exit 0};"
        "[Console]::Error.Write($_.Exception.Message);exit 3}"
    ) % {"log": _ps_quote(log_name),
         "start": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end":   end.strftime("%Y-%m-%d %H:%M:%S"),
         "levels": level_csv}
    return query_live_result(log_name, start, end, levels=levels, timeout=timeout).entries


def query_live_result(log_name: str,
                      start: datetime,
                      end: datetime,
                      *,
                      levels: Sequence[int] = (1, 2, 3),
                      timeout: int = 45) -> EvtxReadResult:
    level_csv = ",".join(str(int(level)) for level in levels)
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$f=@{LogName=%(log)s; StartTime=[datetime]'%(start)s';"
        " EndTime=[datetime]'%(end)s'; Level=%(levels)s};"
        "Get-WinEvent -FilterHashtable $f -ErrorAction SilentlyContinue |"
        + _select_projection() + " |"
        " ConvertTo-Json -Depth 3 -Compress"
    ) % {"log": _ps_quote(log_name),
         "start": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end": end.strftime("%Y-%m-%d %H:%M:%S"),
         "levels": level_csv}
    result = _run_ps_result(ps, timeout)
    result.entries = _project(result.entries)
    return result


def query_file(evtx_path: Path,
               start: datetime,
               end: datetime,
               *,
               levels: Sequence[int] = (1, 2, 3),
               timeout: int = 60) -> list[dict[str, Any]]:
    level_csv = ",".join(str(int(l)) for l in levels)
    ps = (
        "$ErrorActionPreference='Stop';try {"
        "$f=@{Path=%(path)s; StartTime=[datetime]'%(start)s';"
        " EndTime=[datetime]'%(end)s'; Level=%(levels)s};"
        "$events=@(Get-WinEvent -FilterHashtable $f -ErrorAction Stop);"
        "if(-not $events){'[]'}else{$events |"
        " ForEach-Object { $_ | Add-Member -NotePropertyName SourcePath -NotePropertyValue %(path)s -PassThru } |"
        + _select_projection() + ", SourcePath |"
        " ConvertTo-Json -Depth 3 -Compress}"
        "}catch{if($_.Exception.Message -match 'No events were found'){'[]';exit 0};"
        "[Console]::Error.Write($_.Exception.Message);exit 3}"
    ) % {"path":  _ps_quote(str(evtx_path)),
         "start": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end":   end.strftime("%Y-%m-%d %H:%M:%S"),
         "levels": level_csv}
    return query_file_result(evtx_path, start, end, levels=levels, timeout=timeout).entries


def query_file_result(evtx_path: Path,
                      start: datetime,
                      end: datetime,
                      *,
                      levels: Sequence[int] = (1, 2, 3),
                      timeout: int = 60) -> EvtxReadResult:
    evtx_path = Path(evtx_path)
    if not evtx_path.is_file():
        return EvtxReadResult(status="missing_input", error=f"EVTX file not found: {evtx_path.name}")
    if not has_valid_evtx_header(evtx_path):
        return EvtxReadResult(
            status="unsupported_schema",
            error=f"File is not a valid EVTX capture: {evtx_path.name}",
        )
    level_csv = ",".join(str(int(level)) for level in levels)
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$f=@{Path=%(path)s; StartTime=[datetime]'%(start)s';"
        " EndTime=[datetime]'%(end)s'; Level=%(levels)s};"
        "Get-WinEvent -FilterHashtable $f -ErrorAction SilentlyContinue |"
        " ForEach-Object { $_ | Add-Member -NotePropertyName SourcePath -NotePropertyValue %(path)s -PassThru } |"
        + _select_projection() + ", SourcePath |"
        " ConvertTo-Json -Depth 3 -Compress"
    ) % {"path": _ps_quote(str(evtx_path)),
         "start": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end": end.strftime("%Y-%m-%d %H:%M:%S"),
         "levels": level_csv}
    result = _run_ps_result(ps, timeout)
    result.entries = _project(result.entries)
    return result


def iter_entries(path: Path,
                 *,
                 start: datetime | None = None,
                 end: datetime | None = None) -> list[dict[str, Any]]:
    """File-mode helper; for live mode use query()."""
    if start is None or end is None:
        end = end or datetime.now()
        start = start or (end - timedelta(hours=1))
    return query_file(Path(path), start, end)


# ---------------------------------------------------------------------------
# Filter / window helpers
# ---------------------------------------------------------------------------

def apply_filter(entries: Iterable[dict[str, Any]],
                 spec: EvtxFilter | None) -> list[dict[str, Any]]:
    if spec is None or spec.is_empty():
        return list(entries)
    return [e for e in entries if spec.matches(e)]


def _parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def around_window(entries: Iterable[dict[str, Any]],
                  anchor: datetime,
                  window_seconds: int) -> list[dict[str, Any]]:
    lo = anchor - timedelta(seconds=window_seconds)
    hi = anchor + timedelta(seconds=window_seconds)
    out: list[dict[str, Any]] = []
    for e in entries:
        ts = _parse_ts(str(e.get("timestamp", "")))
        if ts is None:
            continue
        # Compare naively if anchor is naive
        if anchor.tzinfo is None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if lo <= ts <= hi:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {"count": 0}
    ids = Counter(int(e.get("event_id", 0) or 0) for e in entries)
    providers = Counter(e.get("provider", "-") for e in entries)
    levels = Counter(e.get("level_display", "-") for e in entries)
    return {
        "count": len(entries),
        "top_event_ids": ids.most_common(10),
        "top_providers": providers.most_common(10),
        "level_distribution": dict(levels),
    }


# ---------------------------------------------------------------------------
# Unified query()
# ---------------------------------------------------------------------------

def query(sources: Iterable[Path | str] | None = None,
          *,
          filter: EvtxFilter | None = None,
          time_range: tuple[datetime | None, datetime | None] | None = None,
          around: datetime | None = None,
          window_seconds: int = 300,
          limit: int | None = None,
          live_logs: Sequence[str] = ()) -> dict[str, Any]:
    """Query event logs from .evtx files and/or live logs.

    - `sources` is an iterable of .evtx paths (offline mode).
    - `live_logs` is a list of live log names (e.g. ["Application","System"]).
    - `time_range` is required when querying live logs; defaults to last hour otherwise.
    """
    if time_range and (time_range[0] or time_range[1]):
        start, end = time_range
    else:
        end = datetime.now()
        start = end - timedelta(hours=1)
    if start is None:
        start = end - timedelta(hours=1)
    if end is None:
        end = datetime.now()

    entries: list[dict[str, Any]] = []
    acquisitions: list[dict[str, Any]] = []
    src_files = [Path(p) for p in (sources or [])]
    for p in src_files:
        read = query_file_result(p, start, end)
        entries.extend(read.entries)
        acquisitions.append({
            "source": p.name,
            "mode": "offline",
            "status": read.status,
            "record_count": len(read.entries),
            "error": read.error,
        })

    for name in live_logs:
        levels = filter.levels if filter else (1, 2, 3)
        read = query_live_result(name, start, end, levels=levels)
        entries.extend(read.entries)
        acquisitions.append({
            "source": name,
            "mode": "live",
            "status": read.status,
            "record_count": len(read.entries),
            "error": read.error,
        })

    if around is not None:
        entries = around_window(entries, around, window_seconds)

    if filter is not None and not filter.is_empty():
        entries = apply_filter(entries, filter)

    truncated = False
    if limit is not None and len(entries) > limit:
        truncated = True
        entries = entries[:limit]

    statuses = {item["status"] for item in acquisitions}
    acquisition_status = (
        "no_input" if not acquisitions
        else "success" if statuses == {"success"}
        else "partial" if "success" in statuses
        else next(iter(statuses)) if len(statuses) == 1
        else "failed"
    )
    return {
        "status": acquisition_status,
        "summary": summarise(entries),
        "entries": entries,
        "truncated": truncated,
        "source_files": [str(p) for p in src_files],
        "live_logs": list(live_logs),
        "acquisitions": acquisitions,
        "quality": {
            "capture_complete": acquisition_status == "success",
            "records_seen": len(entries),
            "truncated": truncated,
        },
        "time_range": {
            "start": start.strftime("%Y-%m-%d %H:%M:%S") if start else None,
            "end":   end.strftime("%Y-%m-%d %H:%M:%S")   if end   else None,
        },
    }
