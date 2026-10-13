# AGENTS.md — IIS Diagnostics Skills

> **Root-manager routing:** New IIS or cross-product incidents must enter at
> `..\orchestrator\scripts\orchestrator.py`. This file defines IIS-local
> behavior after delegation. The IIS orchestrator remains a compatibility
> delegate for existing automation and product-internal calls.

This repository ships a **set of composable diagnostic skills** for Windows / IIS troubleshooting.
This file is auto-loaded by GitHub Copilot CLI (`copilot`) and tells the agent how to use them.

## What you have

A skill collection rooted at this folder. Each subfolder is one skill:

| Skill id | Folder | Entry script | What it does |
|---|---|---|---|
| `iis_logs` | `IIS_logs/` | `scripts/iis_analyzer.py` | Parse IIS W3C logs, compute KPIs, classify problems, recommend follow-up skills. Supports `--around`, `--filter`, `--bucket`, `--report` (HTML+SVG). |
| `ftp_logs` | `ftp_logs/` | `scripts/ftp_analyzer.py` | Parse Microsoft FTP Service W3C logs, reconstruct sessions (connect → USER/PASS → STOR/RETR → QUIT), detect auth failures, brute force, transfer errors, incomplete sessions. Same `--around/--filter/--bucket/--report` surface as `iis_logs`. |
| `httperror` | `httperror/` | `scripts/httperr_analyzer.py` | Parse HTTP.SYS error log (`HTTPERR\httperr*.log`) |
| `event_log` | `event_log/` | `scripts/event_log_analyzer.py` | Query Windows Event Log, correlate to IIS time window |
| `app_crash` | `app_crash/` | `scripts/app_crash_analyzer.py` | Parse .NET crash events, classify exception, suggest fix |
| `security_audit` | `security_audit/` | `scripts/security_audit_analyzer.ps1` | Auth/permission diagnosis (PowerShell complete; Python framework) |
| `resource_monitor` | `resource_monitor/` | `scripts/resource_monitor.ps1` | Live CPU/memory/disk sampling (PowerShell complete; Python framework) |
| `firewall` | `firewall/` | `scripts/firewall_analyzer.ps1` | Firewall DROP analysis (PowerShell complete; Python framework) |
| `netlog` | `netlog/` | `scripts/netlog_analyzer.py` | Parse Chromium `edge://net-export` JSON files dropped alongside the IIS logs. Diagnoses TLS / DNS / proxy / generic client-side failures whose root cause is invisible to IIS, **plus full HTTP-auth analysis** — decodes SSPI `security_status` (e.g. `SEC_E_WRONG_PRINCIPAL`) and Chromium `net_error` (e.g. `ERR_MISSING_AUTH_CREDENTIALS`, `ERR_UNEXPECTED_SECURITY_LIBRARY_STATUS`), pairs `AUTH_LIBRARY_INIT_SEC_CTX` BEGIN/END frames to recover the failing Kerberos SPN, and emits `kerberos_spn_mismatch` / `auth_handshake_loop` / `tls_handshake_failure` problem_types. Auto-discovered by the orchestrator whenever a net-export JSON is present in the folder. |
| `procmon` | `procmon/` | `scripts/procmon_analyzer.py` | Stream bounded Procmon CSV evidence or export PML through an operator-trusted Procmon executable. Auto-runs only when evidence is supplied; emits candidate causes and falsifiers, never a Procmon-only proven root cause. |
| `source_logging_discovery` | Common | `source_guided_logging_discovery.py` | Bounded read-only scan of explicit source/config roots; identifies logging providers and creates a safe collection plan. |
| `validator` | Common | `validator.py` | Final evidence reducer for independent agent/model analyses; deterministic agreement/conflict handling. |
| `orchestrator` | `orchestrator/` | `scripts/skill_orchestrator.py` / `.ps1` | **Folder-first entry point.** Python owns folder discovery, cross-log dispatch, missing-log gating, and HTML reports. The native PowerShell entry handles legacy payload fan-out and selects PowerShell or Python children with `-Runtime`. |

The authoritative registry is [_shared/registry.json](_shared/registry.json).
All skills follow a uniform JSON contract defined in [_shared/contract.py](_shared/contract.py).

## Standard contract (every skill)

Input  — first positional argument is a JSON string OR a path to a JSON file:
```json
{
  "time_range": {"start": "2026-04-21T10:00:00", "end": "2026-04-21T10:30:00"},
  "problem_type": "5xx_error",
  "metrics": { ... optional ... },
  "extra": { ... skill-specific ... }
}
```

