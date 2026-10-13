# Azure DevOps Investigation Reference

Concrete know-how for searching Edge known issues across the two ADO
organizations and turning raw work-item/PR data into report-ready facts.

## MCP setup (`.vscode/mcp.json`)

The remote ADO MCP server (preview) uses Streamable HTTP + Microsoft Entra
OAuth. URL format: `https://mcp.dev.azure.com/{organization}`. Two servers are
used by this skill:

```jsonc
{
  "servers": {
    "ado-microsoft": {
      "url": "https://mcp.dev.azure.com/microsoft",
      "headers": { "X-MCP-Toolsets": "wit,repos", "X-MCP-Readonly": "true" }
    },
    "ado-cssdigital": {
      "url": "https://mcp.dev.azure.com/CSSDigital",
      "headers": { "X-MCP-Toolsets": "wit,repos", "X-MCP-Readonly": "true" }
    }
  }
}
```

Starting a server and Entra sign-in are **interactive**; the agent cannot do
OAuth. If tools are missing, ask the user to run **MCP: List Servers → Start**
and sign in. Useful headers: `X-MCP-Toolsets` (e.g. `wit,repos,pipelines`),
`X-MCP-Readonly`, `X-MCP-Tools`, `X-MCP-Insiders`.

## Organization → tool-name mapping (VERIFY AT RUNTIME)

Tool names are suffixed per configured server (`mcp_azuredevops_<X>_*`), not by
a parameter. **`<X>` is unstable** (e.g. `m`, `2`) and the suffix→org mapping
has been observed **reversed** between runs, so never hardcode it. Resolve it
at the start of every run:

1. Call `*_core_list_projects` on each loaded family.
2. Read the returned `url` — `https://dev.azure.com/<org>/…` tells you the org.
3. Confirm with a project probe: `Edge` exists only in **microsoft**;
   `Browsers` exists only in **CSSDigital** (wrong family → `TF200016 project
   does not exist`).

Then route by role:

| Org | Project | What lives there | Types |
|---|---|---|---|
| `microsoft` | `Edge` | product Bugs, `chromium.src` PRs/code/commits | Bug, Task, User Story |
| `CSSDigital` | `Browsers` | customer-facing known issues (AreaPath `Browsers\CSS\Operational\…`) | **Top Issue**, **Solution**, **Known Issue** |

> One run observed `_2_` = microsoft and `_m_` = CSSDigital — the **opposite**
> of an earlier note. Treat any remembered suffix as stale; verify every time.

## Core tools

- `*_search_workitem` — keyword search. Params: `searchText`, `top`,
  `includeFacets`, optional `project`/`workItemType`/`state` filters.
- `*_wit_work_item` — `action`:
  - `get` (+ `expand:"All"`) → fields **and `relations`** (linked PRs/commits/
    Top Issues/RCA). This is the key call.
  - `list_comments` → fix progress / version discussion.
  - `get_batch`, `list_revisions` as needed.
- `*_repo_pull_request` — `action`:
  - `get` (+ `includeWorkItemRefs:true`, `includeLabels:true`) → status,
    `sourceRefName`/`targetRefName`, `description` (often holds the root cause).
  - `get_changes` (+ `includeLineContent:true`) → file-level diff with code.
  - `list`, `list_by_commits` to discover PRs from a commit.

## Searching work items precisely

`*_search_workitem` is full-text and quirky. To get signal, not noise:

- **Short queries.** A long multi-word `searchText` is treated as near-AND and
  usually returns `count:0`. Use 2–4 word phrases; run several variants that
  each target one facet (symptom / mechanism / component / version / API name).
- **Scope with `project`.** Pass `project:"Browsers"` (CSSDigital) or
  `project:"Edge"` (microsoft) to drop unrelated orgs' hits (OS, OSGS, DAS
  Tools, …). Without it, generic words like "hang" / "SAP" flood the result
  with 8-digit OS bugs.
