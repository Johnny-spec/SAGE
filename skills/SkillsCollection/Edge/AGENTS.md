# AGENTS.md — Microsoft Edge Diagnostics Skills

> **Root-manager routing:** New Edge or cross-product incidents must enter at
> `..\orchestrator\scripts\orchestrator.py`. This file defines Edge-local
> behavior after delegation. The Edge orchestrator remains a compatibility
> delegate for existing automation and product-internal calls.

This folder ships a **set of composable skills** for answering questions about
Microsoft Edge (Chromium) and troubleshooting Edge issues on Windows.

This file is auto-loaded by GitHub Copilot CLI (`copilot`) when you launch it
from this folder, and it tells the agent how to use the skills below.

## What you have

A skill collection rooted at this folder. Each subfolder is one skill:

| Skill id | Folder | Entry script | What it does |
|---|---|---|---|
| `edge_diagnostics` | `edge_diagnostics/` | `scripts/edge_diagnostics.py` | Entry point. Detects Edge install, channel, version, profile path, managed status, recent crashes; classifies problem types; recommends follow-ups. |
| `edge_crash` | `edge_crash/` | `scripts/edge_crash_analyzer.py` | Enumerate Crashpad reports, classify crash signature, suggest mitigations. |
| `edge-dump-report` | `edge-dump-report/` | Agent workflow (`WinDbg/*` MCP) | Symbolized multi-dump analysis with MEX, offline Application/System EVTX correlation, third-party DLL evidence grading, and Markdown report generation. |
| `edge_performance` | `edge_performance/` | `scripts/edge_performance.py` | Sample live `msedge.exe` processes (CPU / memory / handle count) and flag heavy renderers. |
| `edge_network` | `edge_network/` | `scripts/edge_network.py` | Diagnose proxy / DNS / certificate / connectivity issues affecting Edge. |
| `edge_policy` | `edge_policy/` | `scripts/edge_policy.py` | Inspect `HKLM/HKCU\Software\Policies\Microsoft\Edge`, list applied managed policies. Emits `raw.mslearn_lookup` MCP suggestions so the agent can fetch official policy definitions and valid values via `microsoft_docs_search` / `microsoft_docs_fetch`. |
| `edge_extensions` | `edge_extensions/` | `scripts/edge_extensions.py` | Enumerate installed extensions per profile; flag risky/disabled/forced ones. |
| `edge_netlog` | `edge_netlog/` | `scripts/edge_netlog.py` | Parse Chromium `edge://net-export` JSON files. Classifies certificate / DNS / proxy / generic page-load failures and slow URL requests. Feeds `page_load_failure`, `cert_error`, `dns_issue`, `proxy_issue`, `page_slow`. |
| `edge_procmon` | `edge_procmon/` | `scripts/procmon_analyzer.py` | Stream bounded Procmon CSV evidence or export PML through an operator-trusted Procmon executable. Auto-runs only when evidence is supplied; emits candidate causes and falsifiers, never a Procmon-only proven root cause. |
| `edge_qa` | `edge_qa/` | `scripts/edge_qa.py` | Knowledge-base Q&A for common Edge questions (flags, keyboard shortcuts, IE mode, sync, profiles, etc.). Emits `raw.mslearn_lookup` MCP suggestions that the agent should run via `microsoft_docs_search` / `microsoft_docs_fetch`. |
| `edge_known_issue` | `edge_known_issue/` | `scripts/edge_known_issue.py` | Deep known-issue investigation planner for Edge symptoms. Emits a three-lane ADO/workiq/public query plan, evidence placeholders, ranking contract, and optional bilingual-report next step. |
| `source_logging_discovery` | Common | `source_guided_logging_discovery.py` | Bounded read-only scan of explicit source/config roots; finds logging providers and builds collection plans. |
| `validator` | Common | `validator.py` | Provider-neutral final reducer for independent agent/model analyses; deterministic evidence-ID reconciliation. |
| `orchestrator` | `orchestrator/` | `scripts/edge_orchestrator.py` | Run multiple skills based on a problem set, fuse results. |