Output — a single JSON object on stdout (envelope v2.3):
```json
{
  "skill": "<id>",
  "ok": true,
  "findings": [ { "summary": "...", "severity": "critical|warning|info", "evidence": {...} } ],
  "root_cause": "<short string or null>",
  "confidence": "high|medium|low",
  "recommendations": ["[fix:...] ...", "[next:...] ...", "[logs:...] ..."],
  "solutions": [ { "problem_ref": "5xx_error", "title": "...", "severity": "critical|warning|info", "steps": ["..."], "references": ["..."] } ],
  "next_steps": [ { "action": "...", "why": "...", "skill": "<next-skill-id>" } ],
  "additional_logs_needed": [ { "log_kind": "evtx|http_err|iis_log|...", "why": "...", "how_to_collect": "...", "skill": "<consumer>" } ],
  "cross_validation": null,
  "evidence_state": "ROOT_CAUSE_FOUND|INCONCLUSIVE_EVIDENCE|...",
  "evidence_gaps": [],
  "collection_plan": null,
  "raw": { ... optional skill-specific payload ... }
}
```

`solutions`, `next_steps`, `additional_logs_needed` are seeded from [_shared/playbook.json](_shared/playbook.json) keyed on `problem_type`, via `_shared.playbook.merge_into_result(result, problem_types=[...])`. Skills may append context-specific entries. When `recommendations` is empty, the envelope auto-flattens the three structured fields into legacy strings (prefixes `[fix:...]`, `[next:...]`, `[logs:...]`) so older consumers keep working. JSON is always emitted ASCII-safe (em-dashes serialise as `\u2014`).

Errors go to stderr; exit code != 0 means the skill failed.

## Shared log readers ([_shared/logs/](_shared/logs/))

Skills do **not** parse log files inline anymore. Each log kind has one canonical reader exposing a uniform surface:

```python
from _shared.logs import iis_w3c   # or: httperr, ftp_w3c, evtx, perf_counter, firewall

iis_w3c.discover(folder, recursive=True) -> list[Path]
iis_w3c.iter_entries(path) -> Iterator[dict]
iis_w3c.summarise(entries) -> dict
iis_w3c.apply_filter(entries, filter_spec) -> Iterator[dict]
iis_w3c.around_window(entries, anchors, window_seconds) -> Iterator[dict]
iis_w3c.query(sources=..., time_range=..., filter=..., around=..., limit=..., projection=...) -> {"entries", "summary", "truncated", "sources_used"}
```

Per-kind filter dataclasses:

| Module | Filter dataclass | Notable fields |
|---|---|---|
| `iis_w3c` / `ftp_w3c` | `log_filters.FilterSpec` | `method, uri (re), status, ip (CIDR), min_time, ua (re), q` |
| `httperr` | `HttpErrFilter` | `reason` (substring), `client_ip`, `app_pool`, `contains` |
| `evtx` | `EvtxFilter` | `log_names`, `event_ids`, `providers`, `levels`, `keywords` |
| `perf_counter` | `PerfCounterFilter` | `counter_paths`, `min_value`, `max_value` |
| `firewall` | `FirewallFilter` | `action`, `protocol`, `src_ip`, `dst_port` |

Skills declare *what* they want; the shared module owns *how* to read. Skills retain their domain interpretation tables (`EVENT_ID_MAP`, `ROOT_CAUSE_HINTS`, HTTPERR reason → cause mapping, crash-signature classification).

## How to ask Copilot CLI to use these skills

Always launch `copilot` from this folder so AGENTS.md is loaded.

**Quick analysis of a single log file:**
```
analyze the IIS log C:\inetpub\logs\LogFiles\W3SVC1\u_ex260421.log using the iis_logs skill, then run any follow-up skills it recommends and produce a fused diagnosis
```

**Targeted scenario:**
```
investigate 5xx spike between 10:00 and 10:30 today: run iis_logs first, then orchestrator with its problems output
```

**Direct invocation by the agent:**
- Single skill: `python <skill>/scripts/<entry>.py '<json-context>'`
- IIS auto-trigger: `python IIS_logs/scripts/iis_analyzer.py <log_file> --auto-trigger`
- **Folder orchestration (preferred):** `python orchestrator/scripts/skill_orchestrator.py "<folder>" [--report <html>]` — pass the folder **as a positional argument**, never as JSON.
- Legacy pipe orchestration (only when you already have an `iis_logs` payload): `python IIS_logs/scripts/iis_analyzer.py <log> | python orchestrator/scripts/skill_orchestrator.py`
- Native PowerShell fan-out: `Get-Content iis_result.json -Raw | pwsh orchestrator/scripts/skill_orchestrator.ps1 -Runtime Auto`

## Rules for the agent

### Investigation and Cross-Validation

The orchestrator runs evidence-producing skills first and the Common `validator`
last for eligible high-risk problems. Providers are model-neutral: configure
trusted argv adapters with `SKILLS_VALIDATOR_PROVIDERS_JSON`, or inject already
independent structured results through `extra.model_analyses`. Incident input
cannot supply executable provider commands.

