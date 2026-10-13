# SkillsCollection Routing Instructions

For every new IIS or Microsoft Edge troubleshooting request, use the root
`orchestrator` skill first. Read `orchestrator/SKILL.md` and invoke
`orchestrator/scripts/orchestrator.py` with the incident folder or context.

Do not invoke a product-local orchestrator as the first step unless the user
explicitly requests that compatibility entry. The root manager owns product
routing, multi-round cross-validation, evidence/root-quality gates, missing-log
planning, and the final manager report. Product skills own evidence parsing and
domain interpretation after the manager delegates to them.