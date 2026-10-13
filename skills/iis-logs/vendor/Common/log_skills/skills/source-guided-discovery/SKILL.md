---
id: common_source_logging_discovery
name: Source-Guided Logging Discovery
version: 1.0
role: knowledge_base|follow_up
runtime:
  python: source_guided_logging_discovery.py
needs_admin: false
---

Read-only scanner used when existing evidence is inconclusive or the operator
does not know which logs/providers to collect. It scans only explicit roots,
allowlisted text extensions, and bounded bytes. It never executes or imports
target code, never follows symlinks, and redacts secrets before emitting short
source facts.

The output contains `raw.logging_discovery`, structured
`additional_logs_needed`, and a review-only `collection_plan`.