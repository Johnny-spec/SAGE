---
name: edge-procmon
description: "Use when: supplied Sysinternals Procmon CSV/PML evidence may explain Edge startup, hang, crash, renderer loss, file, Registry, DLL, access-denied, lock, slow-I/O, or network-retry behavior."
version: 1.0
role: follow_up
runtime:
  python: scripts/procmon_analyzer.py
needs_admin: false
---

# Edge Procmon

Analyzes supplied Process Monitor evidence through the shared bounded Common
engine. CSV is streamed directly. PML requires an operator-trusted Procmon
executable supplied through CLI or `SKILLS_PROCMON_EXE`; incident JSON cannot
select executable code.

This is an evidence-driven escalation skill. It auto-runs only when
`extra.procmon_paths` or `extra.procmon_folder` is present. Procmon patterns are
candidate evidence and require corroboration from Crashpad/dumps, Application
EVTX, Edge process/policy evidence, WPR/perf, NetLog, or packet captures.
Authentication analysis can surface a bounded `lsass.exe` UDP domain-controller
send/receive imbalance. Treat it as a response-path hypothesis requiring packet
and Security/Netlogon corroboration, not as direct proof of packet loss.