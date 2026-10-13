#!/usr/bin/env python3
"""
edge_performance.py — sample live msedge.exe processes.

Captures per-PID CPU% (sampled over N seconds), working-set / private bytes,
handle and thread counts, and command-line role (renderer / gpu-process /
utility / extension / browser).

Pure stdlib + PowerShell.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _shared.contract import (  # noqa: E402
    Finding, SkillResult, load_context,
)
from _shared import playbook  # noqa: E402
from _shared.sources import processes as proc_src  # noqa: E402
from investigation_protocol import EvidenceState  # type: ignore[reportMissingImports]  # noqa: E402
_ = proc_src  # processes module shares the lightweight Get-Process wrapper used by
              # other skills (edge_diagnostics); imported here to document the dependency.

SKILL_ID = "edge_performance"

PS_SAMPLE = r"""
param([int]$Seconds = 2)
$ErrorActionPreference = 'SilentlyContinue'
$p1 = Get-Process msedge
if(-not $p1){ '[]' | Out-Host; exit 0 }
$cpu1 = @{}; foreach($p in $p1){ $cpu1[$p.Id] = $p.CPU }
Start-Sleep -Seconds $Seconds
$p2 = Get-Process msedge
$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$cmds  = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
         Select-Object ProcessId, CommandLine