No configured providers yields `cross_validation.status=not_configured`; never
claim validation ran. Agreement requires two analyzers citing common evidence
IDs and at least two successful evidence skills. Model agreement alone cannot
raise confidence above `medium`; conflicts require falsifying evidence.

If status is `not_configured` and the host supports multiple agents/models:
1. Take the exact `raw.validation.raw.provider_request` (or direct validator
  `raw.provider_request`) without adding another model's output.
2. Run at least two isolated analysis sessions; prefer different model families.
3. Require JSON matching the embedded response schema and evidence IDs only.
4. Label each result with `model`, `agent_id`, and a distinct
  `independence_group`; shared context/reasoning must share one group.
5. Re-run the validator with results in `extra.model_analyses`. Do not manually
  declare consensus.

When evidence is inconclusive and explicit roots are available, use
`--source-root`, `--config-root`, or `--symbols-root`. The scanner never executes
or imports target code. Full rules: [Common methodology](../Common/METHODOLOGY.md).

After source discovery, read `raw.logging_discovery.official_doc_lookup`.
Validate Windows/IIS/Edge provider names and collection syntax with Microsoft
Learn (`microsoft_docs_search`, then `microsoft_docs_fetch` when needed). Validate
Chromium NetLog event semantics against official Chromium source documentation.
Send query terms and redacted facts only; never send local source snippets or
raw incident logs to documentation/model providers.

0. **Folder → root AI manager → IIS delegate**: invoke `..\orchestrator\scripts\orchestrator.py` first. After it routes the incident to IIS, this product delegate discovers and dispatches IIS-local entry skills.
  - **Offline-safe default**: folder mode only invokes skills backed by evidence in the incident bundle. Live Security/Event/Perf/firewall queries require explicit `--allow-live-host`; use it only when the folder belongs to the current host and live state is relevant.
   - **Invocation**: pass the folder as a **positional argument**: `python orchestrator/scripts/skill_orchestrator.py "<folder>" [--report <html>]`. Do **NOT** wrap it in JSON like `'{"extra":{"folder":"..."}}'`; that targets legacy pipe mode. (The orchestrator now auto-promotes that mistake into folder mode, but the positional form is the documented path.)
   - **Mandatory reporting**: the orchestrator's per-sub-skill calls happen inside one Python subprocess and are invisible to the agent UI. When you summarise the orchestrator's JSON for the user, you **must** surface these keys (use the snippet below):
     - `executed_summary` (top-level) — one-line "iis_logs OK, netlog OK, ..." proving which sub-skills ran.
     - `raw.discovery` — per-log-kind file count, proving what the orchestrator classified in the folder.
     - `raw.executed` — ordered list of sub-skill ids actually invoked.
    - `evidence_state`, `evidence_gaps`, `collection_plan`, and `cross_validation.status`.
    - then the usual `root_cause`, `findings`, `solutions`, `next_steps`, `additional_logs_needed`.
   - **Always surface stderr to the user.** The orchestrator prints a live `>>> running skill: iis_logs / netlog / event_log ...` trace on stderr. You **must** both save it to `orch.err` *and* print its full contents to the chat / terminal interface — never just redirect and drop it.
     - PowerShell pattern:

       ```powershell
       python orchestrator/scripts/skill_orchestrator.py "<folder>" --report orch.html > orch.json 2> orch.err
       Write-Host "----- orchestrator stderr (orch.err) -----"
       Get-Content orch.err
       Write-Host "----- end orchestrator stderr -----"
       ```
     - bash equivalent: `python ... "<folder>" --report orch.html > orch.json 2> orch.err; echo '----- orch.err -----'; cat orch.err`
   - **Canonical summary snippet** (reuse verbatim; works for both folder and auto-promoted JSON modes):

     ```python
     python -c "import json,sys; d=json.load(sys.stdin); \
       print('EXECUTED:', d.get('executed_summary')); \
       print('DISCOVERY:', {k: len(v) for k,v in (d.get('raw',{}).get('discovery') or {}).items()}); \
       print('SUBSKILLS:', d.get('raw',{}).get('executed')); \
       print('ROOT:', d.get('root_cause')); print('CONF:', d.get('confidence')); \
       [print(' -', f['severity'], '|', f['summary']) for f in d.get('findings', [])]; \
       [print(' *', s.get('title')) for s in d.get('solutions', [])[:8]]"
     ```
