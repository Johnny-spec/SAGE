# Common Log Skills

This folder is the canonical home for reusable log-analysis skills shared by IIS, Edge, Windows, and future products.
Shared implementations are grouped by log type, not by product.

## Initial Scope
- Registry federation bootstrap (`_shared/registry.json`)
- Product orchestrators can resolve shared skills through `external_registries`

## First Migrated Batch
- `common_iis_netlog` -> `skills/net-export/iis_netlog_analyzer_common.py`
- `common_iis_httperror` -> `skills/httperror/httperr_analyzer_common.py`
- `common_edge_netlog` -> `skills/net-export/edge_netlog_analyzer_common.py`

## Second Migrated Batch
- `common_iis_iislog` -> `skills/iislog/iis_analyzer_common.py`
- `common_iis_ftplog` -> `skills/ftplog/ftp_analyzer_common.py`
- `common_iis_event_log` -> `skills/event_log/iis_event_log_analyzer_common.py`
- `common_edge_dump_log` -> `skills/dump_log/edge_crash_analyzer_common.py`

## Process Monitor Analysis
- `common_procmon` -> `skills/procmon/procmon_analyzer_common.py`
- Streams structurally validated Procmon CSV exports with bounded bytes, rows, files, and time.
- Accepts PML only through an operator-supplied `Procmon.exe`/`Procmon64.exe`; incident JSON cannot select an executable.
- Summarizes process, file, Registry, network, image-load, lock, and duration evidence with optional incident/baseline comparison.
- Emits candidate causes and falsifiers. Procmon-only evidence never establishes a root cause.
- Can write a self-contained redacted HTML report with `--report`.

Product scripts remain as thin wrappers to preserve existing entry paths and CLI behavior.
Wrappers set `COMMON_SKILL_PRODUCT_ROOT` so common implementations can import the product-local `_shared` modules safely.

## Type-Based Layout
- `net-export/` for Chromium net-export analyzers shared across products
- `procmon/` for Sysinternals Process Monitor CSV/PML analysis
- `httperror/` for HTTP.SYS error-log analyzers
- `iislog/`, `ftplog/`, `event_log/`, `etl_log/`, `networkpackage_log/`, `Auth_script_log/`, `perf_log/`, `dump_log/` reserved for future shared analyzers in the same pattern

## Migration Strategy
1. Move reusable analyzers into this folder.
2. Keep temporary product wrappers for compatibility.
3. Retire wrappers after orchestrators fully depend on shared entries.
