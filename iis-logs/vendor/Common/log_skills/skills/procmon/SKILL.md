---
id: common_procmon
name: procmon
description: "Use when: analyzing Sysinternals Process Monitor, Procmon, PML, or exported CSV evidence for access denied, missing files or DLLs, registry activity, lock contention, process startup or restart loops, hangs, slow I/O, and network retries."
version: 1.0
role: common_follow_up
runtime:
  python: procmon_analyzer_common.py
needs_admin: false
---

# Process Monitor Log Analysis

Analyzes bounded Process Monitor evidence without modifying the target system.
CSV exports are streamed directly. PML inputs require an operator-supplied
`Procmon.exe`, `Procmon64.exe`, or `Procmon64a.exe` through `--procmon-exe` or
the trusted `SKILLS_PROCMON_EXE` environment variable. Incident JSON cannot
select an executable, download Procmon, or accept its EULA.

## Inputs

- `extra.procmon_paths`: explicit CSV/PML evidence paths.
- `extra.procmon_folder`: recursively discover structurally valid CSV/PML files.
- `time_range`: incident window used to anchor and filter time-of-day exports.
- `extra.procmon_time_basis=context`: explicitly confirms that unzoned Procmon
  timestamps use the same clock basis as `time_range`. Without it, the skill
  analyzes the capture but marks cross-source time comparison unavailable.
- `extra.baseline_time_range`: optional healthy comparison window.
- `extra.procmon_filter`: optional process, PID, operation-family, result, path,
  or free-text filters.

## Evidence Rules

The analyzer treats common control-flow results such as `BUFFER OVERFLOW`,
`REPARSE`, `END OF FILE`, `NO MORE FILES`, and `FAST IO DISALLOWED` as noise.
Missing-path probes require repetition or an incident/baseline delta. Output is
bounded and redacted; raw rows, command lines, users, and full paths are not
included in the report.

Procmon is one evidence source. The skill emits candidate causes and explicit
falsifiers, but never promotes a Procmon-only observation to a proven root
cause. Correlate with EVTX, dumps, WPR/perf, NetLog, IIS/HTTPERR, or packet
evidence before remediation.

## CLI

```powershell
python procmon_analyzer_common.py '{"extra":{"procmon_paths":["C:\\incident\\trace.csv"]}}'
python procmon_analyzer_common.py '{"time_range":{"start":"2026-08-20T10:00:00","end":"2026-08-20T10:05:00"},"extra":{"procmon_paths":["C:\\incident\\trace.csv"]}}' --time-basis context
python procmon_analyzer_common.py --input C:\incident\trace.pml --procmon-exe C:\Tools\Procmon64.exe
```