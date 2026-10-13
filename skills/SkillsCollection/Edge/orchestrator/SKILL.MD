---
id: orchestrator
name: Edge Skill Orchestrator
version: 1.1
role: coordinator
description: Run multiple follow-up Edge skills in parallel based on a problem set (typically produced by edge_diagnostics) and fuse the results.
runtime:
  python: scripts/edge_orchestrator.py
needs_admin: false
---

> **Compatibility product delegate.** The canonical project entry is
> `..\..\orchestrator\SKILL.md`. Use this Edge-local skill directly only for
> existing automation or after the root AI manager routes an incident to Edge.

## When to use
After `edge_diagnostics` produces a problem list, or whenever the agent has
a payload of the shape:

```json
{
  "problems": [{"type": "crash", "severity": "critical"}],
  "skills_to_trigger": ["edge_crash", "edge_policy"],
  "time_range": {"start": "...", "end": "..."}
}
```

The orchestrator can also be handed an entire `edge_diagnostics` SkillResult;
it will look inside `raw.skills_to_trigger`.

## Behaviour

1. Resolve each entry in `skills_to_trigger` via `_shared/registry.json`.
2. Build a per-skill subcontext containing `time_range`, the most-relevant
   `problem_type`, and the verbatim `extra` block from the caller.
3. Run evidence-producing skills with a thread pool (max 3 concurrent), each capped at 60s,
   total budget 240s.
4. If explicit source/config roots are supplied and evidence is inconclusive,
  run bounded read-only `source_logging_discovery`.
5. Run `validator` last for eligible high-risk problems. Providers receive a
  redacted evidence pack and cannot see each other's output.
6. Fuse results, evidence state/gaps, collection plan and validation conflicts.

## Output

`raw.children` — full per-skill envelopes, keyed by skill id.
`raw.problem_assignments` — which problem type each skill was given.
`raw.validation` — final validator envelope, separate from parallel children.
`root_cause` — concatenation of high-confidence child root causes.

## Invocation

```powershell
# Called by edge_diagnostics --auto-trigger
python orchestrator\scripts\edge_orchestrator.py '<edge_diagnostics-raw-json>'

# Manual
python orchestrator\scripts\edge_orchestrator.py '{"skills_to_trigger":["edge_crash","edge_policy"],"problems":[{"type":"crash"}]}'
```

## Report sections (envelope v2.3)

This skill emits the standard envelope plus three structured advisory fields seeded from `_shared/playbook.json` via `_shared.playbook.merge_into_result(...)`:

- `solutions[]` — concrete fixes for key problems (`problem_ref / title / severity / steps / references`).
- `next_steps[]` — suggested follow-up actions, optionally pointing at the next skill to run.
- `additional_logs_needed[]` — extra data sources required to deepen analysis.
- `evidence_state / evidence_gaps / collection_plan` — investigation completion
  state and operator-reviewed acquisition plan.
- `cross_validation` — provider-neutral independent-analysis result.

Legacy `recommendations: string[]` is auto-derived from the three structured fields when not set (prefixes `[fix:...]` / `[next:...]` / `[logs:...]`).

## Log/source dependency

- Aggregates every child Edge skill's advisory fields with dedup keys ``(problem_ref,title)`` / ``(action,skill)`` / ``(log_kind,skill)``. Adds ``raw.cross_source_context = {sources_used, shared_problem_types, skills_run, had_input_problems}`` and ``raw.operator_summary`` (Markdown string with the three advisory sections inlined).

Each shared reader exposes the same surface (`discover / iter_entries / summarise / apply_filter / around_window / query`); this skill calls `query(...)` rather than parsing files inline.