1. **Single log file → `iis_logs` / `ftp_logs`**: for HTTP traffic, start from `iis_logs`; for FTP traffic, start from `ftp_logs`. Use this path only when the user pointed at one file (not a folder) and you do not need cross-log correlation.
2. **Only call follow-up skills that the entry-point skill listed in `skills_to_trigger`** — do not invent new ones.
3. **Pass `time_range` from the analysis** to every downstream skill; never use "now − 1 hour" as a fallback when a real range is known.
4. **Treat skills as black boxes via the contract above.** Do not parse internal log files yourself if a skill exists for them.
5. **PowerShell variants** (`*.ps1`) exist for `iis_logs`, `httperror`, `event_log`, `app_crash`, `security_audit`, `resource_monitor`, `firewall`, and legacy `orchestrator` fan-out. Python remains preferred for folder mode and portable analyzers; the PowerShell orchestrator supports `-Runtime Auto|Pwsh|Python` and records the selected child runtime in `raw.runtimes`.
6. **Runtime status matters**: the Python entries for `security_audit`, `resource_monitor`, and `firewall` remain framework stubs, while their PowerShell entries are complete. Use `pwsh` for those skills until their Python ports are implemented.
7. **Admin rights**: `event_log`, `security_audit`, `resource_monitor`, `firewall` may need an elevated shell on production hosts.
8. **Folder input is the default**: both `iis_logs` and `ftp_logs` recursively scan a folder and auto-classify every `.log` by its `#Software:` header. Each skill only consumes its own log kind and lists the rest in `raw.detected_other_logs` so the agent can route them to the matching skill.
9. **FTP routing**: when `iis_logs.raw.detected_other_logs.ftp_w3c` is non-empty, also run `ftp_logs` on the same folder. When `ftp_logs.raw.detected_other_logs.iis_w3c` is non-empty, also run `iis_logs`.
10. **Time-window and search**: for "requests around T" or "find specific requests" questions, prefer `--around "YYYY-MM-DD HH:MM:SS" --window <dur>` plus `--filter "key=value,..."` over re-parsing the log manually. Filter keys: `method, uri (regex), status (e.g. 500-599), ip (CIDR), min-time, ua (regex), q (substring)` for IIS; add `user, cmd, path, min-bytes` for FTP.
11. **HTML report**: pass `--report <path>.html` when the user wants charts (status code distribution, RPS bar chart, latency percentile line, throughput+latency dual-axis, FTP session table). Reports are self-contained (inline SVG, no JS/CDN). When you have produced a consolidated diagnosis for the user, also pass `--agent-summary <path.md>` (or `<path.html>`) to embed it as a featured "Consolidated diagnosis (GitHub Copilot CLI)" section at the top of the report — the orchestrator renders a minimal Markdown subset (headings, lists, tables, fenced code, bold/italic, links).
12. **Error-driven anchor discovery**: when running the orchestrator on a folder, `--error REGEX` scans every text log (IIS, FTP, HTTPERR, plain `.log`) for the regex and converts the matching lines' timestamps into extra `--around` anchors. Use it when the user describes a symptom (e.g. `--error "NullReferenceException"` or `--error "503|Timer_AppPool"`) but does not know exactly when it happened.
13. **Offline event log**: when the user provides an exported `.evtx` file, pass it to `event_log` via `--evtx <path>` (CLI) or `extra.evtx_paths` (JSON). The orchestrator does this automatically for every `.evtx` discovered under the folder.
14. **HTTPERR folder mode**: `httperror` accepts `--folder <dir>` (or `extra.folder` / `extra.no_recursive`) and aggregates statistics across every `httperr*.log` it finds. The orchestrator uses this when at least one HTTPERR file is discovered.
15. **Net-export (Chromium) input**: drop a JSON exported from `edge://net-export` into the incident folder; `log_discovery` classifies it as the `netlog` kind and dispatches `netlog`. URLs are emitted without userinfo/query/fragment. Wall-clock filtering is applied only when `constants.timeTickOffset` exists; otherwise correlation quality is `unavailable`, not silently assumed.
16. **Missing-log reporting**: in folder mode the orchestrator computes the union of log kinds asked for by the playbook (driven by every detected `problem_type`) and by each child skill's `additional_logs_needed`, subtracts what was actually discovered or exercised, and surfaces the gap. Every missing kind becomes a `warning` finding and a row in `raw.missing_logs` and `<h2>Missing required logs</h2>` in the HTML report.
17. **Orchestrator aggregation**: when running on a folder, `orchestrator` dedups and surfaces every child skill's structured advisories, and adds `raw.cross_log_context = {available, time_range, correlatable, correlation_quality, pairs, note}`. `correlation_quality` is currently `time_only` or `unavailable`; never describe it as an exact request join.

## Project conventions

- Python 3.10+, standard library only.
- All scripts must be runnable from any cwd (use `Path(__file__).parent` for sibling lookups).
- Skill paths and capabilities live in [_shared/registry.json](_shared/registry.json) — update it when you add or move a skill.
- Run `python _shared/registry_validation.py` after registry or playbook changes; CI tests require every skill reference and entry file to resolve.
