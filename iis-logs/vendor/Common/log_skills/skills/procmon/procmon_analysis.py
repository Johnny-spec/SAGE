"""Noise-aware, bounded analysis for normalized Process Monitor events."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import PureWindowsPath
from typing import Any, Iterable


MAX_DIMENSION_KEYS = 2_000
MAX_PATTERNS = 2_000
MAX_SAMPLES = 200
MIN_SAMPLE_SIZE = 20

BENIGN_RESULTS = {
    "SUCCESS",
    "BUFFER OVERFLOW",
    "REPARSE",
    "END OF FILE",
    "NO MORE FILES",
    "FAST IO DISALLOWED",
    "FILE LOCKED WITH ONLY READERS",
}
MISSING_RESULTS = {
    "NAME NOT FOUND", "PATH NOT FOUND", "NO SUCH FILE", "OBJECT NAME NOT FOUND",
}
ACCESS_RESULTS = {
    "ACCESS DENIED", "PRIVILEGE NOT HELD", "ACCOUNT RESTRICTION",
}
LOCK_RESULTS = {
    "SHARING VIOLATION", "LOCK VIOLATION", "FILE LOCKED WITH WRITERS",
}
NETWORK_FAILURE_TERMS = (
    "CONNECTION REFUSED", "CONNECTION RESET", "NETWORK UNREACHABLE",
    "HOST UNREACHABLE", "BAD NETWORK PATH", "TIMEOUT", "DISCONNECTED",
)
DEPENDENCY_SUFFIXES = (".dll", ".sys", ".exe", ".config", ".json", ".xml")

_TIME_ONLY_FORMATS = (
    "%I:%M:%S.%f %p", "%I:%M:%S %p", "%H:%M:%S.%f", "%H:%M:%S",
)
_DATE_TIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S.%f %p", "%m/%d/%Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
)
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_DETAIL_PATTERNS = {
    "user_time_seconds": re.compile(r"(?i)\bUser Time:\s*([\d,.]+)\s+seconds?"),
    "kernel_time_seconds": re.compile(r"(?i)\bKernel Time:\s*([\d,.]+)\s+seconds?"),
    "private_bytes": re.compile(r"(?i)\bPrivate Bytes:\s*([\d,]+)"),
    "working_set_bytes": re.compile(r"(?i)\bWorking Set:\s*([\d,]+)"),
    "transfer_bytes": re.compile(r"(?i)(?:^|,\s*)(?:Transfer )?Length:\s*([\d,]+)"),
}


class BoundedCounter:
    """A deterministic bounded counter with disclosed overflow."""

    def __init__(self, capacity: int = MAX_DIMENSION_KEYS):
        self.capacity = max(1, capacity)
        self.counts: Counter[Any] = Counter()
        self.overflow = 0

    def add(self, key: Any, amount: int = 1) -> None:
        if key in self.counts or len(self.counts) < self.capacity:
            self.counts[key] += amount
        else:
            self.overflow += amount

    def most_common(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"key": key, "count": count}
            for key, count in self.counts.most_common(limit)
        ]


@dataclass
class DurationHistogram:
    bounds: tuple[float, ...] = (
        0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0,
    )
    buckets: list[int] = field(default_factory=lambda: [0] * 12)
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0

    def add(self, value: float | None) -> None:
        if value is None or not math.isfinite(value) or value < 0:
            return
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        index = next((i for i, bound in enumerate(self.bounds) if value <= bound), len(self.bounds))
        self.buckets[index] += 1

    def percentile_upper_bound(self, percentile: float) -> float | None:
        if not self.count:
            return None
        target = max(1, math.ceil(self.count * percentile))
        cumulative = 0
        for index, count in enumerate(self.buckets):
            cumulative += count
            if cumulative >= target:
                return self.bounds[index] if index < len(self.bounds) else self.maximum
        return self.maximum

    def summary(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "average_seconds": round(self.total / self.count, 6) if self.count else None,
            "p95_upper_bound_seconds": self.percentile_upper_bound(0.95),
            "maximum_seconds": round(self.maximum, 6) if self.count else None,
            "model": "fixed_histogram_upper_bound",
        }


@dataclass(frozen=True)
class AnalysisFilter:
    process_names: frozenset[str] = frozenset()
    pids: frozenset[str] = frozenset()
    operation_families: frozenset[str] = frozenset()
    result_contains: str | None = None
    path_contains: str | None = None
    contains: str | None = None

    @classmethod
    def from_extra(cls, extra: dict[str, Any]) -> "AnalysisFilter":
        raw = extra.get("procmon_filter") or {}
        if not isinstance(raw, dict):
            raw = {}
        names = raw.get("process_names") or extra.get("process_names") or []
        pids = raw.get("pids") or extra.get("pids") or []
        families = raw.get("operation_families") or []
        return cls(
            process_names=frozenset(str(value).casefold() for value in names),
            pids=frozenset(str(value) for value in pids),
            operation_families=frozenset(str(value).casefold() for value in families),
            result_contains=_optional_casefold(raw.get("result_contains")),
            path_contains=_optional_casefold(raw.get("path_contains")),
            contains=_optional_casefold(raw.get("contains")),
        )


def _optional_casefold(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.casefold() if text else None


def operation_family(operation: str) -> str:
    value = operation.strip().casefold()
    if value.startswith("reg"):
        return "registry"
    if value.startswith(("tcp", "udp")) or "network" in value:
        return "network"
    if value in {"process profiling"}:
        return "profiling"
    if value.startswith(("process", "thread")) or value in {"load image", "debug output profiling"}:
        return "process"
    if (
        "file" in value
        or value.startswith(("irp_", "fastio_", "querydirectory", "createfile"))
        or value in {"queryopen", "setrenameinformationfile", "filesystemcontrol"}
    ):
        return "filesystem"
    return "other"


def parse_duration_seconds(value: str) -> float | None:
    match = _NUMBER_RE.search(value or "")
    if not match:
        return None


def parse_detail_metrics(value: str) -> dict[str, int | float]:
    """Extract numeric Procmon Detail fields without retaining sensitive text."""
    output: dict[str, int | float] = {}
    for key, pattern in _DETAIL_PATTERNS.items():
        match = pattern.search(value or "")
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        try:
            output[key] = float(raw) if key.endswith("_seconds") else int(raw)
        except ValueError:
            continue
    return output
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _trim_fraction(value: str) -> str:
    return _FRACTION_RE.sub(r"\1", value.strip())


def parse_timestamp(value: str) -> tuple[datetime | datetime_time | None, str]:
    """Parse Procmon's date-time or time-only display columns."""
    text = _trim_fraction(value)
    for fmt in _DATE_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), "absolute"
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None), "absolute"
    except ValueError:
        pass
    for fmt in _TIME_ONLY_FORMATS:
        try:
            return datetime.strptime(text, fmt).time(), "time_of_day"
        except ValueError:
            pass
    return None, "unparseable"


