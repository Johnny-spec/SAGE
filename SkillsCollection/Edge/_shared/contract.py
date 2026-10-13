"""
_shared/contract.py — Edge Diagnostics Skill Collection

Dependency-free helpers used by every Edge skill. Mirrors the IIS contract
so an agent that already understands the IIS collection can use these skills
with no additional learning.

Python 3.10+, standard library only.
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT: Path = Path(__file__).resolve().parent.parent
COMMON_ROOT = ROOT.parent / "Common"
if COMMON_ROOT.is_dir() and str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))
from investigation_protocol import (  # type: ignore[reportMissingImports]  # noqa: E402
    CollectionPlan, EvidenceGap, EvidenceState,
    build_collection_plan, gaps_from_missing,
)
from redaction import redact_result  # type: ignore[reportMissingImports]  # noqa: E402
from provenance import attach_finding_provenance  # type: ignore[reportMissingImports]  # noqa: E402
from result_contract import enforce_root_quality  # type: ignore[reportMissingImports]  # noqa: E402


def _skill_root_for_registry_path(registry_path: Path) -> Path:
    if registry_path.parent.name == "_shared":
        return registry_path.parent.parent
    return registry_path.parent


@lru_cache(maxsize=16)
def _load_registry_file(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _registry_path() -> Path:
    return (ROOT / "_shared" / "registry.json").resolve()


def _iter_external_registry_paths(local_reg: dict[str, Any]) -> Iterable[Path]:
    for rel in local_reg.get("external_registries", []):
        try:
            p = (ROOT / str(rel)).resolve()
        except Exception:
            continue
        if p.exists():
            yield p


def _resolve_skill_entry(reg: dict[str, Any], registry_path: Path,
                         skill_id: str, runtime: str) -> Path | None:
    skill = (reg.get("skills") or {}).get(skill_id)
    if not isinstance(skill, dict):
        return None
    rel = (skill.get("entry") or {}).get(runtime)
    if not rel:
        raise KeyError(f"Skill {skill_id} has no {runtime} entry")
    return (_skill_root_for_registry_path(registry_path) / str(rel)).resolve()


def registry() -> dict[str, Any]:
    return _load_registry_file(str(_registry_path()))


def skill_entry(skill_id: str, runtime: str = "python") -> Path:
    reg = registry()

    local = _resolve_skill_entry(reg, _registry_path(), skill_id, runtime)
    if local is not None:
        return local

    overrides = reg.get("skill_overrides") or {}
    lookup_id = str(overrides.get(skill_id, skill_id))
    for ext_reg_path in _iter_external_registry_paths(reg):
        ext_reg = _load_registry_file(str(ext_reg_path))
        ext = _resolve_skill_entry(ext_reg, ext_reg_path, lookup_id, runtime)
        if ext is not None:
            return ext

    raise KeyError(f"Unknown skill: {skill_id}")


def load_context(argv: list[str] | None = None) -> dict[str, Any]:
    """
    Read the first positional argument as a context object.
    Accepts:
      * '{...json...}'       — inline JSON
      * '@path/to/file.json' — read from file
      * (missing)            — return {}
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return {}
    raw = args[0]
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as fh:
            return json.load(fh)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        p = Path(raw)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        raise


@dataclass
class Finding:
    summary: str
    severity: str = "info"            # critical | warning | info
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Solution:
    """A concrete fix proposal for a key problem."""
    title: str
    steps: list[str] = field(default_factory=list)
    problem_ref: str | None = None
    severity: str = "info"
    references: list[str] = field(default_factory=list)


@dataclass
class NextStep:
    """A recommended follow-up investigation action."""
    action: str
    why: str | None = None
    skill: str | None = None


@dataclass
class LogRequest:
    """An additional log/data source needed to deepen the analysis."""
    log_kind: str
    why: str
    how_to_collect: str | None = None
    skill: str | None = None


# ---------------------------------------------------------------------------
# Cross-Validation structures (validator skill)
# ---------------------------------------------------------------------------

