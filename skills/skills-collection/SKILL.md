---
name: skills-collection
description: Coordinate the imported IIS and Microsoft Edge diagnostic SkillsCollection for cross-product incidents, evidence reconciliation, and manager-level recommendations.
---

# SkillsCollection Manager

Use this Skill for broad IIS or Microsoft Edge incidents that require product routing,
multiple diagnostic delegates, evidence-quality checks, or a consolidated manager result.

The imported collection is available under `/SkillsCollection/`. This wrapper invokes its root
orchestrator with live-host diagnostics disabled and model-based cross-validation skipped. It may
analyze one uploaded evidence file or bundle, or route from the supplied symptom text alone.

Prefer a specialized SAGE Skill (`iis-logs` or `netlog`) for a single known evidence type. Use
this manager when the product is unclear, evidence spans products, or coordinated next actions are
needed.