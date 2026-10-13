---
id: security_audit
name: Security Audit Analyzer
version: 2.0
role: follow_up
status: python_framework_pwsh_complete
description: Queries Security events 4625/4656/4663/4672, groups failing principals and source IPs, and correlates suspicious IIS clients.
runtime:
  python: scripts/security_audit_analyzer.py
  pwsh:   scripts/security_audit_analyzer.ps1
needs_admin: true
triggers_for: [auth_error]
---

## When to use
- IIS 401/403 spikes (`auth_error` problem type)

## Inputs (JSON context)

| Field | Required | Description |
|---|---|---|
| `time_range.start` / `time_range.end` | yes | ISO 8601 |
| `extra.suspicious_ips` | no | Client IPs flagged by the parent IIS analysis |

## Runtime status

The PowerShell entry is complete and queries the live Security log. The Python entry currently returns a framework marker; runtime-aware orchestration should select `pwsh`.

## PowerShell behaviour

1. `Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625,4656,4663,4672; StartTime=…; EndTime=…}`
2. Group failures by principal + target resource
3. Cross-reference IP/time with IIS 401/403 entries from the parent context
4. Report top failing principals and most-targeted resources

## Notes
- Security log access requires elevation **and** audit policy enabled (`AuditPol /get /category:*`).
- Until implemented, treat 401/403 spikes as app pool identity / NTFS ACL configuration issues.

## Report sections (envelope v2.3)

This skill emits the standard envelope plus three structured advisory fields seeded from `_shared/playbook.json` via `_shared.playbook.merge_into_result(...)`:

- `solutions[]` — concrete fixes for key problems (`problem_ref / title / severity / steps / references`).
- `next_steps[]` — suggested follow-up actions, optionally pointing at the next skill to run.
- `additional_logs_needed[]` — extra data sources required to deepen analysis.

Legacy `recommendations: string[]` is auto-derived from the three structured fields when not set (prefixes `[fix:...]` / `[next:...]` / `[logs:...]`).

## Log/source dependency

- ``_shared/logs/evtx`` (queried with ``EvtxFilter(log_names=["Security"])``)

Each shared reader exposes the same surface (`discover / iter_entries / summarise / apply_filter / around_window / query`); this skill calls `query(...)` rather than parsing files inline.
