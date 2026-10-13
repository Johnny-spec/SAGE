# Diagnostic Skill Quality Gates

This document defines release acceptance criteria shared by Common, IIS, and
Edge diagnostic skills. `METHODOLOGY.md` defines how investigations should be
performed; this document defines what the repository must enforce before a
skill is considered production-ready.

## Status Terms

- `enforced`: deterministic code and tests reject violations.
- `partial`: a control exists, but does not yet cover every producer or path.
- `planned`: the requirement is normative, but migration is incomplete.

## Release Gates

| Gate | Requirement | Status |
|---|---|---|
| Contract | Every orchestrated child is normalized and validated before aggregation. | enforced |
| Compatibility | Missing `schema_version` is interpreted as legacy v2.3; explicit v2.4 must contain the complete stable envelope. | enforced |
| Identity | A child result's `skill` must match the invoked skill id. | enforced |
| Failure isolation | Malformed child output becomes `ok=false`, low-confidence, inconclusive evidence and cannot contribute a root cause. | enforced |
| Failure privacy | Invalid stdout is represented by length and SHA-256, not copied into the result. | enforced |
| Schema parity | Common, IIS, and Edge result schemas must remain structurally identical. | enforced |
| Failure semantics | `no_events`, missing input, unavailable tooling, parse failure, timeout, and truncation remain distinct on migrated acquisition paths. | partial |
| Provenance | Python and PowerShell findings receive stable evidence ids and derived source/record provenance; locator completeness remains producer-dependent. | partial |
| Quality metadata | Migrated IIS Event Log/app-crash, Edge process/crash/NetLog, and IIS W3C paths report explicit quality; remaining readers are pending. | partial |
| Root-cause promotion | `ROOT_CAUSE_FOUND` requires explicit quality proof; orchestrators ignore unproven root text. | enforced |
| Privacy | Python/PowerShell wire output and IIS HTML use shared default redaction; remaining ad-hoc stderr/report paths require audit. | partial |
| Bounded execution | Readers enforce byte, record, sample, deadline, and concurrency budgets and still emit an envelope on exhaustion. | partial |
| Determinism | Evidence identity, contract normalization, and validator reduction are stable under fixture reordering. | enforced |
| Remediation safety | Changes are operator-approved and include impact, rollback, success metrics, and an observation window. | partial |

## Wire Contract Policy

The canonical schema is `Common/schemas/skill_result.schema.json`. Product
copies are mirrors for local discovery and offline use.

- An unversioned result is treated as legacy v2.3.
- Legacy fields are preserved; missing stable fields receive conservative
  defaults during normalization.
- Explicit v2.4 results must contain the complete stable envelope.
- Unknown versions, invalid enums, invalid nested types, identity mismatch, and
  non-object output are contract failures.
- Contract failures never retain malformed stdout text.
- Contract validation metadata is written to `raw.contract_validation`.
- Operational failures use complete v2.4 envelopes and never retain context-bearing
   command arguments.
- Broad evidence states remain compatibility classes; `raw.failure.codes[]`
   preserves exact timeout/query/parse/permission/schema acquisition statuses.
- `ROOT_CAUSE_FOUND` requires a non-empty cause, medium/high confidence,
   direct join or two independent sources, sufficient sample, complete capture,
   comparable time, and no blocking gaps.

The runtime validator is dependency-free and lives in
`Common/result_contract.py`. JSON Schema remains the declarative contract for
tooling and CI; orchestrators do not require a third-party schema package.

Every finding receives an `EV-*` evidence id plus derived provenance. Provider
claims must cite a valid id and include a falsifier; rejected claims remain in
the validator audit output but cannot participate in consensus.

## Production Definition Of Done

A skill is production-ready only when all applicable requirements are met:

1. `SKILL.md`, registry metadata, runtime status, entrypoint, and actual
   capabilities agree.
2. Normal, empty, malformed, permission-blocked, unavailable-tool, truncated,
   and timeout paths return valid envelopes.
3. Every causal finding has traceable evidence and states what would falsify
   it.
4. Blocking evidence gaps prevent root-cause promotion.
5. Default JSON, Markdown, HTML, and provider packs contain no test secrets or
   unredacted sensitive fixture values.
6. Fixed thresholds report sample size, denominator, observation duration, and
   whether a historical baseline was available.
7. Readers remain within declared resource budgets on adversarial fixtures.
8. Focused tests, product regressions, registry closure, schema parity, and
   PowerShell syntax checks pass.

## Current Validation Commands

Run from each indicated directory:

```powershell
# Root static gate
python Common\quality_gate.py

# Common
python -m pytest tests -q

# IIS
python -m pytest _shared\tests -q

# Edge
python -m pytest _shared\tests -q
```

The root CI workflow runs these suites, schema/registry closure, and AST parsing
for every version-controlled PowerShell source.

## Next Enforcement Slice

The next shared implementation should migrate quality metadata to FTP,
HTTPERR, policy, extensions, and network producers; replace remaining
whole-file IIS/FTP/HTTPERR aggregations with bounded streaming; add historical
baselines; and implement request/PID/app-pool exact joins. Registry risk and
capability metadata also remains a staged migration rather than inferred data.