class TimestampResolver:
    def __init__(self, anchor: datetime | None = None):
        self.anchor = anchor.replace(tzinfo=None) if anchor else None
        self.current_date: date | None = self.anchor.date() if self.anchor else None
        self.last_value: datetime | None = None
        self.models: Counter[str] = Counter()
        self.unparseable = 0

    def resolve(self, value: str) -> datetime | None:
        parsed, model = parse_timestamp(value)
        if parsed is None:
            self.unparseable += 1
            return None
        if isinstance(parsed, datetime):
            resolved = parsed.replace(tzinfo=None)
            self.models["absolute"] += 1
        elif self.current_date is not None:
            resolved = datetime.combine(self.current_date, parsed)
            if self.last_value and resolved < self.last_value - timedelta(hours=12):
                self.current_date += timedelta(days=1)
                resolved = datetime.combine(self.current_date, parsed)
            elif self.last_value is None and self.anchor and resolved < self.anchor - timedelta(hours=12):
                self.current_date += timedelta(days=1)
                resolved = datetime.combine(self.current_date, parsed)
            self.models["anchored_time_of_day"] += 1
        else:
            self.models["relative_time_of_day"] += 1
            return None
        self.last_value = resolved
        return resolved

    def begin_source(self) -> None:
        """Reset rollover state while retaining aggregate timestamp quality."""
        self.current_date = self.anchor.date() if self.anchor else None
        self.last_value = None

    @property
    def comparable(self) -> bool:
        return bool(self.models["absolute"] or self.models["anchored_time_of_day"])

    def summary(self) -> dict[str, Any]:
        return {
            "models": dict(self.models),
            "unparseable": self.unparseable,
            "wall_clock_comparable": self.comparable,
            "anchor": self.anchor.isoformat() if self.anchor else None,
        }


