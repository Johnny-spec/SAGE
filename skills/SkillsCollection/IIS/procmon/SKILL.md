---
name: procmon
description: "Use when: IIS, w3wp, authentication, startup, latency, file, Registry, DLL, access-denied, lock, or network symptoms need bounded Process Monitor CSV/PML analysis."
version: 1.0
role: follow_up
runtime:
  python: scripts/procmon_analyzer.py
needs_admin: false
---

# Procmon

Analyzes supplied Process Monitor evidence through the shared Common engine.
CSV is streamed directly. PML requires a trusted `Procmon.exe` supplied through
the CLI or `SKILLS_PROCMON_EXE`; incident JSON cannot select an executable.

This is an evidence-driven IIS follow-up. A Procmon file in an incident folder
is analyzed automatically, but Procmon is not requested for every IIS failure.
Its observations remain candidate causes until corroborated by IIS/HTTPERR,
Event/Security logs, WPR/perf, dumps, NetLog, or packet evidence.
Authentication analysis can surface a bounded `lsass.exe` UDP domain-controller
send/receive imbalance. Treat it as a response-path hypothesis requiring packet
and Security/Netlogon corroboration, not as direct proof of packet loss.