$cmdMap = @{}; foreach($c in $cmds){ $cmdMap[[int]$c.ProcessId] = $c.CommandLine }
$rows = foreach($p in $p2){
  $deltaCpu = $p.CPU - ($cpu1[$p.Id])
  $cpuPct   = [math]::Round(100 * $deltaCpu / [math]::Max($Seconds * $cores, 0.001), 1)
  [pscustomobject]@{
    Pid             = $p.Id
    CpuPct          = $cpuPct
    CpuTotalSec     = [math]::Round($p.CPU, 1)
    WorkingSetMB    = [math]::Round($p.WorkingSet64 / 1MB, 1)
    PrivateBytesMB  = [math]::Round($p.PrivateMemorySize64 / 1MB, 1)
    Handles         = $p.HandleCount
    Threads         = $p.Threads.Count
    CommandLine     = $cmdMap[[int]$p.Id]
  }
}
$rows | ConvertTo-Json -Depth 3 -Compress
"""

ROLE_RE = re.compile(r"--type=([\w-]+)")
EXT_RE = re.compile(r"--extension-process", re.I)


@dataclass
class SampleResult:
    status: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _classify_role(cmdline: str | None) -> str:
    if not cmdline:
        return "browser"
    if EXT_RE.search(cmdline):
        return "extension"
    m = ROLE_RE.search(cmdline)
    return m.group(1) if m else "browser"


def _sample(seconds: int) -> SampleResult:
    if os.name != "nt":
        return SampleResult(
            status="unsupported_platform",
            error="Edge process sampling requires Windows.",
        )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SAMPLE,
             "-Seconds", str(seconds)],
            capture_output=True, text=True, timeout=seconds + 30,
        )
    except FileNotFoundError:
        return SampleResult(status="tool_unavailable", error="PowerShell was not found.")
    except subprocess.TimeoutExpired:
        return SampleResult(
            status="timeout",
            error=f"Process sampling timed out after {seconds + 30}s.",
        )
    except OSError as exc:
        return SampleResult(status="tool_unavailable", error=f"PowerShell failed to start: {exc}")
    if proc.returncode != 0:
        return SampleResult(
            status="query_failed",
            error=f"PowerShell process query exited with code {proc.returncode}.",
        )
    out = (proc.stdout or "").strip()
    if not out:
        return SampleResult(status="parse_failed", error="PowerShell returned no JSON output.")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return SampleResult(status="parse_failed", error="PowerShell returned invalid JSON.")
    rows = data if isinstance(data, list) else [data]
    if not all(isinstance(row, dict) for row in rows):
        return SampleResult(status="parse_failed", error="PowerShell JSON rows must be objects.")
    return SampleResult(status="success", rows=rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Edge performance sampler")
    ap.add_argument("context", nargs="?", default=None)
    args = ap.parse_args(argv)

    ctx = load_context([args.context] if args.context else [])
    extra = ctx.get("extra") or {}
    seconds = int(extra.get("sample_seconds", 2))
    top_n = int(extra.get("top_n", 10))

    sample = _sample(seconds)
    if sample.status != "success":
        failed = SkillResult(
            skill=SKILL_ID,
            ok=False,
            findings=[Finding(
                summary=f"Edge process sampling failed: {sample.status}.",
                severity="warning",
                evidence={"status": sample.status},
            )],
            confidence="low",
            evidence_state=EvidenceState.TOOL_UNAVAILABLE,
            raw={
                "failure": {"code": sample.status},
                "quality": {
                    "sample_status": sample.status,
                    "sample_seconds": seconds,
                    "capture_complete": False,
                    "sample_sufficient": False,
                    "records_seen": 0,
                }
            },
            error=sample.error,
        )
        failed.emit()
        return 1

    rows = sample.rows
    if not rows:
        none_running = SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(
                summary="No msedge.exe processes were present during a successful sample.",
                severity="info",
            )],
            confidence="high",
            recommendations=["Launch Edge, reproduce the perf issue, then re-run me."],
            raw={
                "quality": {
                    "sample_status": "success",
                    "sample_seconds": seconds,
                    "capture_complete": True,
                    "sample_sufficient": True,
                    "records_seen": 0,
                }
            },
        )
        seed_pt = (ctx.get("extra") or {}).get("problem_type") or "slow_startup"
        playbook.merge_into_result(none_running, [seed_pt])
        none_running.emit()
        return 0

    for r in rows:
        r["role"] = _classify_role(r.get("CommandLine"))

    by_role: dict[str, int] = {}
    total_ws = 0.0
    total_cpu_pct = 0.0
    for r in rows:
        by_role[r["role"]] = by_role.get(r["role"], 0) + 1
        total_ws += float(r.get("WorkingSetMB") or 0)
        total_cpu_pct += float(r.get("CpuPct") or 0)

    rows_sorted = sorted(rows, key=lambda r: (-float(r.get("CpuPct") or 0),
                                              -float(r.get("WorkingSetMB") or 0)))
    top = rows_sorted[:top_n]
    safe_top = [
        {key: value for key, value in row.items() if key != "CommandLine"}
        for row in top
    ]

    findings: list[Finding] = [
        Finding(
            summary=f"{len(rows)} msedge.exe processes; total ~{total_ws:.0f} MB working set, ~{total_cpu_pct:.0f}% CPU.",
            severity="warning" if (total_ws > 2048 or total_cpu_pct > 80) else "info",
            evidence={"by_role": by_role},
        )
    ]
    heavy = [r for r in top if float(r.get("WorkingSetMB") or 0) > 500]
    for h in heavy[:5]:
        findings.append(Finding(
            summary=f"PID {h['Pid']} ({h['role']}): {h['WorkingSetMB']} MB, {h['CpuPct']}% CPU",
            severity="warning",
            evidence={"handles": h.get("Handles"), "threads": h.get("Threads")},
        ))

    recommendations: list[str] = []
    if total_ws > 4096:
        recommendations.append("Edge is using >4 GB. Open edge://settings/system and toggle 'Startup boost' / 'Continue running background apps' as needed; close idle tabs or enable Sleeping tabs.")
    if total_cpu_pct > 100:
        recommendations.append("Sustained CPU > 100% (cross-core) — open Edge's Browser Task Manager (Shift+Esc) to see which renderer is hot, and check edge_extensions output.")
    if any(r["role"] == "extension" for r in top):
        recommendations.append("Disable extensions one by one (edge://extensions) and recheck, or run edge_extensions to enumerate them.")
    if not recommendations:
        recommendations.append("Footprint looks within normal range for the current workload.")

    result = SkillResult(
        skill=SKILL_ID, ok=True,
        findings=findings, confidence="medium",
        recommendations=recommendations,
        raw={
            "summary": {
                "process_count": len(rows),
                "total_working_set_mb": round(total_ws, 1),
                "total_cpu_pct": round(total_cpu_pct, 1),
                "by_role": by_role,
                "sample_seconds": seconds,
            },
            "quality": {
                "sample_status": "success",
                "sample_seconds": seconds,
                "capture_complete": True,
                "sample_sufficient": len(rows) > 0,
                "records_seen": len(rows),
            },
            "top_processes": safe_top,
        },
    )
    pts: list[str] = []
    if total_cpu_pct > 80:
        pts.append("high_cpu")
    if total_ws > 2048:
        pts.append("high_memory")
    if any(r["role"] == "extension" for r in top):
        pts.append("extension_issue")
    playbook.merge_into_result(result, pts)
    result.emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