def parse_range(value: Any) -> tuple[datetime, datetime] | None:
    if not isinstance(value, dict) or not value.get("start") or not value.get("end"):
        return None
    try:
        start = datetime.fromisoformat(str(value["start"]).strip().replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(value["end"]).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    start = start.replace(tzinfo=None)
    end = end.replace(tzinfo=None)
    return (start, end) if start <= end else None


@dataclass
class PatternRecord:
    kind: str
    severity: str
    process_name: str
    pid: str
    operation: str
    result: str
    path: str
    family: str
    count: int = 0
    duration_total: float = 0.0
    duration_max: float = 0.0
    sample: dict[str, Any] = field(default_factory=dict)

    def add(self, row: dict[str, str], duration: float | None) -> None:
        self.count += 1
        if duration is not None:
            self.duration_total += duration
            self.duration_max = max(self.duration_max, duration)
        if not self.sample:
            self.sample = {
                "source_file": row.get("source_file"),
                "line_number": row.get("line_number"),
                "timestamp": row.get("timestamp"),
                "pid": row.get("pid"),
                "operation": row.get("operation"),
                "result": row.get("result"),
                "path": row.get("path"),
            }


@dataclass
class ProcessProfile:
    process_name: str
    pid: str
    samples: int = 0
    user_time_seconds: float = 0.0
    kernel_time_seconds: float = 0.0
    private_bytes: int = 0
    working_set_bytes: int = 0

    def update(self, metrics: dict[str, int | float]) -> None:
        self.samples += 1
        self.user_time_seconds = max(
            self.user_time_seconds, float(metrics.get("user_time_seconds", 0.0))
        )
        self.kernel_time_seconds = max(
            self.kernel_time_seconds, float(metrics.get("kernel_time_seconds", 0.0))
        )
        self.private_bytes = max(
            self.private_bytes, int(metrics.get("private_bytes", 0))
        )
        self.working_set_bytes = max(
            self.working_set_bytes, int(metrics.get("working_set_bytes", 0))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_name": self.process_name,
            "pid": self.pid,
            "samples": self.samples,
            "user_time_seconds_max": round(self.user_time_seconds, 6),
            "kernel_time_seconds_max": round(self.kernel_time_seconds, 6),
            "private_bytes_max": self.private_bytes,
            "working_set_bytes_max": self.working_set_bytes,
        }


@dataclass
class WindowStats:
    rows: int = 0
    processes: BoundedCounter = field(default_factory=BoundedCounter)
    pids: BoundedCounter = field(default_factory=BoundedCounter)
    operations: BoundedCounter = field(default_factory=BoundedCounter)
    results: BoundedCounter = field(default_factory=BoundedCounter)
    families: BoundedCounter = field(default_factory=BoundedCounter)
    durations: DurationHistogram = field(default_factory=DurationHistogram)
    io_bytes_by_process: BoundedCounter = field(default_factory=BoundedCounter)
    io_bytes_total: int = 0
    process_profiles: dict[tuple[str, str], ProcessProfile] = field(default_factory=dict)
    patterns: dict[tuple[str, str, str, str, str], PatternRecord] = field(default_factory=dict)
    dropped_pattern_keys: int = 0
    first_time: datetime | None = None
    last_time: datetime | None = None

    def add_dimensions(
        self, row: dict[str, str], family: str, timestamp: datetime | None, duration: float | None,
    ) -> None:
        self.rows += 1
        process = row.get("process_name") or "-"
        pid = row.get("pid") or "-"
        self.processes.add(process)
        self.pids.add((process, pid))
        self.operations.add(row.get("operation") or "-")
        self.results.add(row.get("result") or "-")
        self.families.add(family)
        self.durations.add(duration)
        if timestamp is not None:
            self.first_time = timestamp if self.first_time is None else min(self.first_time, timestamp)
            self.last_time = timestamp if self.last_time is None else max(self.last_time, timestamp)

    def add_detail_metrics(self, row: dict[str, str], family: str) -> None:
        metrics = parse_detail_metrics(row.get("detail") or "")
        transfer_bytes = int(metrics.get("transfer_bytes", 0))
        if transfer_bytes and family in {"filesystem", "network"}:
            process = row.get("process_name") or "-"
            self.io_bytes_total += transfer_bytes
            self.io_bytes_by_process.add(process, transfer_bytes)
        if family != "profiling" or not metrics:
            return
        key = (row.get("process_name") or "-", row.get("pid") or "-")
        profile = self.process_profiles.get(key)
        if profile is None:
            if len(self.process_profiles) >= MAX_DIMENSION_KEYS:
                return
            profile = ProcessProfile(process_name=key[0], pid=key[1])
            self.process_profiles[key] = profile
        profile.update(metrics)


def _path_key(path: str) -> str:
    if not path:
        return "-"
    name = PureWindowsPath(path).name
    return name or path[-160:]


def _signal_for(row: dict[str, str], family: str, duration: float | None) -> tuple[str, str] | None:
    result = (row.get("result") or "").strip().upper()
    operation = (row.get("operation") or "").strip().casefold()
    path = (row.get("path") or "").strip().casefold()
    if result in ACCESS_RESULTS:
        return "access_denied", "warning"
    if result in LOCK_RESULTS:
        return "lock_contention", "warning"
    if family == "network" and result not in BENIGN_RESULTS:
        if any(term in result for term in NETWORK_FAILURE_TERMS) or result:
            return "network_failure", "warning"
    if operation == "load image" and result not in BENIGN_RESULTS:
        return "image_load_failure", "critical"
    if result in MISSING_RESULTS:
        if path.endswith(DEPENDENCY_SUFFIXES):
            return "missing_dependency", "warning"
        return "repeated_path_probe", "info"
    if operation in {"process create", "process start"} and result in BENIGN_RESULTS:
        return "process_restart_loop", "warning"
    if duration is not None and duration >= 0.5 and family in {"filesystem", "network", "registry"}:
        return "slow_operation", "warning"
    return None


_PATTERN_THRESHOLDS = {
    "access_denied": 3,
    "lock_contention": 3,
    "network_failure": 3,
    "image_load_failure": 1,
    "missing_dependency": 3,
    "repeated_path_probe": 20,
    "process_restart_loop": 10,
    "slow_operation": 5,
}

_FALSIFIERS = {
    "access_denied": "The same operation succeeds for the same process and target during the incident window.",
    "lock_contention": "The lock result is transient and does not coincide with the reported delay or failure.",
    "network_failure": "NetLog or packet evidence shows the connection completed without retry in the same window.",
    "image_load_failure": "A dump or loader event proves a different module or exception caused the failure.",
    "missing_dependency": "The missing probe is followed by a successful fallback lookup before the symptom.",
    "repeated_path_probe": "The same probe rate appears in a healthy baseline and the application succeeds.",
    "process_restart_loop": "Process identity or command line shows the launches are distinct expected workers.",
    "slow_operation": "WPR/perf evidence attributes the delay to CPU scheduling rather than this I/O path.",
}

_NEXT_SOURCE = {
    "access_denied": "Security EVTX and effective ACL/token evidence",
    "lock_contention": "WPR file I/O trace or process dump",
    "network_failure": "NetLog or packet capture",
    "image_load_failure": "Crash dump and Application EVTX",
    "missing_dependency": "Application EVTX, loader snaps, or crash dump",
    "repeated_path_probe": "Healthy baseline Procmon capture",
    "process_restart_loop": "Application/System EVTX and crash dumps",
    "slow_operation": "WPR/perf counters",
}


class ProcmonAnalyzer:
    def __init__(
        self,
        *,
        incident_range: tuple[datetime, datetime] | None = None,
        baseline_range: tuple[datetime, datetime] | None = None,
        filter_spec: AnalysisFilter | None = None,
    ):
        self.incident_range = incident_range
        self.baseline_range = baseline_range
        anchor = incident_range[0] if incident_range else baseline_range[0] if baseline_range else None
        self.timestamps = TimestampResolver(anchor)
        self.filter_spec = filter_spec or AnalysisFilter()
        self.incident = WindowStats()
        self.baseline = WindowStats()
        self.rows_received = 0
        self.rows_filtered = 0
        self.rows_outside_windows = 0
        self.process_tree: dict[tuple[str, str], dict[str, str]] = {}

    def begin_source(self) -> None:
        self.timestamps.begin_source()

    def _matches_filter(self, row: dict[str, str], family: str) -> bool:
        spec = self.filter_spec
        if spec.process_names and (row.get("process_name") or "").casefold() not in spec.process_names:
            return False
        if spec.pids and (row.get("pid") or "") not in spec.pids:
            return False
        if spec.operation_families and family.casefold() not in spec.operation_families:
            return False
        if spec.result_contains and spec.result_contains not in (row.get("result") or "").casefold():
            return False
        if spec.path_contains and spec.path_contains not in (row.get("path") or "").casefold():
            return False
        if spec.contains:
            haystack = " ".join(str(value) for value in row.values()).casefold()
            if spec.contains not in haystack:
                return False
        return True

    def _window(self, timestamp: datetime | None) -> WindowStats | None:
        if timestamp is not None:
            if self.incident_range and self.incident_range[0] <= timestamp <= self.incident_range[1]:
                return self.incident
            if self.baseline_range and self.baseline_range[0] <= timestamp <= self.baseline_range[1]:
                return self.baseline
        if not self.incident_range and not self.baseline_range:
            return self.incident
        return None

    def consume(self, row: dict[str, str]) -> None:
        self.rows_received += 1
        family = operation_family(row.get("operation") or "")
        if not self._matches_filter(row, family):
            self.rows_filtered += 1
            return
        timestamp = self.timestamps.resolve(row.get("timestamp") or "")
        target = self._window(timestamp)
        if target is None:
            self.rows_outside_windows += 1
            return
        duration = parse_duration_seconds(row.get("duration") or "")
        target.add_dimensions(row, family, timestamp, duration)
        target.add_detail_metrics(row, family)

        process_key = (row.get("process_name") or "-", row.get("pid") or "-")
        if process_key not in self.process_tree and len(self.process_tree) < MAX_DIMENSION_KEYS:
            self.process_tree[process_key] = {
                "process_name": process_key[0],
                "pid": process_key[1],
                "parent_pid": row.get("parent_pid") or "",
            }

        signal = _signal_for(row, family, duration)
        if signal is None:
            return
        kind, severity = signal
        key = (
            kind,
            (row.get("process_name") or "-").casefold(),
            (row.get("operation") or "-").casefold(),
            (row.get("result") or "-").upper(),
            _path_key(row.get("path") or "").casefold(),
        )
        record = target.patterns.get(key)
        if record is None:
            if len(target.patterns) >= MAX_PATTERNS:
                target.dropped_pattern_keys += 1
                return
            record = PatternRecord(
                kind=kind,
                severity=severity,
                process_name=row.get("process_name") or "-",
                pid=row.get("pid") or "-",
                operation=row.get("operation") or "-",
                result=row.get("result") or "-",
                path=row.get("path") or "-",
                family=family,
            )
            target.patterns[key] = record
        record.add(row, duration)

    def patterns(self, limit: int = 20) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for key, record in self.incident.patterns.items():
            threshold = _PATTERN_THRESHOLDS.get(record.kind, 1)
            baseline_count = self.baseline.patterns.get(key, PatternRecord(
                record.kind, record.severity, "", "", "", "", "", ""
            )).count
            baseline_rate = (
                baseline_count / self.baseline.rows if self.baseline.rows else 0.0
            )
            incident_rate = record.count / self.incident.rows if self.incident.rows else 0.0
            ratio = (
                incident_rate / baseline_rate if baseline_rate > 0
                else None if not self.baseline.rows else float("inf")
            )
            material_delta = ratio is not None and ratio >= 3.0 and record.count >= 3
            if record.count < threshold and not material_delta:
                continue
            output.append({
                "type": record.kind,
                "severity": record.severity,
                "process_name": record.process_name,
                "pid": record.pid,
                "operation": record.operation,
                "result": record.result,
                "path": record.path,
                "operation_family": record.family,
                "count": record.count,
                "baseline_count": baseline_count,
                "incident_rate": round(incident_rate, 8),
                "baseline_rate": round(baseline_rate, 8),
                "baseline_ratio": (
                    "new_in_incident" if ratio == float("inf") else round(ratio, 3)
                    if isinstance(ratio, float) else None
                ),
                "duration_average_seconds": (
                    round(record.duration_total / record.count, 6)
                    if record.duration_total else None
                ),
                "duration_max_seconds": round(record.duration_max, 6) or None,
                "sample": record.sample,
                "falsifier": _FALSIFIERS[record.kind],
                "next_source": _NEXT_SOURCE[record.kind],
                "threshold": threshold,
            })
        severity_rank = {"critical": 3, "warning": 2, "info": 1}
        output.sort(
            key=lambda item: (
                severity_rank.get(item["severity"], 0), item["count"],
            ),
            reverse=True,
        )
        return output[:limit]

    def _window_summary(self, stats: WindowStats) -> dict[str, Any]:
        elapsed = (
            (stats.last_time - stats.first_time).total_seconds()
            if stats.first_time and stats.last_time and stats.last_time > stats.first_time
            else None
        )
        return {
            "rows": stats.rows,
            "first_time": stats.first_time.isoformat() if stats.first_time else None,
            "last_time": stats.last_time.isoformat() if stats.last_time else None,
            "event_rate_per_second": round(stats.rows / elapsed, 3) if elapsed else None,
            "top_processes": stats.processes.most_common(),
            "top_pids": stats.pids.most_common(),
            "top_operations": stats.operations.most_common(),
            "top_results": stats.results.most_common(),
            "operation_families": stats.families.most_common(),
            "durations": stats.durations.summary(),
            "io": {
                "observed_transfer_bytes": stats.io_bytes_total,
                "top_processes_by_observed_bytes": stats.io_bytes_by_process.most_common(),
                "scope": "Procmon Detail Length/Transfer Length fields only",
            },
            "process_profiling": [
                profile.to_dict()
                for profile in sorted(
                    stats.process_profiles.values(),
                    key=lambda item: (
                        item.working_set_bytes,
                        item.private_bytes,
                        item.user_time_seconds + item.kernel_time_seconds,
                    ),
                    reverse=True,
                )[:20]
            ],
            "bounded_counter_overflow": {
                "processes": stats.processes.overflow,
                "pids": stats.pids.overflow,
                "operations": stats.operations.overflow,
                "results": stats.results.overflow,
                "families": stats.families.overflow,
                "io_processes": stats.io_bytes_by_process.overflow,
                "pattern_keys": stats.dropped_pattern_keys,
            },
        }

    def summary(self) -> dict[str, Any]:
        patterns = self.patterns()
        return {
            "rows_received": self.rows_received,
            "rows_filtered": self.rows_filtered,
            "rows_outside_windows": self.rows_outside_windows,
            "incident": self._window_summary(self.incident),
            "baseline": self._window_summary(self.baseline),
            "patterns": patterns,
            "process_tree": list(self.process_tree.values())[:100],
            "timestamp_model": self.timestamps.summary(),
            "candidate_cause": (
                _candidate_summary(patterns[0]) if patterns else None
            ),
            "sample_sufficient": self.incident.rows >= MIN_SAMPLE_SIZE,
            "analysis_limits": {
                "max_dimension_keys": MAX_DIMENSION_KEYS,
                "max_pattern_keys": MAX_PATTERNS,
                "max_output_patterns": 20,
                "max_process_tree_rows": 100,
            },
        }


def _candidate_summary(pattern: dict[str, Any]) -> str:
    return (
        f"{pattern['type']} pattern for {pattern['process_name']} "
        f"({pattern['operation']} / {pattern['result']}, count={pattern['count']})"
    )


def finding_summary(pattern: dict[str, Any]) -> str:
    labels = {
        "access_denied": "Repeated access-denied operations",
        "lock_contention": "Repeated file/registry lock contention",
        "network_failure": "Repeated network operation failures",
        "image_load_failure": "Image or DLL load failure",
        "missing_dependency": "Repeated missing dependency/configuration lookup",
        "repeated_path_probe": "High-volume missing-path lookup pattern",
        "process_restart_loop": "Repeated process creation pattern",
        "slow_operation": "Repeated slow I/O operation pattern",
    }
    return (
        f"{labels.get(pattern['type'], pattern['type'])}: "
        f"{pattern['process_name']} performed {pattern['operation']} "
        f"with {pattern['result']} {pattern['count']} times."
    )


__all__ = [
    "AnalysisFilter", "BENIGN_RESULTS", "ProcmonAnalyzer", "TimestampResolver",
    "finding_summary", "operation_family", "parse_detail_metrics", "parse_duration_seconds", "parse_range",
    "parse_timestamp",
]