- **`includeFacets:true`** returns `System.WorkItemType` and
  `System.TeamProject` counts — use them to see the type/area distribution and
  to confirm your `project` scope took effect.
- **Don't over-filter in the query.** A restrictive `workItemType:"Solution"`
  can yield `0` even when Solutions exist; search broad, then filter the
  returned rows by `system.workitemtype` / `system.areapath` yourself. Keep
  **Top Issue / Solution / Bug / Known Issue**; drop **"KSI DOCUMENT REVIEW"**
  Tasks and other automation noise.
- **Results land in `content.json`** when large. Parse with the file tools and
  project the useful columns: `project.name`, `fields.'system.id'`,
  `fields.'system.workitemtype'`, `fields.'system.state'`,
  `fields.'system.areapath'`, `fields.'system.title'`.

## Reading known-issue work items

- Fetch **one id at a time** with `*_wit_work_item action:"get"`. `get_batch`
  fails the **entire** call if any single id is unreadable (permissions).
- `expand:"All"` returns `relations` + all custom fields. The customer-facing
  fields on Top Issue / Solution (HTML — strip tags) are:
  `Custom.SupportAreaPath`, `Custom.Workarounds`, `Custom.Investigationdetails`
  (often holds the WinDbg stack / bisect / culprit PR), `Custom.Customerreadymessage`,
  `Custom.Cases`, `Custom.FixAvailableIn`, `Microsoft.VSTS.TCM.ReproSteps`,
  `Microsoft.VSTS.Common.Resolution`.

## Known identifiers (Edge)

| Item | Value |
|---|---|
| Project `Edge` | GUID `90b2a23c-cab8-4e7c-90e7-a977f32c1f5d` |
| Repo `chromium.src` (Edge browser core) | GUID `a45cf45f-f6db-438b-baf7-da6b3de589ba` |
| Repo `chromium.omaha` (EdgeUpdate / Copilot lift-and-shift) | GUID `a9346b01-fd95-429c-a9d7-68e08c901730` |
| Downloads BTR area path | `Edge\Fundamentals\Performance\WebUI\BTR` |

These let you call `repo_pull_request` / `repo_search_commits` /
`repo_file get_content` with `repositoryId` directly when you only have a
PR number, a commit SHA, or a file path.

### Common branches

- `chromium.src`: `main`, plus per-release `official/4078m150` (Edge 150),
  `official/4022m149` (Edge 149), `official/3967m148` (Edge 148).
- `chromium.omaha`: `master`.

## Parsing `relations` (the link goldmine)

`expand:"All"` returns `relations[]`. Extract linked artifacts from the
`vstfs:///` URLs — the trailing segment is the ID:

| `rel` / `name` | URL shape | What to take |
|---|---|---|
| `ArtifactLink` "Pull Request" | `vstfs:///Git/PullRequestId/{proj}%2F{repo}%2F{PR_ID}` | `{PR_ID}` (last segment) |
| `ArtifactLink` "Fixed in Commit" | `vstfs:///Git/Commit/{proj}%2F{repo}%2F{SHA}` | `{SHA}` |
| `System.LinkTypes.Remote.Related` | `.../{org}/.../workItems/{ID}` | cross-org Top Issue `{ID}` |

The `%2F` (or `%2f`) are URL-encoded slashes. A work item can list **multiple**
PRs (e.g. the main fix and a `[CP150]` cherry-pick) — enumerate them all, then
fetch each to classify (target branch tells you which Edge release).

## Assembling human URLs from IDs

When you only have an ID, build the web link:

- Work item: `https://dev.azure.com/{org}/{project}/_workitems/edit/{id}`
- Pull request: `https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}`
- Source file at a branch:
  `https://dev.azure.com/{org}/{project}/_git/{repo}?path={/abs/path}&version=GB{branch}`
  - `main` → `version=GBmain`
  - release branch → `version=GBofficial/4078m150` (Edge 150), etc.
  - PR source branch → `version=GB{sourceRefName without refs/heads/}`

