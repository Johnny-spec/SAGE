# Report Format Reference

How to assemble the two HTML reports (Chinese + English) from the investigation
evidence. Use [../assets/report-template.html](../assets/report-template.html)
as the starting skeleton.

## Deliverables

- Two self-contained HTML files written to a **`Temp/` folder at the workspace
  root** (create it if missing):
  - `edge-known-issue-report.zh.html` → `<html lang="zh-CN">`
  - `edge-known-issue-report.en.html` → `<html lang="en">`
- On name conflict, append an incrementing sequence number before the
  extension (`edge-known-issue-report.zh.1.html`, `.2.html`, …) so earlier
  reports are never overwritten.
- Add a small language-switch bar (`.langbar`) at the top of each file linking
  to the other language, using the exact filenames written (including any
  sequence number) so the cross-links resolve.
- Identical structure and evidence; only the prose language differs.
- Open both at the end.

## Visual style (plain, document-like)

Keep it calm and printable — no flashy gradients, glows, neon, or heavy
shadows. The template already encodes this palette:

| Token | Value | Use |
|---|---|---|
| `--bg` | `#ffffff` | page background |
| `--ink` | `#1f2328` | body text |
| `--muted` | `#656d76` | secondary text |
| `--line` | `#d0d7de` | borders |
| `--soft` | `#f6f8fa` | panels / code background |
| `--brand` | `#0969da` | links / accents |

Max content width ~880px. Monospace for code/paths. Status uses small text
"tags", not large colored banners.

## Section structure (in order)

1. **Links** — three parts, preceded by a one-line **ranked best match** (the
   single most-likely item with its match-strength rationale: exact vs
   adjacent, plus the version-polarity / layer check):
   1. A table of every related **ADO** item with assembled URLs and status:
      Top Issue / Solution (CSSDigital), Bug(s) (microsoft/Edge), PR(s)
      including any `[CPxxx]` cherry-picks, upstream fixes (e.g. GitHub FAST
      PR), RCA task. Add a small flow diagram of how they relate.
   2. A sub-list **"Internal discussion / 内部讨论"** listing the workiq
      hits from Step 2b, formatted as `alias · YYYY-MM-DD · Author display
      name · one-line technical gist`. Omit customer PII. If workiq returned
      no hits, omit the sub-list and add the inline note
      `内部讨论未找到相关线索 / No related internal discussion found`.
      If `workiq` was not configured for this run, state
      `workiq 未配置 / workiq not configured`.
   3. A sub-list **"Public references / 公开参考"** for Step 2c hits — vendor
      KBAs, Chromium / Chrome deprecation docs, Microsoft Learn policy / Q&A
      pages — each as `source · title · one-line relevance`, quoting the literal
      matching sentence where it names the Edge version + symptom. If none,
      note `公开参考未找到 / No public reference found`.
2. **Description & Repro** — the symptom in plain words + the numbered repro
   steps, quoting the work-item/Top-Issue text.
3. **Symptom → Defect mapping** *(include only when one scenario spans
   multiple defects)* — a table mapping each repro step to its mechanism and
   owning work item/PR, plus an evidence label per row (fact / fact+inference
   / inference). Add a short "why this differs from bug X" comparison when
   the user's experience does not fully match a single bug. If a workiq
   thread contradicts an ADO record, surface it here with both citations and
   prefer the ADO record.
4. **Root Cause** — one block per defect. State the mechanism from the PR
   description; for UI/web defects explain the framework behavior plainly.
5. **Code Explanation** — see the **depth contract** below; this section is
   the most likely place to under-deliver.
6. **Workarounds** — only those actually present in the Top Issue/Bug or in
   the workiq threads. If none exist, say "not found".
