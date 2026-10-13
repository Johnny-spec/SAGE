# Cross-Product Investigation Methodology

This document defines the investigation control plane shared by IIS and Edge
skills. Product analyzers produce evidence; this layer governs uncertainty,
collection, source-guided discovery, independent analysis, and escalation.
Repository release acceptance criteria are defined in
[Diagnostic Skill Quality Gates](QUALITY_GATES.md).

## Investigation Loop

1. **Define the case**: symptom, impact, host/product boundary, time range,
   expected behavior, reproduction, and whether analysis is live or offline.
2. **Inventory evidence**: record provided sources, unknown files, parse quality,
   permissions, time basis, truncation, and missing sources.
3. **Normalize facts**: preserve source/record locators and original values while
   converting time and domain fields into a common representation.
4. **Run evidence skills**: execute independent source-specific skills in
   parallel. Do not run the validator yet.
5. **Assess evidence state**: distinguish a proven root cause from an
   inconclusive run, missing evidence, incomparable clocks, permissions, or
   unavailable tooling.
6. **Plan the smallest collection**: create a read-only, operator-reviewed
   `collection_plan` with scope, admin requirements, sensitivity, outputs,
   validation rules, and stop conditions.
7. **Use source-guided discovery when needed**: scan explicit local source/config
   roots to identify existing logging providers and the minimum capture needed.
8. **Cross-validate last**: independent agents/models analyze the same redacted
   evidence pack without seeing each other's output. Deterministic code computes
   agreement and conflicts.
9. **Remediate and verify**: changes require impact review, rollback, a success
   metric, and a bounded observation window.

## Evidence States

`ok` means the skill process completed; `evidence_state` says whether the
investigation completed.

| State | Meaning |
|---|---|
| `ROOT_CAUSE_FOUND` | Direct or independently corroborated evidence supports a root cause. |
| `INCONCLUSIVE_EVIDENCE` | Analysis ran, but the available facts do not prove a root cause. |
| `MISSING_REQUIRED_EVIDENCE` | Required logs or data sources are absent. |
| `TIME_NOT_COMPARABLE` | Sources cannot be placed on a reliable shared timeline. |
| `PERMISSION_BLOCKED` | Access controls prevented required evidence acquisition. |
| `TOOL_UNAVAILABLE` | The runtime or collection utility is unavailable. |
| `UNKNOWN_LOG_TYPE` | Supplied evidence could not be classified safely. |
| `LIVE_COLLECTION_REQUIRED` | Historical evidence is insufficient; a controlled live capture is needed. |
| `COLLECTION_PLAN_READY` | A bounded operator-reviewed collection plan was generated. |

Do not translate `INCONCLUSIVE_EVIDENCE` into “no issue found.” Do not use low
confidence as a substitute for one of these states.

## Multi-Agent and Multi-Model Validation

The validator is a **final reducer**, not another evidence-producing skill.

### Roles

- **Evidence agents**: IIS W3C, HTTPERR, EVTX, Crashpad, policy, NetLog, source
  discovery. Their outputs contain source locators and quality metadata.
- **Hypothesis agents/models**: independently interpret a bounded redacted
  evidence pack and return structured claims.
- **Deterministic reducer**: validates evidence IDs, groups claim keys, computes
  agreement/partial agreement/conflicts, and caps confidence.
- **Operator**: resolves conflicts, approves collection and remediation.

### Independence Rules

- Providers receive identical evidence packs and never receive another
  provider's output.
- Every claim cites one or more evidence IDs such as `E0001`.
- Agreement requires at least two completed providers citing common evidence.
- By default, at least two independent evidence-producing skills must succeed.
- Model agreement alone never raises confidence above `medium`.
- Conflicts lower confidence and produce falsifying-evidence requests.
- No configured provider yields `status=not_configured`; the system must never
  claim that validation ran.

### Provider Interfaces

Trusted host configuration may set `SKILLS_VALIDATOR_PROVIDERS_JSON` to a JSON
array. Commands are argv arrays, run with `shell=false`, receive the provider
request on stdin, and must return JSON on stdout.

```json
[
  {
    "name": "model-a",
    "command": ["provider-a-adapter", "--json"],
    "timeout_seconds": 45
  },
  {
    "name": "model-b",
    "command": ["provider-b-adapter", "--json"],
    "timeout_seconds": 45
  }
]
```

Incident input cannot provide executable provider commands. An agent-hosted
workflow can instead inject already-independent results in
`extra.model_analyses`:

```json
{
  "model_a": {
    "confidence": 0.8,
    "claims": [
      {
        "claim_key": "iis-config-access",
        "conclusion": "IIS configuration access is denied",
        "evidence_refs": ["E0001", "E0004"],
        "falsifiers": ["The same request succeeds under the same identity"],
        "recommended_next_evidence": ["evtx"]
      }
    ]
  }
}
```

## Source-Guided Logging Discovery

Use `source_logging_discovery` when:

- evidence is inconclusive and no existing log identifies the failing layer;
- the operator does not know which provider, Event ID, ETW category, or NetLog
  event to capture;
- logs exist but do not contain a required join key;
- custom application logging must be located.

The scanner requires explicit `repo_roots`, `config_roots`, or `symbols_roots`.
It only reads allowlisted text extensions within file/total-byte budgets. It
never executes, imports, builds, or uploads target code; never follows symlinks;
and emits only short redacted snippets plus hashes and relative paths.

Search targets include:

- .NET `EventSource`, `EventListener`, `ILogger`, Event Log and logging config;
- native ETW `TraceLoggingWrite`, provider declarations and EventWrite calls;
- IIS authentication, modules, handlers, HTTP logging and FREB config;
- Chromium/Edge NetLog event names and source-dependency clues;
- TLS/certificate bindings and Schannel references.

The output contains provider candidates, official-document search suggestions,
legacy log requests, and a structured `collection_plan`. Official documentation
queries are suggestions only; local source is never sent automatically.

## Source and Documentation Escalation Order

1. Use the registered product skill and existing evidence catalog.
2. Inspect product configuration and local source with bounded read-only scan.
3. Identify provider/event names, IDs, source dependencies, and configuration
   gates from source facts.
4. Verify collection syntax against authoritative documentation:
   Microsoft Learn for IIS/Windows/Edge policy; Chromium source docs for NetLog.
5. Generate the smallest capture covering the incident time and component.
6. Validate output files and rerun evidence skills.
7. Escalate to a larger ETW/WPR/dump capture only if the cheaper evidence cannot
   falsify competing hypotheses.

## Collection Plan Safety

A collection plan never executes automatically. Every request records:

- required vs optional evidence and consuming skill;
- live/offline mode and incident time range;
- administrator requirement and sensitive data categories;
- read-only commands/manual steps and expected output;
- validation rules and alternatives;
- stop conditions that prevent unbounded data collection.

Raw NetLog, dumps, Security EVTX, URLs, user names, paths, and source snippets may
be sensitive. The validator receives bounded redacted facts, never raw logs or
full source.

## Stop and Escalation Criteria

Stop collection when a direct join proves the failing component, the approved
minimal plan is exhausted, or collection risk exceeds scope. Escalate when:

- independent evidence conflicts;
- clocks remain incomparable;
- required evidence needs unavailable privilege or tooling;
- source/schema versions are unsupported;
- the next capture would include credentials, private content, or production
  memory and lacks explicit approval.

A useful final report states what is proven, what remains a hypothesis, what
would falsify it, what evidence is missing, and exactly how success will be
verified after remediation.
