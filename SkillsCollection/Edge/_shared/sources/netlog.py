"""Bounded Chromium net-export reader with explicit capture quality."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "NetlogFilter", "NetlogReadResult", "read_file",
    "discover", "iter_entries",
    "summarise", "apply_filter", "around_window", "query",
]

DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_EVENTS = 250_000


@dataclass
class NetlogFilter:
    source_type: str | None = None   # e.g. "URL_REQUEST", "HOST_RESOLVER_IMPL"
    phase: int | None = None         # 0=begin, 1=end, 2=none
    contains: str | None = None      # substring on JSON-encoded params

    def is_empty(self) -> bool:
        return not any((self.source_type, self.phase is not None, self.contains))

    def matches(self, e: dict[str, Any]) -> bool:
        if self.source_type and self.source_type != e.get("source_type"):
            return False
        if self.phase is not None and int(e.get("phase", -1)) != int(self.phase):
            return False
        if self.contains:
            blob = json.dumps(e.get("params") or {}, ensure_ascii=False).lower()
            if self.contains.lower() not in blob:
                return False
        return True


@dataclass
class NetlogReadResult:
    status: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    time_model: dict[str, Any] = field(default_factory=dict)
    dependency_graph: dict[str, Any] = field(default_factory=dict)


def discover(folder: Path | str | None = None,
             recursive: bool = True) -> list[Path]:
    """Find .json files that look like edge-net-export under `folder`."""
    if folder is None:
        return []
    base = Path(folder)
    if base.is_file():
        return [base] if base.suffix.lower() == ".json" else []
    pattern = "**/*.json" if recursive else "*.json"
    return sorted(base.glob(pattern))


def _mapped_name(value: Any, inverse: dict[int, str]) -> str | None:
    if value is None:
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value)
    return inverse.get(numeric, str(value))


def _time_model(constants: dict[str, Any]) -> dict[str, Any]:
    raw_offset = constants.get("timeTickOffset")
    try:
        offset = float(raw_offset)
    except (TypeError, ValueError):
        offset = None
    return {
        "time_tick_offset_ms": offset,
        "wall_clock_available": offset is not None,
        "quality": "approximate" if offset is not None else "unavailable",
        "formula": "unix_ms = event_time_ms + timeTickOffset_ms" if offset is not None else None,
    }


def _source_dependencies(
    params: Any, source_types: dict[int, str]
) -> list[tuple[Any, str | None]]:
    if not isinstance(params, dict):
        return []
    raw_values: list[Any] = []
    for key in ("source_dependency", "source_dependencies"):
        value = params.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value is not None:
            raw_values.append(value)
    dependencies: list[tuple[Any, str | None]] = []
    for value in raw_values:
        if not isinstance(value, dict):
            continue
        dependencies.append((
            value.get("id"),
            _mapped_name(value.get("type"), source_types),
        ))
    return dependencies


def read_file(
    path: Path,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> NetlogReadResult:
    """Read one capture within hard byte/event budgets."""
    p = Path(path)
    try:
        size_bytes = p.stat().st_size
    except OSError as exc:
        return NetlogReadResult(
            status="missing_input",
            error=f"NetLog input is unavailable: {exc}",
            quality={"capture_complete": False, "bytes_read": 0},
        )
    if size_bytes > max_input_bytes:
        return NetlogReadResult(
            status="input_too_large",
            error=f"NetLog input exceeds the {max_input_bytes}-byte budget.",
            quality={
                "capture_complete": False,
                "size_bytes": size_bytes,
                "bytes_read": 0,
                "max_input_bytes": max_input_bytes,
                "truncated": True,
            },
        )
    try:
        raw = p.read_bytes()
        blob = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except OSError as exc:
        return NetlogReadResult(
            status="read_failed",
            error=f"NetLog read failed: {exc}",
            quality={"capture_complete": False, "bytes_read": 0},
        )
    except json.JSONDecodeError as exc:
        return NetlogReadResult(
            status="parse_failed",
            error=f"NetLog JSON is invalid: {exc.msg}",
            quality={
                "capture_complete": False,
                "size_bytes": size_bytes,
                "bytes_read": len(raw),
            },
        )
    events = blob.get("events") if isinstance(blob, dict) else None
    if not isinstance(events, list):
        return NetlogReadResult(
            status="unsupported_schema",
            error="NetLog JSON must contain an events array.",
            quality={
                "capture_complete": False,
                "size_bytes": size_bytes,
                "bytes_read": len(raw),
            },
        )
    constants = blob.get("constants") or {}
    source_types_map = (constants.get("logSourceType") or {})
    event_types_map = (constants.get("logEventTypes") or {})
    inv_source = {
        int(value): str(name)
        for name, value in source_types_map.items()
        if isinstance(value, int)
    }
    inv_event = {
        int(value): str(name)
        for name, value in event_types_map.items()
        if isinstance(value, int)
    }
    model = _time_model(constants if isinstance(constants, dict) else {})
    truncated = len(events) > max_events
    selected = events[:max_events]
    entries: list[dict[str, Any]] = []
    nodes: set[tuple[Any, str | None]] = set()
    edges: list[dict[str, Any]] = []
    phase_map = (constants.get("logEventPhase") or {}) if isinstance(constants, dict) else {}
    begin_phase = phase_map.get("PHASE_BEGIN", 1)
    end_phase = phase_map.get("PHASE_END", 2)
    phase_balance: Counter[tuple[Any, Any]] = Counter()
    invalid_events = 0
    for ev in selected:
        if not isinstance(ev, dict):
            invalid_events += 1
            continue
        src = ev.get("source") or {}
        if not isinstance(src, dict):
            invalid_events += 1
            continue
        st = src.get("type")
        source_type = _mapped_name(st, inv_source)
        source_id = src.get("id")
        params = ev.get("params")
        nodes.add((source_id, source_type))
        event_type = _mapped_name(ev.get("type"), inv_event)
        phase = ev.get("phase")
        phase_key = (source_id, event_type)
        if phase == begin_phase:
            phase_balance[phase_key] += 1
        elif phase == end_phase:
            phase_balance[phase_key] -= 1
        try:
            event_time_ms = float(ev.get("time"))
        except (TypeError, ValueError):
            event_time_ms = None
        unix_ms = (
            event_time_ms + model["time_tick_offset_ms"]
            if event_time_ms is not None and model["time_tick_offset_ms"] is not None
            else None
        )
        dependencies = _source_dependencies(params, inv_source)
        for dependency_id, dependency_type in dependencies:
            edges.append({
                "from_id": source_id,
                "from_type": source_type,
                "to_id": dependency_id,
                "to_type": dependency_type,
            })
        entries.append({
            "time": ev.get("time"),
            "time_unix_ms": unix_ms,
            "phase": phase,
            "source_id": source_id,
            "source_type": source_type,
            "type": ev.get("type"),
            "event_type": event_type,
            "params": params,
            "source_dependencies": [
                {"id": dependency_id, "type": dependency_type}
                for dependency_id, dependency_type in dependencies
            ],
        })

    dangling_edges = [
        edge for edge in edges
        if (edge["to_id"], edge["to_type"]) not in nodes
        and not any(node[0] == edge["to_id"] for node in nodes)
    ]
    unmatched_begin = sum(value for value in phase_balance.values() if value > 0)
    unmatched_end = sum(-value for value in phase_balance.values() if value < 0)
    capture_complete = (
        not truncated and invalid_events == 0
        and unmatched_begin == 0 and unmatched_end == 0
    )
    return NetlogReadResult(
        status="success",
        entries=entries,
        quality={
            "capture_complete": capture_complete,
            "size_bytes": size_bytes,
            "bytes_read": len(raw),
            "max_input_bytes": max_input_bytes,
            "events_declared": len(events),
            "records_seen": len(entries),
            "invalid_events": invalid_events,
            "max_events": max_events,
            "truncated": truncated,
            "unmatched_begin": unmatched_begin,
            "unmatched_end": unmatched_end,
        },
        time_model=model,
        dependency_graph={
            "node_count": len(nodes),
            "edge_count": len(edges),
            "dangling_edge_count": len(dangling_edges),
            "closed": bool(edges) and not dangling_edges,
            "edges": edges[:1000],
            "truncated": len(edges) > 1000,
        },
    )


def iter_entries(path: Path) -> Iterator[dict[str, Any]]:
    """Yield normalized rows through the bounded reader."""
    yield from read_file(path).entries


def apply_filter(entries: Iterable[dict[str, Any]],
                 spec: NetlogFilter | None) -> list[dict[str, Any]]:
    if spec is None or spec.is_empty():
        return list(entries)
    return [e for e in entries if spec.matches(e)]


def around_window(entries: Iterable[dict[str, Any]],
                  anchor: datetime,
                  window_seconds: int) -> list[dict[str, Any]]:
    anchor_utc = anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)
    center_ms = anchor_utc.timestamp() * 1000
    radius_ms = window_seconds * 1000
    return [
        entry for entry in entries
        if entry.get("time_unix_ms") is not None
        and abs(float(entry["time_unix_ms"]) - center_ms) <= radius_ms
    ]


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {"count": 0}
    by_src = Counter(e.get("source_type", "?") for e in entries)
    by_phase = Counter(e.get("phase", -1) for e in entries)
    return {
        "count": len(entries),
        "top_source_types": by_src.most_common(10),
        "phase_distribution": dict(by_phase),
    }


def query(sources: Iterable[Path | str],
          *,
          filter: NetlogFilter | None = None,
          time_range=None,
          around=None,
          window_seconds: int = 300,
          limit: int | None = None,
          max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
          max_events: int = DEFAULT_MAX_EVENTS) -> dict[str, Any]:
    src_files = [Path(p) for p in sources]
    entries: list[dict[str, Any]] = []
    acquisitions: list[dict[str, Any]] = []
    time_models: list[dict[str, Any]] = []
    dependency_graphs: list[dict[str, Any]] = []
    for p in src_files:
        read = read_file(
            p, max_input_bytes=max_input_bytes, max_events=max_events
        )
        entries.extend(read.entries)
        acquisitions.append({
            "source": p.name,
            "status": read.status,
            "error": read.error,
            "quality": read.quality,
        })
        time_models.append(read.time_model)
        dependency_graphs.append(read.dependency_graph)

    if time_range and any(time_range):
        start, end = time_range
        if start is not None:
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        if end is not None:
            end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        entries = [
            entry for entry in entries
            if entry.get("time_unix_ms") is not None
            and (start is None or float(entry["time_unix_ms"]) >= start.timestamp() * 1000)
            and (end is None or float(entry["time_unix_ms"]) <= end.timestamp() * 1000)
        ]

    if around is not None:
        entries = around_window(entries, around, window_seconds)

    if filter is not None and not filter.is_empty():
        entries = apply_filter(entries, filter)

    truncated = False
    if limit is not None and len(entries) > limit:
        truncated = True
        entries = entries[:limit]

    statuses = {item["status"] for item in acquisitions}
    status = (
        "no_input" if not acquisitions
        else "success" if statuses == {"success"}
        else "partial" if "success" in statuses
        else next(iter(statuses)) if len(statuses) == 1
        else "failed"
    )
    capture_complete = bool(acquisitions) and all(
        item["status"] == "success"
        and bool((item.get("quality") or {}).get("capture_complete"))
        for item in acquisitions
    )
    return {
        "status": status,
        "summary": summarise(entries),
        "entries": entries,
        "truncated": truncated or not capture_complete,
        "source_files": [str(p) for p in src_files],
        "acquisitions": acquisitions,
        "time_models": time_models,
        "dependency_graphs": dependency_graphs,
        "quality": {
            "capture_complete": capture_complete,
            "records_seen": len(entries),
            "time_comparable": bool(time_models) and all(
                model.get("wall_clock_available") for model in time_models
            ),
            "dependency_graph_closed": bool(dependency_graphs) and all(
                graph.get("closed") for graph in dependency_graphs
            ),
            "truncated": truncated or not capture_complete,
        },
    }
