---
name: edge-known-issue-report
description: 'Investigate a Microsoft Edge problem against Azure DevOps (microsoft + CSSDigital orgs), internal CSS/Edge mailing-list discussion (via workiq), and public vendor/Chromium known issues; produce bilingual (Chinese + English) HTML reports. USE WHEN the user describes an Edge symptom, bug, crash, hang, or policy/sign-in issue, or asks "is this a known bug", "search ADO / Top Issue", or wants a report linking Top Issue / Bug / PR. Runs three lanes in parallel (ADO in both orgs, six CSS/Edge internal aliases via workiq, public web including Chromium tracker and vendor KBAs). When a code fix exists, explains the root cause at source-code level with clickable URLs and annotated snippets. When nothing matches, scans recent commits in the relevant Edge repos to detect a fresh behavior change. DO NOT USE FOR general coding questions, non-Edge products, or when no Azure DevOps MCP server is configured.'
---

# Edge Known-Issue Report

Turn a free-form Microsoft Edge problem description into an evidence-based,
source-code-level known-issue report, delivered as two self-contained HTML
files (Chinese + English). Evidence is gathered from Azure DevOps (primary,
authoritative) and corroborated in parallel against internal CSS/Edge
mailing-list discussion and the public web (Chromium / vendor known issues,
platform deprecations); when nothing matches, recent commits in Edge repos are
scanned to detect a fresh behavior change.

## Prerequisites

1. **Azure DevOps remote MCP servers must be configured and signed in.** Two
   orgs are reachable, defined in `.vscode/mcp.json` as `ado-microsoft`
   (`https://mcp.dev.azure.com/microsoft`) and `ado-cssdigital`
   (`https://mcp.dev.azure.com/CSSDigital`). Their tools are auto-suffixed
   `mcp_azuredevops_<X>_*` where `<X>` is an **unstable** token (`m`, `2`, …)
   whose org mapping must NOT be assumed — see the Iron Rule "Resolve the
   org→suffix mapping at runtime". Load the tools with tool-search first; if a
   server is unauthenticated, ask the user to start it in **MCP: List Servers**
   and sign in (the agent cannot do interactive OAuth). Toolset / `.vscode/mcp.json`
   shape: [references/ado-investigation.md](references/ado-investigation.md).

2. **`workiq` MCP** (`mcp_workiq_ask_work_iq`) for the mailing-list cross-check
   (Step 2b). If unavailable, run without it and note "workiq not configured".

3. **`fetch_webpage`** for the public-web cross-check (Step 2c) — always
   available. See [references/public-web-search.md](references/public-web-search.md).

4. The `reSearch` MCP (Edge source code search), if present, may confirm code
   paths. Optional — ADO `get_changes` / `repo_file get_content` is usually
   enough.

## Iron Rules (read before every run)

These encode hard-won lessons. Violating them produces misleading reports.

- **Resolve the org→suffix mapping at runtime — never hardcode `_m_`/`_2_`.**
  The auto-generated suffix can point either way and HAS been observed
  reversed. Before searching, call `*_core_list_projects` on each loaded
  `mcp_azuredevops_<X>_*` family and read the returned `url`
  (`dev.azure.com/<org>/…`) to learn which suffix is **microsoft** vs
  **CSSDigital**. Then route by **role**, not by letter:
  - **microsoft** org → project `Edge` → product **Bugs**, `chromium.src`
    PRs/commits/code.
  - **CSSDigital** org → project `Browsers` → customer-facing **Top Issues**,
    **Solutions**, **Known Issues** (AreaPath `Browsers\CSS\Operational\…`).
  `Browsers` exists ONLY in CSSDigital; using it on the microsoft family
  returns `TF200016 project does not exist` — a fast way to confirm a wrong
  suffix.
- **Extract keywords from the user's own words; never reverse-search.** Pull
  natural terms from the symptom (e.g. "rdp download stuck keep delete",
  "please wait popup hang"); do not paste a known exact title back into
  search. State which keywords you used.
- **Search short, search in groups, then filter.** ADO `search_workitem`
  treats a long multi-word phrase as near-AND and routinely returns **0**. Use
  2–4 word phrases, several variants covering different facets (symptom /
  mechanism / component / version / API name), add `project=` +
  `includeFacets=true`, and filter results post-hoc by type/AreaPath. Full
  precision recipe: [references/ado-investigation.md](references/ado-investigation.md).
