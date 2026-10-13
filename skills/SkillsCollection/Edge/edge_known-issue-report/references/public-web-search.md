# Public-Web Search Reference

The third search lane (Step 2c). Use it to find vendor/Chromium known issues
and platform deprecations that explain an Edge behavior change — these often
give the *strongest* match when the root cause is a Chromium platform change
rather than an Edge-specific defect (e.g. a `document.domain` deprecation that
breaks a vendor app's popup↔parent handoff). Always corroborate, never leak
customer data.

## Safety

Search **only** generic, sanitized terms: symptom words, API names, Edge/Chrome
version numbers, vendor product names. **Never** put a customer domain, case /
SR / CRM number, tenant name, or email into a `fetch_webpage` query or URL.

## `fetch_webpage` gotchas (what extracts, what doesn't)

- **Search-engine result pages mostly DON'T extract.** A `bing.com/search?q=…`
  fetch returns only nav chrome, no result snippets.
- **Use DuckDuckGo's server-rendered HTML endpoint** for result snippets:
  `https://html.duckduckgo.com/html/?q=<url-encoded query>`. It returns plain
  result titles + snippet text + real target URLs (`uddg=` param), which is
  enough to pick which pages to fetch next.
- **JS single-page apps DON'T extract**: `issues.chromium.org/issues/<id>`,
  Gerrit (`chromium-review.googlesource.com/c/.../+/<n>`), and most ADO web
  views return "Failed to extract meaningful content". Get the *facts you need*
  from a page that server-renders, or from the ADO MCP (for Chromium commits,
  the microsoft-org `chromium.src` mirror).
- **These render fine** (server-side HTML): `learn.microsoft.com`,
  `developer.chrome.com`, `web.dev`, most vendor KBA *preview* pages, MDN,
  community forum threads.
- Fetch a small batch of specific URLs per call with a focused `query`; prefer
  authoritative pages over aggregators.

## Vendor KBA preview pages (no login needed)

Many vendor KBAs expose **Symptom + Environment + Keywords without login** on a
"preview" page; only the full resolution needs a login. That preview is often
enough to confirm a match. Example shape (SAP):
`https://userapps.support.sap.com/sap/support/knowledge/en/<number>` — the
preview lists the Symptom and a Keywords block that frequently names the exact
browser version and UI surface (e.g. "Edge version 148 … Pop up … Please wait …
WebClient UI"). Quote those verbatim; mark the full article as login-required.

## Good public sources by symptom type

| Symptom / cause type | Where to look |
|---|---|
| Chromium platform change / web API removal | `developer.chrome.com/blog/<feature>-deprecation`, `web.dev`, MDN, the HTML spec |
| Origin / cross-window / `document.domain` / iframe breakage | Chrome `document.domain` deprecation doc; `learn.microsoft.com/deployedge/edge-learnmore-origin-keyed-agent-cluster` |
| Edge enterprise policy that mitigates it | `learn.microsoft.com/deployedge/microsoft-edge-policies` (+ the per-policy page for the reg key / GP path) |
| Vendor app broken by a browser version | that vendor's KBA / notes / community (SAP, Citrix, Oracle, …) — search the vendor name + symptom + "Edge"/"Chrome" |
| "Has anyone else seen this on Edge N" | `learn.microsoft.com/answers`, vendor community forums |

## Turning a public hit into report evidence

- Quote the **literal** matching sentence (symptom line, keyword block, spec
  wording) and cite the URL.
- Record the **mechanism** (e.g. "`document.domain` setter is a no-op under an
  origin-keyed agent cluster from Chrome/Edge 115+") and the **fixes** the
  source gives — typically a server-side header (`Origin-Agent-Cluster: ?0`),
  an enterprise policy (`OriginAgentClusterDefaultEnabled=false`), or an app
  refactor (`postMessage()`).
- Apply the same **exact-vs-adjacent** ranking as ADO hits: does the public doc
  name the *same* version direction and the *same* surface/layer as the user's
  case? If a vendor KBA literally names the failing Edge version and symptom,
  it is strong corroboration and belongs in Section 1 of the report.
