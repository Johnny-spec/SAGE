# Skill: `netlog`

Analyse Chromium **net-export** captures (`edge://net-export` /
`chrome://net-export`) and surface request-level failures, TLS issues, DNS
errors, and proxy fallback events from the client's perspective.

## Why it exists in the IIS collection

When troubleshooting IIS-served traffic, a net-export capture from the client
browser is often the only place where you can see the request as it was sent
and answered: TLS handshake exchange, proxy fallback, redirect chains. The
client-side view complements the server-side W3C and HTTP.SYS logs.

## Input

Standard contract envelope:

| Field | Notes |
|---|---|
| `extra.netlog_paths` | List of net-export JSON file paths. |
| `extra.folder`       | Folder to scan (recursively) for net-export `*.json`. |
| `time_range`         | Advisory (net-export absolute time correlation requires `constants.timeTickOffset`, out of scope). |
| `problem_type`       | IIS/client symptom used to grade observed NetLog mechanisms. |
| `question` / `symptom` | Recommended symptom text for separating relevant events from background noise. |

When neither is provided, the skill returns `ok=true` with a warning finding
and asks for a netlog capture via `additional_logs_needed`.

## Output

Same v2.1 envelope as every other skill. Extracts:

* total events, top source types, phase distribution
* URL request totals + failed/slow breakdown
* TLS / DNS / proxy failures grouped by source kind
* Top slow requests

## Noise and Causality Rules

The analyzer retains all observed NetLog mechanisms but labels each as
`supportive`, `contextual`, or `background` relative to the IIS/client symptom.
Only relevant mechanisms feed `raw.candidate_cause`, `raw.problems`, and the
playbook. Unrelated DNS, proxy, certificate, authentication, or latency events
remain auditable information findings and cannot drive remediation.

NetLog proves client-side behavior, not IIS root cause by itself. Every
candidate carries a falsifier and points to independent IIS/HTTPERR/EVTX/
Security/packet evidence needed to corroborate it.

## CLI

```
python netlog/scripts/netlog_analyzer.py '{"extra":{"netlog_paths":["C:\\dumps\\edge-net.json"]}}'
python netlog/scripts/netlog_analyzer.py '{"extra":{"folder":"C:\\dumps"}}'
```
