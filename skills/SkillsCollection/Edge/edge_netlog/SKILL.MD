# Skill: `edge_netlog`

Analyse Chromium **net-export** captures (`edge://net-export`) and surface
symptom-aligned client-side mechanisms and candidate causes for page-load,
certificate, proxy, DNS, authentication, and latency failures.

## Why it exists

The `netlog` log kind has been listed as a recommended follow-up in
`playbook.json` (for `page_load_failure`, `cert_error`, `proxy_issue`) since
v1, but no skill actually consumed it. This skill closes that gap.

## Input

Standard contract envelope. The skill is interested in two things:

| Field | Notes |
|---|---|
| `extra.netlog_paths` | List of net-export JSON file paths to analyse. |
| `extra.folder`       | Folder to scan (recursively) for `*.json` net-export files. |
| `time_range`         | Optional; currently advisory. Net-export `time` is a per-session millisecond offset so absolute correlation requires `constants.timeTickOffset` (out of scope). |
| `problem_type`       | Reported scenario used to separate relevant signals from background observations. |
| `question` / `symptom` | Symptom text used for mechanism relevance; recommended for noisy captures. |

If neither is provided, the skill returns `ok=true` with a warning finding and
populates `additional_logs_needed` with `log_kind=netlog`.

## What it reports

For every file:

* total event count, top source types (`URL_REQUEST`, `HOST_RESOLVER_IMPL`,
  `SOCKET`, ...), phase distribution
* URL requests whose `params` contain `net_error` strings (`ERR_*` codes)
* DNS resolution failures
* TLS handshake failures (`ERR_CERT_*`)
* Proxy fallback events
* Slowest URL requests (top N, by begin→end span in the same source_id)

These are translated into the standard `findings` / `solutions` /
`next_steps` / `additional_logs_needed` blocks.

## Noise and Causality Rules

- Preserve every observed NetLog problem type, but grade it as `supportive`,
    `contextual`, or `background` against the reported symptom.
- Only supportive/contextual mechanisms feed candidate-cause and playbook
    output. Background certificate/DNS/proxy/slow-request events remain visible
    but are not treated as the current incident cause.
- A slow request count is not proof of the reported slowdown without matching
    origin/window and a healthy comparison.
- Each causal mechanism includes a falsifier and recommended independent
    evidence. NetLog alone does not prove server-side root cause.

## CLI

```
python edge_netlog/scripts/edge_netlog.py '{"extra":{"netlog_paths":["C:\\dumps\\edge-net.json"]}}'
python edge_netlog/scripts/edge_netlog.py '{"extra":{"folder":"C:\\dumps"}}'
```
