---
id: edge_known_issue
name: Edge Known Issue Deep Investigation
version: 1.0
role: follow_up
description: Prepare a deep known-issue investigation plan for Microsoft Edge using ADO, workiq, and public-web evidence lanes. Emits a structured query plan and ranking contract that the agent can execute and merge back into a final answer or bilingual HTML report.
runtime:
  python: scripts/edge_known_issue.py
needs_admin: false
triggers_for: [known_issue]
---

## When to use
Use this when the user asks whether an Edge symptom is a known issue, wants an
ADO / Top Issue / workiq investigation, or needs a deep report that links
symptoms to bugs, PRs, public references, and fix versions.

## Inputs

| Field | Required | Description |
|---|---|---|
| `question` / `summary` / `symptom` | yes | Free-text symptom or issue description. Any of these fields is accepted. |
| `problem_type` | no | Defaults to `known_issue`. |
| `problems[]` | no | Optional structured problem list; summaries are folded into the query plan. |
| `extra.use_known_issue_mcp` | no | Emit ADO / workiq / public-web suggested calls (default `true`). |
| `extra.report_mode` | no | `json` by default. Set to `html_bilingual` to request dual-language report generation downstream. |
| `mcp_results` | no | Optional second-stage hand-back payload from the agent after MCP execution. When present, the skill merges lane results into `evidence_matrix`, `match_ranking`, polarity/layer checks, and adjacent-change output. |

## Method

This skill follows a deep-investigation workflow while remaining hermetic:

1. Collect and sanitize the problem description.
2. Build short keyword groups rather than one long search phrase.
3. Emit a three-lane search plan:
   - ADO `microsoft` / project `Edge`
   - ADO `CSSDigital` / project `Browsers`
   - `workiq` + public-web corroboration
4. Emit ranking and evidence placeholders that the agent must fill after MCP execution.
5. If no credible hit exists after lane execution, fall back to a recent-commits scan before concluding there is no known issue.

## MCP hand-back contract

After the agent executes `raw.known_issue_lookup.suggested_calls`, it can call
the skill again with an `mcp_results` payload like this:

```json
{
  "mcp_results": {
    "organization_resolution": {
      "microsoft_tool_family": "mcp_azuredevops_2",
      "cssdigital_tool_family": "mcp_azuredevops_m"
    },
    "lane_results": {
      "ado_microsoft": {
        "status": "completed",
        "results": [
          {
            "id": "BUG-100",
            "title": "Edge download popup hang after 150 update",
            "summary": "Bug links popup hang to the Edge 150 download flow.",
            "url": "https://dev.azure.com/...",
            "match_strength": "exact",
            "version_polarity": "aligned",
            "surface_layer": "aligned",
            "fix_version": "150.0.200.0"
          }
        ]
      },
      "workiq": {
        "status": "completed",
        "results": [
          {
            "id": "THREAD-7",
            "title": "CSS confirms popup hang workaround",
            "match_strength": "adjacent",
            "version_polarity": "aligned",
            "surface_layer": "aligned"
          }
        ]
      }
    }
  }
}
```

The skill then:

1. Writes collected items into `raw.evidence_matrix`.
2. Produces scored candidates in `raw.match_ranking`.
3. Updates `raw.version_polarity_check` and `raw.surface_layer_check`.
4. Fills `raw.not_found_gaps` for empty or pending lanes.
5. Extracts `raw.adjacent_changes` from the `recent_commits` lane.

## Deep-investigation rules

- Resolve the ADO organization mapping at runtime; do not hardcode tool suffixes.
- Use short keyword groups and search multiple variants.
- Treat ADO as the primary source of truth.
- Treat workiq and public-web hits as corroboration unless the root cause is a platform/vendor change.
- Validate version polarity before accepting an exact match.
- Validate surface layer before accepting an exact match.
- Support one symptom mapping to multiple defects.
- Mark missing evidence explicitly as `not found`; never guess.

## Output

The skill emits the standard SkillResult envelope plus deep-investigation fields in `raw`:

- `query_plan`
- `lane_results`
- `evidence_matrix`
- `match_ranking`
- `version_polarity_check`
- `surface_layer_check`
- `not_found_gaps`
- `adjacent_changes`
- `known_issue_lookup.suggested_calls`

`known_issue_lookup.suggested_calls` is the hand-off contract for the agent.
The Python skill does not call MCP tools directly.

## Invocation

```powershell
python edge_known_issue\scripts\edge_known_issue.py '{"question":"Edge 149 works but 150 download popup hangs","problem_type":"known_issue"}'

python edge_known_issue\scripts\edge_known_issue.py '{"summary":"Is this a known Edge sign-in bug?","extra":{"report_mode":"html_bilingual"}}'
```

## Report mode

When `extra.report_mode=html_bilingual`, this skill adds a next step that asks
the agent to generate a Chinese + English HTML report after external evidence
has been fetched and ranked.