@dataclass
class CrossValidationAnalysis:
    """A single AI analysis result (Claude or GPT)."""
    analyzer: str                      # "claude" or "gpt"
    findings: list[str] = field(default_factory=list)  # key findings from this analyzer
    confidence: float = 0.0            # 0.0 to 1.0
    reasoning: dict[str, Any] = field(default_factory=dict)  # detailed reasoning steps
    alternative_hypotheses: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CrossValidationConsensus:
    """Comparison result between Claude and GPT analyses."""
    agreed_findings: list[str] = field(default_factory=list)  # items both agree on
    conflicting_findings: list[dict[str, Any]] = field(default_factory=list)  # differences
    partial_agreement: list[dict[str, Any]] = field(default_factory=list)  # partial overlaps
    consensus_confidence: float = 0.0  # min(claude.conf, gpt.conf) * agreement_ratio
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EvidenceChainStep:
    """One step in the evidence chain (data collection → analysis → resolution)."""
    step_name: str                     # e.g. "data_collection", "claude_analysis", "conflict_resolution"
    method: str | None = None         # how it was done (e.g. "regex_extraction", "mcp_call")
    input_summary: dict[str, Any] = field(default_factory=dict)  # what went in
    output_summary: dict[str, Any] = field(default_factory=dict)  # what came out
    data_range: str | None = None     # e.g. "lines 42-67, 2026-07-02T10:05:00Z"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CrossValidation:
    """Cross-validation framework result attached to SkillResult."""
    enabled: bool = False
    problem_type: str | None = None    # e.g. "5xx_error", "crash"
    severity: str | None = None        # critical | warning | info
    analyses: dict[str, CrossValidationAnalysis] = field(default_factory=dict)  # {claude, gpt}
    consensus: CrossValidationConsensus | None = None
    evidence_chain: list[EvidenceChainStep] = field(default_factory=list)
    new_hypotheses: list[str] = field(default_factory=list)  # discovered during evidence_chain
    validation_notes: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SkillResult:
    skill: str
    ok: bool = True
    findings: list[Finding] = field(default_factory=list)
    root_cause: str | None = None
    confidence: str = "low"           # high | medium | low
    recommendations: list[str] = field(default_factory=list)
    # v2.1 structured advisory sections (auto-flattened into recommendations
    # by emit() when caller leaves recommendations empty).
    solutions: list[Solution] = field(default_factory=list)
    next_steps: list[NextStep] = field(default_factory=list)
    additional_logs_needed: list[LogRequest] = field(default_factory=list)
    cross_validation: CrossValidation | None = None  # v2.2 — cross-validation framework
    evidence_state: str = EvidenceState.INCONCLUSIVE_EVIDENCE
    evidence_gaps: list[EvidenceGap] = field(default_factory=list)
    collection_plan: CollectionPlan | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def _flatten_recommendations(self) -> list[str]:
        out: list[str] = []
        for s in self.solutions:
            head = f"[fix:{s.problem_ref}] {s.title}" if s.problem_ref else f"[fix] {s.title}"
            if s.steps:
                head += f" — {s.steps[0]}"
            out.append(head)
        for n in self.next_steps:
            tag = f"[next:{n.skill}]" if n.skill else "[next]"
            out.append(f"{tag} {n.action}")
        for l in self.additional_logs_needed:
            out.append(f"[logs:{l.log_kind}] {l.why}")
        return out

    def to_json(self) -> str:
        if not self.recommendations and (self.solutions or self.next_steps
                                          or self.additional_logs_needed):
            self.recommendations = self._flatten_recommendations()
        if self.collection_plan is None and self.additional_logs_needed:
            self.collection_plan = build_collection_plan(
                self.additional_logs_needed, products=["Edge"]
            )
        if not self.evidence_gaps and self.additional_logs_needed:
            self.evidence_gaps = gaps_from_missing(self.additional_logs_needed)
        if self.evidence_state == EvidenceState.INCONCLUSIVE_EVIDENCE:
            if self.collection_plan and self.collection_plan.requests:
                self.evidence_state = EvidenceState.COLLECTION_PLAN_READY
        payload = attach_finding_provenance(asdict(self))
        payload = enforce_root_quality(payload)
        return json.dumps(
            redact_result(payload), indent=2, ensure_ascii=True, default=str
        )

    def emit(self) -> None:
        print(self.to_json())


def fail(skill: str, message: str, exit_code: int = 1) -> None:
    SkillResult(skill=skill, ok=False, error=message, confidence="low").emit()
    sys.exit(exit_code)


def parse_time(value: str) -> datetime:
    s = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def time_range(ctx: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    tr = ctx.get("time_range") or {}
    start = parse_time(tr["start"]) if tr.get("start") else None
    end = parse_time(tr["end"]) if tr.get("end") else None
    return start, end


def in_window(ts: datetime, start: datetime | None, end: datetime | None,
              tolerance_minutes: float = 0) -> bool:
    from datetime import timedelta
    tol = timedelta(minutes=tolerance_minutes)
    if start and ts < start - tol:
        return False
    if end and ts > end + tol:
        return False
    return True


__all__ = [
    "ROOT", "registry", "skill_entry",
    "load_context",
    "Finding", "Solution", "NextStep", "LogRequest",
    "CrossValidationAnalysis", "CrossValidationConsensus", "EvidenceChainStep", "CrossValidation",
    "EvidenceState", "EvidenceGap", "CollectionPlan",
    "SkillResult", "fail",
    "parse_time", "time_range", "in_window",
]
