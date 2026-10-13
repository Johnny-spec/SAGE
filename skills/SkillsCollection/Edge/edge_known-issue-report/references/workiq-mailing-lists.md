# workiq Mailing-List Cross-Check Reference

Concrete know-how for using `workiq` (`mcp_workiq_ask_work_iq`) as the
**secondary** evidence source for an Edge known-issue investigation. Six
internal CSS/Edge mailing-list aliases are in scope. ADO is and remains the
**primary** source — workiq is for "has this been discussed?" and "did the PG
weigh in?".

## In-scope aliases

These six internal CSS/Edge aliases are the search scope. Per the workiq
guardrail in `AGENTS.md`, the only limit is on **what you send**: never put
customer log data or sensitive customer info into a query — use sanitized
symptom / keyword terms only.

| Alias | Email | What it's for / how to use it |
|---|---|---|
| **CSS Pod-Developer Support Browsers** | `CSSPodDevSptSB@microsoft.com` | Recent Top Issues are announced here. Search by Top Issue keyword + timeframe to learn which CSE first surfaced it and whether a CRM message was published. |
| **CSS Browser Solution Tasks** | `cssiesol@microsoft.com` | Customer-case solutions that the submitter considered valuable. Good for "has someone already solved a similar repro?". |
| **CSS Developer Browsers Technical Discussion** | `cssiedbg@microsoft.com` | Open technical discussion group. Often the first place a CSE asks "anyone seen X?". Useful for symptom phrasing variants and for early sightings before a Bug is filed. |
| **Microsoft Edge Discussions** | `edgedisc@microsoft.com` | The channel for asking **PG / senior ICs**. If a Pod-Lead or PG owner replied, this is usually where it happens. Useful for naming the ADO Area Path / feature owner. |
| **Weekly Browsers ICM digest** | `bbutler@microsoft.com` (Brian Butler digest) | Weekly digest of ICMs (incidents). Cross-check whether the symptom hit ICM tracking; an entry there usually links to a PG owner. |
| **Weekly Browsers Top Issues digest** | `bbutler@microsoft.com` (Brian Butler digest) | Weekly Top-Issue digest. Tells you whether the issue is already officially "Top" and gives the Top Issue ID directly. |

The last two are different digest series authored by the same person — when
searching, scope each query separately so the result attribution stays clean.

## When to query (and when not to)

**Query when:**

- Step 2a (ADO search) returned zero or weak hits — workiq may surface the
  CSE who first reported it, with a Bug ID you missed.
- Step 2a returned several candidate hits and you need to pick the most
  relevant one — workiq tells you which one the field is actually citing.
- The symptom mentions "PG said …" / "we got an answer from product group" /
  "in last week's digest" — these phrases map 1-to-1 to the aliases above.

**Skip when:**

- The user has already given you the exact Bug/Top Issue/PR IDs — go straight
  to ADO Step 3.
- The symptom is purely a setup question (proxy, install, GPO syntax) with no
  product-bug component — workiq adds noise.

## Suggested prompt templates

Pass these literally as the `prompt` to `mcp_workiq_ask_work_iq`. Replace
`{KEYWORDS}` with 2–3 natural-language keywords from Step 1 of the workflow
(do **not** paste long titles). Run one prompt per alias, in parallel; keep
the results separate.

### Generic — "any prior discussion"

```
Search recent messages from {ALIAS_EMAIL} that mention {KEYWORDS}.
Return up to 10 hits. For each hit give:
- date (ISO)
- subject
- the sender's display name and alias
- one or two sentences quoting the technical statement
- any Bug ID / Work Item ID / PR ID / version string the message contains
Do NOT include customer email addresses, tenant names, SR/CRM numbers, or
attachments.
```

### Targeted — "did the PG / a Pod-Lead respond"

```
Search {ALIAS_EMAIL} for threads about {KEYWORDS} in the last 90 days where
a reply came from a Microsoft Edge PG owner, Pod-Lead, or Principal Engineer.
For each thread give the subject, date, replier's display name, and the
technical conclusion they stated. Skip customer-identifying details.
```

### Digest scan — "is this in this week's Top Issues"

```
Search messages from bbutler@microsoft.com with subject containing
"Weekly Browsers Top Issues" from the last 4 weeks. List each Top Issue
entry that mentions {KEYWORDS}, including the Top Issue ID and the
one-line summary as written in the digest.
```

### ICM scan

```
Search messages from bbutler@microsoft.com with subject containing
"Weekly Browsers ICM digest" from the last 4 weeks. List each ICM
that mentions {KEYWORDS}, including the ICM ID and the triage status.
```

## How to use the results

1. **Extract IDs first.** Look for Bug IDs (6–7 digit numbers), Top Issue
   IDs, PR IDs, and version strings (`149.0.x.y`, `[CP150]`, …). These are
   the high-value cross-links back to Step 3 / 4 in ADO.
2. **Note the author and date** — these go into the report's Section 1
   "Internal discussion" sub-list as: `alias · YYYY-MM-DD · Author · one-line gist`.
3. **Verify any factual claim in ADO** before quoting it as the root cause.
   Mailing-list statements (even from PG) are not authoritative once an ADO
   PR exists — the PR description and diff are.
4. **If workiq surfaces a thread that contradicts your ADO finding**, flag
   it in the report under Section 3 (Symptom → Defect mapping) as an
   inconsistency, with both citations, and prefer the ADO record.

## Failure modes & safety

- **EULA prompt.** If `mcp_workiq_ask_work_iq` returns an EULA error, call
  `mcp_workiq_accept_eula` once with the user's confirmation, then retry.
- **No tools loaded.** If the workiq tool surface is missing, ask the user
  to start the server in **MCP: List Servers**. Do not block the rest of the
  workflow on this — proceed with ADO only and note "workiq not configured"
  in the report.
- **Customer-data leak guard.** Even though the six aliases themselves are
  internal, customer correspondence is regularly **forwarded into** them.
  Never quote out: customer email addresses, tenant names, SR numbers, CRM
  numbers, screenshots that include customer chrome (machine name, user
  name, OneDrive path, mail thread chrome). Quote only the technical
  statement.
- **Cite the alias, not the person**, if a public-facing version of the
  report is intended; for internal use (the default), the author's display
  name is fine.
