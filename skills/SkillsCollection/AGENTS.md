# AGENTS.md - SkillsCollection AI Manager

This repository contains IIS and Microsoft Edge diagnostic skills. For every
new IIS or Edge incident, start with the root `orchestrator` skill. Product
orchestrators are internal delegates and backward-compatible direct entries.

## Mandatory Entry Flow

1. Read `orchestrator/SKILL.md`.
2. Invoke `python orchestrator/scripts/orchestrator.py <input>`.
3. Let the manager determine IIS, Edge, or both from explicit scope, symptoms,
   problem types, and bounded evidence discovery.
4. Do not bypass the manager unless the user explicitly requests a named
   product skill or a specialized workflow such as Edge Dump.

## AI Manager Responsibilities

- Own product routing and incident scope.
- Delegate parsing and product interpretation to IIS/Edge skills.
- Preserve product evidence IDs, provenance, quality, and falsifiers.
- Run three validation rounds: product-local analysis/validation, Common
  manager reconciliation, and deterministic reference/root-quality checking.
- Never promote a candidate without the Common proof gate.
- Aggregate missing logs into an operator-reviewed collection plan.
- Produce matching English and Chinese redacted manager reports containing the
  problem description, troubleshooting process, cause/solution evidence,
  prioritized next actions and missing logs, cross-validation agreements and
  differences, per-module confidence, and the skill invocation chain with each
  skill's work summary.

## Canonical Commands

```powershell
# Auto-route an incident folder
python orchestrator/scripts/orchestrator.py C:\incident --report C:\incident\manager-report.html

# Explicit product scope
python orchestrator/scripts/orchestrator.py @context.json --product IIS
python orchestrator/scripts/orchestrator.py @context.json --product Edge

# Cross-product incident
python orchestrator/scripts/orchestrator.py @context.json --product both
```

When routing is ambiguous, the manager must stop and request `--product`; it
must not silently choose a product.

## Delegates

- IIS: `IIS/orchestrator/scripts/skill_orchestrator.py`
- Edge diagnostics: `Edge/edge_diagnostics/scripts/edge_diagnostics.py`
- Edge fan-out: `Edge/orchestrator/scripts/edge_orchestrator.py`
- Cross-validation: `Common/log_skills/skills/validator/validator.py`

Product `AGENTS.md` files define product-local behavior after delegation.