The authoritative registry is [_shared/registry.json](_shared/registry.json).
All skills follow a uniform JSON contract defined in [_shared/contract.py](_shared/contract.py).

## Standard contract (every skill)

Input — first positional argument is a JSON string OR `@path/to/file.json`:
```json
{
  "time_range": {"start": "2026-05-26T10:00:00", "end": "2026-05-26T10:30:00"},
  "problem_type": "crash",
  "question": "How do I enable IE mode for a site?",
  "extra": { "profile": "Default" }
}
```

Output — a single JSON object on stdout (envelope v2.3):
```json
{
  "skill": "<id>",
  "ok": true,
  "findings": [{ "summary": "...", "severity": "critical|warning|info", "evidence": {} }],
  "root_cause": "<short string or null>",
  "confidence": "high|medium|low",
  "recommendations": ["[fix:...] ...", "[next:...] ...", "[logs:...] ..."],
  "solutions": [ { "problem_ref": "crash", "title": "...", "severity": "critical|warning|info", "steps": ["..."], "references": ["..."] } ],
  "next_steps": [ { "action": "...", "why": "...", "skill": "<next-skill-id>" } ],
  "additional_logs_needed": [ { "log_kind": "crashpad|netlog|registry|user_data|processes", "why": "...", "how_to_collect": "...", "skill": "<consumer>" } ],
  "cross_validation": null,
  "evidence_state": "ROOT_CAUSE_FOUND|INCONCLUSIVE_EVIDENCE|...",
  "evidence_gaps": [],
  "collection_plan": null,
  "raw": { ... skill-specific payload ... }
}
```

`solutions`, `next_steps`, `additional_logs_needed` are seeded from [_shared/playbook.json](_shared/playbook.json) keyed on `problem_type`, via `_shared.playbook.merge_into_result(result, problem_types=[...])`. Skills may append context-specific entries. When `recommendations` is empty, the envelope auto-flattens the three structured fields into `[fix:...]` / `[next:...]` / `[logs:...]` strings for legacy consumers. JSON is always emitted ASCII-safe.

Errors go to stderr; exit code != 0 means the skill failed.

## Shared source readers ([_shared/sources/](_shared/sources/))

Skills do **not** parse Edge data sources inline anymore. Each kind has one canonical reader exposing a uniform `discover / iter_entries / summarise / apply_filter / around_window / query` surface:

| Module | Filter dataclass | What it reads |
|---|---|---|
| `crashpad` | `CrashpadFilter(signature, process_type, min_size_bytes)` | `User Data/Crashpad/reports/*.dmp` metadata |
| `edge_registry` | `PolicyFilter(category, name_contains, subkey_contains, hive)` | `HKLM/HKCU\Software\Policies\Microsoft\Edge` (Windows-only) |
| `user_data` | `EdgeProfileFilter(profile_name, extension_id, enabled_only)` | Profile `Preferences`, `Secure Preferences`, `Extensions/` |
| `processes` | `ProcessFilter(min_cpu_seconds, min_working_set_mb, name_contains)` | live `msedge.exe` snapshot (CPU / WS / handle count / type) |
| `netlog` (Chromium net-export) | `NetlogFilter(source_type, phase, contains)` | Edge `--log-net-log` / `edge://net-export` JSON; the `edge_netlog` skill is the primary consumer. |

Each `query()` returns `{"entries": [...], "summary": {...}, "truncated": bool, "sources_used": [...]}`.

## Orchestrator aggregation

[orchestrator/scripts/edge_orchestrator.py](orchestrator/scripts/edge_orchestrator.py) dedups every child skill's `solutions` / `next_steps` / `additional_logs_needed` onto its own envelope, and writes two extra raw fields:

- `raw.cross_source_context = { sources_used, shared_problem_types, skills_run, had_input_problems }` — which `_shared/sources/*` were exercised across the run.
- `raw.missing_logs = [{ log_kind, why, how_to_collect, skill }, …]` — log kinds the playbook or a child skill flagged as required but the user did not provide. Each missing kind is also emitted as a `warning` `Finding` and rendered in the `## Missing required logs` section of `raw.operator_summary`. Pass `extra.netlog_paths` (or any future `extra.*_paths`) to close the gap for that kind.
- `raw.operator_summary` — a Markdown string (`# Edge orchestrator summary` + top findings + the three advisory sections + missing-log section if any) that is terminal-friendly even without an HTML report.

## How to ask Copilot CLI to use these skills

Always launch `copilot` from this folder so `AGENTS.md` is loaded.

**General troubleshooting (let the entry skill decide):**
```
my Edge keeps crashing on startup — run edge_diagnostics, then orchestrate the follow-ups it recommends
```

**Targeted scenario:**
```
investigate the Edge crash spike between 10:00 and 10:30 today: run edge_crash with that window, then summarize the top signatures
```

**Symbolized dump investigation:**
```
use edge-dump-report to analyze every open Edge WinDbg session, correlate the .evtx files beside the dumps, identify third-party evidence, and generate a report
```

**Ask a question (Q&A KB):**
```
how do I configure IE mode site list via Group Policy? use edge_qa
```

**Known issue investigation:**
```
is this a known Edge bug in 150 but not 149? use edge_known_issue
```

**Direct invocation by the agent:**
- Single skill: `python <skill>/scripts/<entry>.py '<json-context>'`
- Auto-orchestrate: `python edge_diagnostics/scripts/edge_diagnostics.py --auto-trigger`
- Full orchestration: `python orchestrator/scripts/edge_orchestrator.py '<diagnostics-result-json>'`
- Dump workflow: select the project-level `Edge Dump` custom agent from
  `.github/agents/edge-dump.agent.md`; it reads `edge-dump-report/SKILL.md`
  and calls the host's `WinDbg/*` MCP tools directly.

## Rules for the agent

### Cross-Validation and Source-Guided Discovery

The orchestrator runs normal Edge evidence skills first, optional source-guided
discovery second, and `validator` last. Validator providers are not hard-coded to
specific model brands. Configure trusted argv adapters through
`SKILLS_VALIDATOR_PROVIDERS_JSON`, or inject independent structured outputs in
`extra.model_analyses`. Providers receive the same redacted evidence pack and do
not see each other's output.

Consensus requires common evidence IDs and multiple evidence-producing skills.
Model agreement alone is capped at `medium`; conflicts lower confidence and
request falsifying evidence. No provider produces explicit `not_configured`.
Incident input cannot provide executable commands.

When status is `not_configured` and the host supports multiple agents/models,
send the exact validator `raw.provider_request` to at least two isolated
sessions. They must not see each other's outputs. Prefer different model
families, require the supplied JSON schema/evidence IDs, label `model`,
`agent_id`, and distinct `independence_group`, then re-run validator with those
objects in `extra.model_analyses`. Never synthesize “two agents” in one shared
reasoning context.

If no root cause is established, pass explicit `extra.repo_roots`,
`extra.config_roots`, or `extra.symbols_roots`. Source discovery is bounded,
read-only, non-executing, and emits official-document query suggestions plus a
safe collection plan. See [Common methodology](../Common/METHODOLOGY.md).

Verify source-derived Windows/Edge provider candidates with Microsoft Learn
(`microsoft_docs_search`, then `microsoft_docs_fetch` when needed), and NetLog
events against official Chromium source documentation. Use only suggested query
terms and redacted facts; do not upload local source snippets or raw captures.

