"""
Edge/_shared/sources/processes.py — enumerate running msedge.exe processes.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "ProcessFilter",
    "discover", "iter_entries",
    "summarise", "apply_filter", "around_window", "query",
    "count_msedge", "count_msedge_result", "ProcessCountResult",
    "ProcessListResult", "list_processes_result",
]


@dataclass
class ProcessFilter:
    min_cpu_seconds: float | None = None
    min_working_set_mb: float | None = None
    name_contains: str | None = None

    def is_empty(self) -> bool:
        return not any((self.min_cpu_seconds, self.min_working_set_mb, self.name_contains))

    def matches(self, e: dict[str, Any]) -> bool:
        if self.min_cpu_seconds is not None and float(e.get("cpu_seconds", 0) or 0) < self.min_cpu_seconds:
            return False
        if self.min_working_set_mb is not None and float(e.get("working_set_mb", 0) or 0) < self.min_working_set_mb:
            return False
        if self.name_contains and self.name_contains.lower() not in str(e.get("name", "")).lower():
            return False
        return True


@dataclass(frozen=True)
class ProcessCountResult:
    status: str
    count: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProcessListResult:
    status: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def discover(folder: Path | str | None = None,
             recursive: bool = True) -> list[str]:
    return ["msedge"]


def count_msedge_result() -> ProcessCountResult:
    """Return a process count without collapsing acquisition failures to zero."""
    if os.name != "nt":
        return ProcessCountResult(
            status="unsupported_platform",
            error="Edge process enumeration requires Windows.",
        )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Process msedge -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return ProcessCountResult(status="tool_unavailable", error="PowerShell was not found.")
    except subprocess.TimeoutExpired:
        return ProcessCountResult(status="timeout", error="Process enumeration timed out.")
    except OSError as exc:
        return ProcessCountResult(status="tool_unavailable", error=f"PowerShell failed to start: {exc}")
    if proc.returncode != 0:
        return ProcessCountResult(
            status="query_failed",
            error=f"PowerShell process query exited with code {proc.returncode}.",
        )
    text = (proc.stdout or "").strip()
    if not text:
        return ProcessCountResult(status="parse_failed", error="PowerShell returned no count.")
    try:
        return ProcessCountResult(status="success", count=int(text))
    except ValueError:
        return ProcessCountResult(status="parse_failed", error="PowerShell returned an invalid count.")


def count_msedge() -> int:
    """Legacy integer helper that refuses to turn acquisition failure into zero."""
    result = count_msedge_result()
    if result.count is None:
        raise RuntimeError(result.error or f"Process count failed: {result.status}")
    return result.count


def list_processes_result(
    process_name: str = "msedge", timeout: int = 20
) -> ProcessListResult:
    if os.name != "nt":
        return ProcessListResult(
            status="unsupported_platform",
            error="Process enumeration requires Windows.",
        )
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p = Get-Process %(name)s -ErrorAction SilentlyContinue;"
        "if (-not $p) { '[]'; return };"
        "$rows = foreach ($x in $p) {"
        "  [pscustomobject]@{"
        "    pid = $x.Id;"
        "    name = $x.ProcessName;"
        "    working_set_mb = [math]::Round($x.WorkingSet64 / 1MB, 2);"
        "    private_mb = [math]::Round($x.PrivateMemorySize64 / 1MB, 2);"
        "    cpu_seconds = if ($x.CPU) { [math]::Round($x.CPU, 2) } else { 0 };"
        "    started = if ($x.StartTime) { $x.StartTime.ToString('o') } else { $null };"
        "    threads = $x.Threads.Count;"
        "    handles = $x.HandleCount"
        "  }"
        "};"
        "$rows | ConvertTo-Json -Depth 3 -Compress"
    ) % {"name": process_name}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return ProcessListResult(status="tool_unavailable", error="PowerShell was not found.")
    except subprocess.TimeoutExpired:
        return ProcessListResult(status="timeout", error="Process enumeration timed out.")
    except OSError as exc:
        return ProcessListResult(status="tool_unavailable", error=f"PowerShell failed to start: {exc}")
    if proc.returncode != 0:
        return ProcessListResult(
            status="query_failed",
            error=f"PowerShell process query exited with code {proc.returncode}.",
        )
    if not proc.stdout.strip():
        return ProcessListResult(status="parse_failed", error="PowerShell returned no JSON output.")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ProcessListResult(status="parse_failed", error="PowerShell returned invalid JSON.")
    entries = data if isinstance(data, list) else [data]
    if not all(isinstance(entry, dict) for entry in entries):
        return ProcessListResult(status="parse_failed", error="Process JSON rows must be objects.")
    return ProcessListResult(status="success", entries=entries)


def _list_processes(process_name: str = "msedge", timeout: int = 20) -> list[dict[str, Any]]:
    """Legacy list API that refuses to collapse acquisition failure to []."""
    result = list_processes_result(process_name, timeout)
    if result.status != "success":
        raise RuntimeError(result.error or f"Process enumeration failed: {result.status}")
    return result.entries


def iter_entries(process_name: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield a verified snapshot or raise when acquisition did not complete."""
    for row in _list_processes(process_name or "msedge"):
        yield row


def apply_filter(entries: Iterable[dict[str, Any]],
                 spec: ProcessFilter | None) -> list[dict[str, Any]]:
    if spec is None or spec.is_empty():
        return list(entries)
    return [e for e in entries if spec.matches(e)]


def around_window(entries: Iterable[dict[str, Any]], *_args, **_kwargs) -> list[dict[str, Any]]:
    return list(entries)


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {"count": 0}
    ws = [float(e.get("working_set_mb", 0) or 0) for e in entries]
    cpu = [float(e.get("cpu_seconds", 0) or 0) for e in entries]
    return {
        "count": len(entries),
        "total_working_set_mb": round(sum(ws), 2),
        "max_working_set_mb": max(ws) if ws else 0,
        "total_cpu_seconds": round(sum(cpu), 2),
        "max_cpu_seconds": max(cpu) if cpu else 0,
    }


def query(sources: Iterable[str] | None = None,
          *,
          filter: ProcessFilter | None = None,
          time_range=None,
          around=None,
          window_seconds: int = 300,
          limit: int | None = None) -> dict[str, Any]:
    names = list(sources) if sources else ["msedge"]
    entries: list[dict[str, Any]] = []
    acquisitions: list[dict[str, Any]] = []
    for n in names:
        read = list_processes_result(n)
        entries.extend(read.entries)
        acquisitions.append({
            "process_name": n,
            "status": read.status,
            "record_count": len(read.entries),
            "error": read.error,
        })

    if filter is not None and not filter.is_empty():
        entries = apply_filter(entries, filter)

    truncated = False
    if limit is not None and len(entries) > limit:
        truncated = True
        entries = entries[:limit]

    statuses = {item["status"] for item in acquisitions}
    status = (
        "success" if statuses == {"success"}
        else "partial" if "success" in statuses
        else next(iter(statuses)) if len(statuses) == 1
        else "failed"
    )
    return {
        "status": status,
        "summary": summarise(entries),
        "entries": entries,
        "truncated": truncated,
        "process_names": names,
        "acquisitions": acquisitions,
        "quality": {
            "capture_complete": status == "success" and not truncated,
            "records_seen": len(entries),
            "truncated": truncated,
        },
    }
