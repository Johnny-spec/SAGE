---
name: orchestrator
description: "Use when: investigating any IIS or Microsoft Edge/Chrome browser incident, analyzing an incident folder or supplied logs, coordinating product skills, performing multi-round cross-validation, or generating an evidence-grounded manager report."
version: 1.0
role: ai_manager|coordinator|entry_point
runtime:
  python: scripts/orchestrator.py
needs_admin: false
---

# SkillsCollection AI Manager

This is the canonical first entry point for IIS and Microsoft Edge incidents.
Act as the AI manager: identify the affected product from explicit scope,
symptoms, and supplied evidence; delegate evidence analysis to the product
skills; reconcile their results; enforce the shared evidence-quality gate; and
produce one bounded, redacted manager report.

## Manager Workflow

1. Establish incident scope and route to IIS, Edge, or both. Chrome/Chromium
   NetLog evidence is analyzed through the Edge/IIS Chromium analyzers; Chrome
   is not a separate product route. Never guess when evidence is ambiguous;
   request `--product` instead.
2. Inventory supplied evidence and delegate through product-local
   orchestrators. Product skills retain ownership of log parsing and domain
   interpretation.
3. Run product-local validation, manager-level evidence reconciliation, and a
   final deterministic consistency/root-quality round.
4. Preserve producer evidence IDs, provenance, falsifiers, missing-log gaps,
   and collection plans. A candidate is not a root cause.
5. Generate the manager report with analysis process, evidence chain, possible
   causes and evidence, solutions and evidence, missing logs, cross-validation,
   and the skill invocation chain.

## Evidence Reasoning Policy

Act as a senior support engineer using dialectical reasoning:

- Begin from the reported symptom, problem type, affected process/origin, and
   incident window. A log anomaly that does not match this scope is not causal
   evidence.
- Classify noisy observations as `supportive`, `contextual`, or `background`.
   Retain background observations for audit, but exclude them from candidate
   causes and remediation.
- Event volume is not causality. Repetition only matters after operation
   semantics, healthy baseline, target process/origin, and time comparability
   have been established.
- Temporal co-occurrence is weaker than an exact request/PID/source join.
- For every proposed cause, show supporting evidence, falsifiers/counter-
   evidence, alternative explanations, evidence gaps, and confidence.
- Procmon and NetLog are single perspectives. They may establish a mechanism or
   candidate, but do not independently prove a cross-product root cause.
- Evidence IDs in the report must be accompanied by concrete meaning: finding,
   source, time, record locator, key facts, symptom relevance, and noise risk.

## Report Contract

`--report <path>` always creates two self-contained, redacted HTML files:

- `<path>`: English report (`en-US`).
- `<stem>.zh-CN.html`: Chinese report.

Both reports follow the same reading order:

1. Executive summary and confidence by module.
2. Problem description and incident scope.
3. Troubleshooting process with ordered steps and confidence.
4. Senior engineering assessment that weighs support, counter-evidence,
   alternatives, noise/interference, uncertainty, and the next decision.
5. Cause analysis with concrete evidence details, IDs, falsifiers, and confidence.
6. Recommended solutions with evidence, verification, and rollback criteria.
7. Next actions and required logs, ordered by priority.
8. Cross-validation rounds, agreements, differences, partial agreements, and
   confidence.
9. Skill invocation chain, including what each skill did and its result state.
10. Bounded evidence appendix with concrete source/record/time/fact details.

The Chinese report localizes the report structure and manager explanations.
Technical evidence, commands, product identifiers, and analyzer conclusions are
preserved verbatim when no authoritative translation is available, preventing
translation from changing diagnostic meaning.

## Inputs

- Folder: `python orchestrator/scripts/orchestrator.py C:\incident`
- Context JSON or `@file`: product, symptoms/problem types, time range, and
  `extra.*` evidence paths.
- `--product auto|IIS|Edge|both` controls routing. Explicit scope wins.
- `--report <path>` writes both redacted self-contained HTML manager reports.

The product-local orchestrators remain compatibility delegates. New IIS/Edge
investigations should enter here first.