0. **Explicit dump / WinDbg input → `edge-dump-report` directly.** When the
  user provides `.dmp` files, open WinDbg sessions, MEX output, or asks for a
  symbolized dump report, use the agent workflow instead of `edge_diagnostics`
  or the Crashpad string scanner. Analyze every relevant session, correlate
  adjacent offline EVTX logs, and generate the report. Never add
  `edge-dump-report` to Python `skills_to_trigger`; a Python child cannot call
  the host's DbgX MCP tools.
1. **After the root AI manager routes to Edge, start from `edge_diagnostics`** for other troubleshooting requests, unless
   the user has already named the problem type or is asking a knowledge question.
2. For knowledge questions ("how do I…", "what is…", "where is…"), go straight
   to `edge_qa`.
3. For known-issue investigations ("is this a known bug", "search ADO / Top Issue",
  "investigate workiq"), go straight to `edge_known_issue`.
4. **Only call follow-up skills that `edge_diagnostics` listed in
   `skills_to_trigger`** — do not invent new ones.
5. **Pass `time_range` from the diagnostics result** to every downstream skill
   when a window is known; do not fall back to "now − 1h" silently.
6. **Treat skills as black boxes via the contract above.** Don't re-scrape
   Edge's profile folder yourself if a skill already exposes that data.
7. **Some skills are read-only by design** — none of these scripts modify Edge
   settings or terminate processes. If a fix requires changes, describe the
   change and ask the user to apply it.
8. **No admin rights are required** for the default actions. Some
   investigations (e.g., reading another user's profile, querying enterprise
   policy in HKLM) may need elevation; the affected skill will report
   `needs_elevation: true` in its `raw` block when it hits this limit.
9. **After running `edge_qa` or `edge_policy`, augment its answer with the
   Microsoft Learn MCP.** Both skills publish a `raw.mslearn_lookup` block
   with a `suggested_calls[]` array — each item has `{tool, query, why}`.
   For every item:
   - If `tool == "microsoft_docs_search"` → call the `microsoft_docs_search`
     MCP tool with the given `query`.
   - If `tool == "microsoft_docs_fetch"` → call `microsoft_docs_fetch` with
     the given URL.
   Merge the live results into the final reply and cite the returned URLs.
   - For **`edge_qa`**: when `raw.matches` is empty and `confidence == "low"`,
     the MCP results become the **primary** answer.
   - For **`edge_policy`**: use the master reference fetch plus per-policy
     searches to enrich the registry walk with official definitions, valid
     values, and deprecation notes. Honour the `truncated: true` flag — if
     set, the agent may issue follow-up searches for additional policy names
     the user asks about.
   If `extra.use_mslearn: false` is set or `raw.mslearn_lookup` is absent,
   skip the MCP step (offline mode).
10. **After running `edge_known_issue`, execute its deep-investigation lane plan.**
    The skill publishes `raw.known_issue_lookup.suggested_calls` plus
    `raw.query_plan`, `raw.match_ranking`, `raw.version_polarity_check`, and
    `raw.surface_layer_check`.
    - Resolve the Azure DevOps organization mapping at runtime; do not hardcode tool suffixes.
    - Run the ADO `microsoft` and `CSSDigital` lanes in parallel, then the workiq and public-web corroboration lanes.
    - Validate version polarity and surface layer before treating a candidate as an exact match.
    - If all lanes miss, run the recent-commits lane before concluding no known issue exists.
    - If `extra.report_mode == "html_bilingual"`, use the fetched evidence to generate the bilingual report flow described by the skill.

## Project conventions

- Python 3.10+, standard library only.
- All scripts must be runnable from any cwd (use `Path(__file__).parent` for
  sibling lookups).
- Skill paths and capabilities live in [_shared/registry.json](_shared/registry.json)
  — update it when you add or move a skill.
- This collection targets **Windows** first (where Edge is most commonly
  deployed and managed). Scripts degrade gracefully on other OSes by emitting
  an `ok: true` envelope with an empty payload and a note in `findings`.
