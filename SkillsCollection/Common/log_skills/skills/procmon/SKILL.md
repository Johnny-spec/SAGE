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
- `problem_type`, `question`/`symptom`, and `extra.process_names`: strongly
  recommended incident scope. Without symptom scope, Procmon patterns remain
  contextual observations and cannot become the candidate cause.

## Evidence Rules

The analyzer treats common control-flow results such as `BUFFER OVERFLOW`,
`REPARSE`, `END OF FILE`, `NO MORE FILES`, and `FAST IO DISALLOWED` as noise.
Missing-path probes require repetition or an incident/baseline delta. Output is
bounded and redacted; raw rows, command lines, users, and full paths are not
included in the report.

For authentication captures, the analyzer also aggregates UDP Send/Receive
activity to domain-controller service ports. A large send/receive imbalance is
reported as a possible response-path gap only when it meets the static threshold
and aligns with the reported symptom/process. Because Procmon is not a packet
capture, `UDP Send / SUCCESS` never proves delivery and missing Receive rows do
not independently prove packet loss or an unavailable domain controller.

After static noise thresholds, each pattern is graded against the reported
problem type, symptom terms, target process, incident window, and healthy-
baseline delta:

- `supportive`: aligned with the symptom/mechanism and eligible as a candidate.
- `contextual`: useful context but insufficient to drive a cause.
- `background`: retained for audit but suppressed from causal findings.

Symptom relevance is evaluated before the final output limit. Detailed findings
are emitted only for supportive patterns; contextual patterns remain available
as a bounded raw summary.

High event count alone never raises relevance. A failure from an unrelated
process (for example an updater during an IIS 500 incident) remains background.

Procmon is one evidence source. The skill emits a candidate only from a
symptom-aligned supportive pattern and includes explicit falsifiers. It never
promotes a Procmon-only observation to a proven root
cause. Correlate with EVTX, dumps, WPR/perf, NetLog, IIS/HTTPERR, or packet
evidence before remediation.

## CLI

```powershell
python procmon_analyzer_common.py '{"extra":{"procmon_paths":["C:\\incident\\trace.csv"]}}'
python procmon_analyzer_common.py '{"time_range":{"start":"2026-08-20T10:00:00","end":"2026-08-20T10:05:00"},"extra":{"procmon_paths":["C:\\incident\\trace.csv"]}}' --time-basis context
python procmon_analyzer_common.py --input C:\incident\trace.pml --procmon-exe C:\Tools\Procmon64.exe
```