- **Three search lanes, run in parallel; ADO is authoritative.** (a) ADO both
  orgs, (b) workiq mailing lists, (c) public web. workiq and public hits are
  **leads and corroboration**, not root-cause truth — tie the final report back
  to ADO work items/PRs/diffs, OR, when the cause is a platform/vendor change,
  to an authoritative public doc (Chromium tracker, Chrome/Edge deprecation
  doc, vendor KBA). A vendor KBA that literally names the Edge version +
  symptom is strong corroboration; cite it as such.
- **Rank matches: exact vs adjacent — and beware false twins.** For every
  candidate, classify match strength and record WHY it is or isn't this case.
  Two traps seen in practice: **version polarity** (an issue *introduced* in
  149 is NOT a match for a 149-*success* / 148-*fail* case — check the
  direction) and **layer** ("SAP in embedded WebView2 desktop" ≠ "SAP in the
  Edge browser tab" — same vendor, different surface).
- **workiq safety — keep customer data out of the query.** Per the `AGENTS.md`
  guardrail, never put customer log data or sensitive customer info (raw logs,
  PII, emails, tenant names, SR/CRM numbers) into a workiq prompt — query with
  sanitized symptom / keyword terms only. Do not widen to other aliases or
  customer-mail folders, and do not copy customer PII into the report.
- **Public-web safety — never put customer data in a web query.** Search only
  generic, sanitized terms (symptom, API, version). No customer domain, case
  ID, tenant, or email ever leaves in a `fetch_webpage` query.
- **Evidence only; mark gaps as "not found / 未找到".** Every claim must trace
  to fetched ADO content, a quoted mailing-list message, or a fetched public
  page. Absent fields (workaround, fix version, CRM) are stated as missing.
  Never invent. Separate fact from inference and label inferences; note when
  two items have no explicit link.
- **One symptom may map to multiple defects.** Different repro steps may be
  different work items/PRs — reconcile them; don't stop at the first hit.
- **If you find nothing, say so — then look for a recent change.** Before
  concluding "no known issue", run the recent-commits scan (Step 5.5) and
  present anything behaviorally adjacent, clearly labeled inference.
- **Output both languages**, identical structure and evidence. When the cause
  is a code defect, explain it for a non-developer (file, function, line(s),
  what was wrong, what the fix does) with clickable source URLs and annotated
  snippets — depth contract in
  [references/report-format.md](references/report-format.md).

## Workflow

Track progress with a todo list. Step 2 has three lanes that run in parallel;
Steps 3–6 are the investigation core.

### 1. Parse the problem

Extract: affected channel/version (e.g. 148.x vs 149.x — note **both** when the
user contrasts a working vs failing build, since the delta is the strongest
lead); each distinct user-visible symptom on its own line; the surfaces/APIs
involved (`msedge.exe`, downloads hub, IE mode, popups/`window.opener`,
`document.domain`, service workers, extensions, …); and 2–3 natural keyword
variants. Multiple symptoms may map to separate defects.

### 2. Search — three lanes in parallel

First **resolve the org→suffix mapping** (Iron Rule) once, then run all three:

**2a. ADO, both orgs.** Run `*_search_workitem` on the microsoft family
(project `Edge`) and the CSSDigital family (project `Browsers`) with the
keyword variants. Use **short** phrases, `includeFacets=true`, and the
`project` filter to cut OS/OSGS/DAS-Tools / "KSI DOCUMENT REVIEW" noise; keep
Top Issue / Solution / Bug / Known Issue and sort by relevance. Precision
recipe: [references/ado-investigation.md](references/ado-investigation.md).

**2b. workiq mailing lists.** Query `mcp_workiq_ask_work_iq` across the six
CSS/Edge aliases with the same keyword groups — **one focused question per
group, not one giant blob**. Alias table + prompt templates:
[references/workiq-mailing-lists.md](references/workiq-mailing-lists.md). Treat
hits as pointers (PR/work-item IDs, fix version, PG owner, workaround) and as
customer-impact signal; quote author + technical statement only, no PII. If a
CSE used terminology you missed, re-run 2a/2c with it.

**2c. Public web.** Use `fetch_webpage` for vendor/Chromium known issues and
platform deprecations that explain Edge behavior changes (e.g. a Chromium
deprecation, or a vendor KBA naming the Edge version + symptom). Tooling
gotchas (Bing/JS-SPA pages don't extract; use DuckDuckGo HTML; vendor KBA
preview pages expose Symptom+Keywords without login) and the good-source list:
[references/public-web-search.md](references/public-web-search.md).

Report the keywords used in each lane.

### 3. Read authoritative ADO records

For each promising hit from Step 2:

- `*_wit_work_item` `action:"get"`, `expand:"All"` — returns fields **and the
  `relations`** array (the source of truth for linked PRs, commits, RCA tasks,
  cross-org Top Issues). PR/commit IDs are encoded at the end of the
  `vstfs:///Git/...` URLs (parsing in
  [references/ado-investigation.md](references/ado-investigation.md)).
- Fetch hits **one id at a time** with `action:"get"`. `get_batch` fails the
  **whole** call if any single id is unreadable (permissions), so prefer
  per-id.
- For Top Issues / Solutions, read the custom fields **verbatim** (they are
  HTML — strip tags): `Custom.Workarounds`, `Custom.Investigationdetails`,
  `Custom.Customerreadymessage`, `Custom.Cases`, `Custom.FixAvailableIn`,
  `Custom.SupportAreaPath`, `Microsoft.VSTS.TCM.ReproSteps`,
  `Microsoft.VSTS.Common.Resolution`.
- `action:"list_comments"` for fix progress / version discussion.

### 4. Read the pull requests and code

For each linked PR (use the **microsoft**-org repo tools):

- `*_repo_pull_request` `action:"get"` (with `includeWorkItemRefs:true`) for
  status, target branch, description (the description usually contains the
  root-cause statement).
- `action:"get_changes"` with `includeLineContent:true` for the actual diff.
  Identify the changed files, the key function(s), and the minimal fix.
  Large results are written to a `content.json` file — read it with the file
  tools and grep within it for the meaningful hunks (skip lockfiles,
  `package.json`, large generated files).
- When a changed file is small and central to the fix, pull the **current
  full content** of that file at `version=GBmain` (or the relevant branch)
  via `*_repo_file action:"get_content"`. This lets you place the diff into
  the surrounding context — required when you need to do a function-by-function
  walk in Section 5 of the report.

### 5. Reconcile, map symptoms → defects, and rank relevance

Build a step-by-step map from each repro step to the mechanism and owning work
item / PR / public doc. Apply the **exact-vs-adjacent** ranking (Iron Rule):
record match strength and the version-polarity / layer check for each
candidate. Explicitly flag where ADO has no link between items and where your
mapping is inference. If workiq or the public web named a different (or
additional) cause, verify it against ADO — or, for a platform/vendor change,
an authoritative public doc — before adopting it.

### 5.5. Recent-commits scan — only when Steps 2–5 found nothing

If none of the three lanes (ADO / workiq / public web) surfaced a credible
match, do **not** conclude "no known issue" yet. Scan recent changes with
`*_repo_search_commits` (microsoft org) on the relevant repo and a date window
matching symptom onset (30–90 days; widen when the user is vague), filter by
commit-message keywords from Step 1, then `*_repo_pull_request
action:"list_by_commits"` → `get` to read each candidate's description. Repo
ids, search shape, and the keep/drop criteria:
[references/ado-investigation.md](references/ado-investigation.md). Present
survivors in report **Section 7b**, labeled `推断 / Inference`.

If even this yields nothing, state plainly: ADO had no matching Bug/Top Issue,
workiq no matching discussion, the public web no matching KBA/doc, and the last
N days of commits show nothing adjacent — then recommend opening a new Bug.

### 6. Generate the two HTML reports

Use [assets/report-template.html](assets/report-template.html) +
[references/report-format.md](references/report-format.md). Write both files to
a **`Temp/` folder at the workspace root**:
`edge-known-issue-report.zh.html` (`lang="zh-CN"`) and
`edge-known-issue-report.en.html` (`lang="en"`); on name conflict append an
incrementing sequence number so prior reports are never overwritten, and make
the language-switch bar link the exact filenames written. Both files cover the
same sections, same order, same evidence; plain light style.

The full section list and all depth contracts live in
[references/report-format.md](references/report-format.md) — follow it rather
than restating here. Two things that session experience proved easy to miss:
Section 1 (Links) must list **all three** source lanes (ADO items, internal
discussion, public KBAs/docs), and the report must state the **ranked best
match** with its match-strength rationale (exact vs adjacent, version-polarity
/ layer).

### 7. Open and summarize

Open both reports (`Start-Process`). In chat, give a concise evidence-based
summary with the key links (Top Issue, Bug(s), PR(s), notable workiq threads,
public KBAs/docs), the fix version, the **ranked best match**, and any
"not found" gaps. If Step 5.5 ran, state the negative finding and list the
adjacent commits surfaced.

## Output contract

- Two self-contained HTML files (zh + en) in the workspace `Temp/` folder,
  named as above (sequence-numbered on conflict), plain style, identical
  structure/evidence. The section list, visual palette, and the Section 5
  code-explanation depth contract are defined once in
  [references/report-format.md](references/report-format.md).
- A chat summary with assembled ADO URLs, notable workiq thread refs, public
  KBA/doc links, the ranked best match, and explicit "not found" notes.