7. **Fix Progress / Version** — a timeline per defect: merged-to-main date,
   cherry-pick PRs and target release branch, Canary/Beta evidence, and an
   explicit note if a backport for the user's channel was **not found**.
   - **7b. Recent adjacent changes** *(include only when Step 5.5 ran — i.e.
     when no clean known-issue match exists)*: one card per candidate
     commit/PR with the merge date, link, one-line description, and the
     reason it is on the list. Every entry must be labeled
     `推断 / Inference · 尚未关联到已知 Top Issue / not yet linked to a known
     Top Issue`. If the scan returned nothing, replace 7b with the single
     sentence: "近 N 天 chromium.src / chromium.omaha 中未找到相邻变更 / No
     adjacent changes found in chromium.src / chromium.omaha in the last N
     days" and recommend opening a new Bug.
8. **Customer Ready Message** — quote the Top Issue's CRM field verbatim. If
   it is stale relative to the fix, note that and suggest an update (clearly
   marked as a suggestion, not as official text).

Footer: an "internal material / evidence-based, nothing fabricated"
disclaimer and the generation date + data sources (including which workiq
aliases were queried).

## Code Explanation depth contract (Section 5)

When a code fix exists, match the depth used in
`Temp/copilotinstall/copilot-install-internal.html` Section 3 ①/④ and Section
4. Concretely:

- **One card per source file.** Do **not** bundle multiple files into a
  single `<pre>` block.
- Each card has, in order:
  1. The file path as a **clickable link** to the ADO web view at the right
     branch (see [ado-investigation.md](ado-investigation.md) for the URL
     template).
  2. The line range or function name as a small `lineref` next to the path.
  3. A **10–40 line annotated code excerpt** of the key function or hunk —
     read from the diff (`get_changes`) and, when context is missing, from
     `repo_file get_content` of the current full file. Add inline `//`
     comments **on the lines that matter** to point out the bug and the
     fix. Don't paste hundreds of lines.
  4. A **one-sentence plain-language summary** under the snippet explaining
     what the fix does for a non-developer reader.
- If the fix touches multiple files or multiple functions in the same file,
  give each its own card. A control-flow walk across 3–5 cards is good.
- For *enumerations / status codes / registry value mappings*, render a
  small inline table after the snippet — the way the internal doc renders
  the `--copilot-upgrade-only=N` ↔ `CopilotUpgradeCheck` mapping.
- When the same function is changed across several PRs (main + cherry-picks),
  show the **main-branch version once**, then list the cherry-picks below
  with the target branch as a `tag`.
- Keep all identifiers / branch names / file paths verbatim in both the
  Chinese and English files; only the prose comments and the summary
  sentence get translated.

A single short snippet with one bullet of prose is *not* enough for
non-trivial logic. The reader should be able to point at the lines and say
"that's what's wrong" — the prose alone should not be carrying the argument.

## Evidence labeling

- Tag claims as **Fact** when literally present in ADO content.
- Tag as **Fact (workiq)** when literally present in a quoted mailing-list
  message; include the alias and date.
- Tag as **Fact (public)** when literally present on a fetched public page
  (vendor KBA, Chromium/Chrome doc, Microsoft Learn); include the URL.
- Tag as **Inference** when you reasoned across items; say so in the
  `源/Source` line of the section.
- Tag each candidate's **match strength** — `exact` vs `adjacent` — and state
  the version-polarity and layer check. An adjacent item with reversed version
  polarity (introduced in the version that *works*) or a different surface
  (embedded WebView2 vs browser tab) is **not** the root cause; label it so.
- When two work items have **no explicit ADO link**, state it where you
  assert they are related.
- For Section 7b entries (recent adjacent changes), tag every entry as
  **Inference** — they are by definition not yet linked to a known issue.
- Use the words "not found / 未找到" for missing fields — never guess.

## Bilingual tips

- Translate UI chrome (section headings, table headers, tags) but keep
  identifiers, branch names, enum values, file paths, and code verbatim in both
  versions.
- Keep the same section numbering and the same links/IDs in both files so they
  can be diffed and cross-referenced.
