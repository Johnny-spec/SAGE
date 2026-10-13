---
id: common_validator
name: Evidence-Grounded Cross Validator
version: 1.0
role: final_reducer
runtime:
  python: validator.py
needs_admin: false
---

Runs after evidence-producing skills. It builds a bounded redacted evidence
pack and reconciles independent structured analyses. Providers may be injected
as `extra.model_analyses` or configured by the trusted environment variable
`SKILLS_VALIDATOR_PROVIDERS_JSON`; incident input cannot supply executable
commands. No provider produces an explicit `not_configured` result.

Consensus requires at least two independent analyzers to cite common evidence
IDs for the same claim. Model agreement alone never raises confidence above
`medium`.