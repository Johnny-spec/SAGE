---
name: netlog
description: Analyze Chromium Edge or Chrome net-export JSON for authentication, TLS, DNS, proxy, request failures, and latency. Use only for valid browser NetLog captures.
---

# Chromium NetLog

Use this Skill for JSON captured from `edge://net-export` or `chrome://net-export`.

Analyze browser-side authentication, TLS, DNS, proxy, request failures, and latency. Preserve
the distinction between observed evidence and recommendations. Do not use the IIS W3C Skill for
this format.

For authentication incidents, prioritize `ERR_MISSING_AUTH_CREDENTIALS`, Negotiate/NTLM
challenge behavior, SSPI status, attempted SPN, and repeated 401 restart loops.