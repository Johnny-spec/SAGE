#!/usr/bin/env python3
"""
edge_crash_analyzer.py — enumerate Edge Crashpad reports.

Reads %LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\Crashpad\\reports\\*.dmp,
filters by the supplied time window (default: last 24h), and attempts a very
approximate signature extraction from the first 4KB of each minidump.

For real stack walking, use WinDbg with `srv*c:\\symbols*https://msdl.microsoft.com/download/symbols`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_default_product_root = Path(__file__).resolve().parents[4] / "Edge"
_product_root = Path(os.environ.get("COMMON_SKILL_PRODUCT_ROOT", str(_default_product_root))).resolve()
sys.path.insert(0, str(_product_root))
from _shared.contract import (  # noqa: E402
    Finding, SkillResult, load_context, time_range,
)
from _shared import playbook  # noqa: E402
from _shared.sources import crashpad as crashpad_src, user_data  # noqa: E402
from investigation_protocol import EvidenceGap, EvidenceState  # type: ignore[reportMissingImports]  # noqa: E402

SKILL_ID = "edge_crash"

# Back-compat re-exports (tests / other skills may import these symbols)
SIGNATURE_HINTS = crashpad_src._SIGNATURE_HINTS
_signature = crashpad_src.extract_signature
_default_user_data_dir = user_data.default_user_data_dir
_resolve_user_data_dir = user_data.resolve_user_data_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Edge Crashpad analyzer")
    ap.add_argument("context", nargs="?", default=None)
    args = ap.parse_args(argv)

    ctx = load_context([args.context] if args.context else [])
    udd = _resolve_user_data_dir(ctx)

    if udd is None:
        no_udd = SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(
                summary="No Edge User Data directory found.",
                severity="info",
            )],
            confidence="low",
            recommendations=["Confirm Edge is installed and has been launched at least once."],
            evidence_state=EvidenceState.MISSING_REQUIRED_EVIDENCE,
            evidence_gaps=[EvidenceGap(
                code="edge_user_data_missing",
                summary="Edge User Data is required to inspect Crashpad reports.",
                blocking=True,
                log_kind="crashpad",
                skill=SKILL_ID,
            )],
            raw={"quality": {
                "sources_present": False,
                "capture_complete": False,
                "sample_sufficient": False,
                "blocking_gaps": True,
            }},
        )
        playbook.merge_into_result(no_udd, ["crash"])
        no_udd.emit()
        return 0

    crashpad = udd / "Crashpad" / "reports"
    if not crashpad.exists():
        no_dir = SkillResult(
            skill=SKILL_ID, ok=True,
            findings=[Finding(
                summary="The Edge Crashpad reports directory is missing.",
                severity="warning",
                evidence={"user_data_dir": str(udd)},
            )],
            confidence="low",
            recommendations=[
                "If you expect crashes, check whether MetricsReportingEnabled is disabled by policy.",
                "Reproduce the crash; Edge writes a .dmp file each time a process crashes.",
            ],
            evidence_state=EvidenceState.MISSING_REQUIRED_EVIDENCE,
            evidence_gaps=[EvidenceGap(
                code="crashpad_reports_missing",
                summary="Crashpad reports are required to test the crash hypothesis.",
                blocking=True,
                log_kind="crashpad",
                skill=SKILL_ID,
            )],
            raw={
                "user_data_dir": str(udd),
                "quality": {
                    "sources_present": False,
                    "capture_complete": False,
                    "sample_sufficient": False,
                    "blocking_gaps": True,
                },
            },
        )
        playbook.merge_into_result(no_dir, ["crash"])
        no_dir.emit()
        return 0

    start, end = time_range(ctx)
    if start is None:
        start = datetime.now() - timedelta(hours=24)
    if end is None:
        end = datetime.now()

    query = crashpad_src.query(
        sources=[crashpad],
        time_range=(start, end),
    )
    all_dmps = list(query.get("entries") or [])
    dmps = [
        {
            "file": item.get("file"),
            "size_bytes": item.get("size_bytes"),
            "mtime": item.get("timestamp"),
            "minutes_ago": item.get("minutes_ago"),
            "signature": item.get("signature") or "unknown",
        }
        for item in all_dmps[:50]
    ]
    sigs = Counter(
        str(item.get("signature") or "unknown") for item in all_dmps
    )

    findings: list[Finding] = []
    confidence = "low"
    candidate_cause: str | None = None

    if not dmps and sigs.total() == 0:
        findings.append(Finding(
            summary=f"No Edge crashes in window {start.isoformat()} → {end.isoformat()}.",
            severity="info",
            evidence={"reports_dir": str(crashpad)},
        ))
        confidence = "low"
    else:
        top, top_n = sigs.most_common(1)[0]
        findings.append(Finding(
            summary=f"{sigs.total()} Edge crash report(s) in window; top signature: {top} ({top_n})",
            severity="critical" if sigs.total() >= 3 else "warning",
            evidence={"by_signature": dict(sigs.most_common())},
        ))
        if top != "unknown" and top_n / max(sigs.total(), 1) >= 0.5:
            candidate_cause = f"Repeated crash reports contain module hint: {top}"
            confidence = "medium" if sigs.total() >= 3 else "low"
            findings.append(Finding(
                summary=(
                    "The repeated module hint is a crash hypothesis, not a stack-walk "
                    "or faulting-thread root cause."
                ),
                severity="warning",
                evidence={
                    "candidate_signature": top,
                    "sample_count": top_n,
                    "falsifier": "A symbolized stack identifies a different faulting module/thread.",
                },
            ))

    recommendations: list[str] = []
    if dmps:
        recommendations.extend([
            "Open edge://crashes to see the crash list with upload state.",
            "If a single signature dominates, search learn.microsoft.com / Edge release notes for it.",
            "For a true stack walk, open the .dmp in WinDbg with Microsoft public symbols.",
        ])
        if any(d["signature"].endswith("(GPU)") or "graphics" in d["signature"].lower() for d in dmps):
            recommendations.append("Try launching Edge with `--disable-gpu` to confirm a GPU-driver root cause.")

    result = SkillResult(
        skill=SKILL_ID, ok=True,
        findings=findings, root_cause=None, confidence=confidence,
        recommendations=recommendations,
        raw={
            "user_data_dir": str(udd),
            "reports_dir": str(crashpad),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "total_reports_in_window": sigs.total(),
            "by_signature": dict(sigs.most_common()),
            "candidate_cause": candidate_cause,
            "reports": dmps,
            "quality": {
                **(query.get("quality") or {}),
                "direct_join": False,
                "independent_sources": 1,
                "sample_sufficient": sigs.total() >= 1,
                "time_comparable": True,
                "blocking_gaps": False,
            },
        },
    )
    # Always relevant problem_type for this skill
    pts = ["crash"]
    if any("graphics" in d["signature"].lower() or "GPU" in d["signature"] for d in dmps):
        pts.append("render_process_gone")
    playbook.merge_into_result(result, pts)
    result.emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
