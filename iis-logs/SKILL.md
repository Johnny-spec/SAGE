---
name: iis-logs
description: Analyze uploaded IIS W3C access logs for errors, latency, traffic patterns, and recommended follow-up evidence. Use when a message mentions IIS, W3C logs, HTTP status failures, or IIS request performance and includes a .log attachment.
---

# IIS Logs

Analyze only the evidence uploaded with the current request. Report request metrics,
detected problem types, evidence, and recommended next steps. Do not query the live
host and do not invoke follow-up Skills automatically.

For incident-report requests, read `references/triage.md` before running the analyzer.
For a simple request-count or smoke test, skip that reference and run the analyzer directly.

The upstream implementation is vendored under `vendor/` from the trusted local
SkillsCollection. Its original instructions and source remain unchanged.