## Reading large tool results

Big MCP results are written to a `content.json` file instead of inline. Read it
with the file tools. To find meaningful diff hunks fast, grep inside that file
for the changed paths and key tokens (function names, the bug keyword), and
**skip noise** like `package.json`, lockfiles, and unchanged context blocks.
On Windows the workspace grep may not reach the temp/resources folder — use a
terminal `Select-String -Path <content.json> -Pattern '...'` to list line
numbers, then read those ranges.

## Release / branch cheat sheet

- Cherry-pick PRs are titled `[CPxxx]` and target `refs/heads/official/4078m{ver}`.
- A PR description saying "in Canary from <date>" / "promotion <ver> Beta 1"
  is evidence for the fix-version timeline.
- If you find only a 150 cherry-pick and the user is on 149, state that a 149
  backport was **not found** rather than assuming one exists.

## Reading the current full source of a file

When you need surrounding context for an annotated snippet (Section 5 of the
report), don't try to reconstruct it from the diff alone. Pull the file's
current content from the right branch:

```
<microsoft>_repo_file        # <microsoft> = microsoft-org tool family (resolve suffix at runtime)
  action: get_content
  repositoryId: a45cf45f-f6db-438b-baf7-da6b3de589ba    # chromium.src
  path: /chrome/browser/download/download_target_determiner.cc
  version: main                                          # or 'official/4078m150'
  versionType: Branch
```

The result may be a `resource://` URI when large; read it with the file
tools and grep for the function name. Use the file's own URL pattern for the
link in the report:

```
https://microsoft.visualstudio.com/Edge/_git/chromium.src?path=/chrome/browser/download/download_target_determiner.cc&version=GBmain
```

For `chromium.omaha` use
`https://dev.azure.com/microsoft/Edge/_git/chromium.omaha?path=/omaha/client/bundle_creator.cc`
(no `version` parameter defaults to the repo's default branch, which is
`master` for omaha).

## Recent-commits scan (Step 5.5)

Use this when Steps 2–5 turned up no credible match and you need to detect a
fresh behavior change before concluding "no known issue".

### Tool

`<microsoft>_repo_search_commits` with the relevant `repositoryId`
and a `fromDate` / `toDate` window (ISO 8601). Default window: 30 days; widen
to 90 days when the user says "started after a recent update" but is vague
about when.

### Per-repo search shape (chromium.src)

```
<microsoft>_repo_search_commits
  repositoryId: a45cf45f-f6db-438b-baf7-da6b3de589ba
  fromDate: 2026-03-22T00:00:00Z
  toDate:   2026-06-22T23:59:59Z
  itemPath: /chrome/browser/download         # narrow when the symptom area is known
  searchCriteria.itemVersion.version: main   # search main; cherry-picks will reference back
```

For symptom-driven keyword scoping, follow up with:

```
<microsoft>_repo_search_commits
  repositoryId: a45cf45f-f6db-438b-baf7-da6b3de589ba
  searchText: "download hub"   # natural commit-message keyword
```

### From commit → PR → description

For every candidate commit, get the owning PR (commits land via PR in Edge):

```
<microsoft>_repo_pull_request
  action: list_by_commits
  repositoryId: a45cf45f-f6db-438b-baf7-da6b3de589ba
  commitIds: [<sha>]
```

Then `action:get` on each PR ID for the description (root-cause statement)
and `action:get_changes` for the diff.

### What to keep

Keep a commit/PR in the report's "Recent adjacent changes" sub-section only
if **both** are true:

1. The PR description / commit message text overlaps the symptom area
   (e.g. mentions the same UI surface, file type, error code).
2. The merge date plausibly precedes the user's first sighting (allow 1–2
   weeks of rollout lag for Stable, ~1 week for Beta, days for Dev/Canary).

Drop everything else. A noisy negative-finding section is worse than a
honest "no adjacent change found".
