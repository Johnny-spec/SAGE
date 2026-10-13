#!/usr/bin/env python3
"""Root AI manager for IIS and Microsoft Edge diagnostic skills."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
COMMON_ROOT = ROOT / "Common"
if str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))

from investigation_protocol import (  # type: ignore[reportMissingImports]  # noqa: E402
    EvidenceState, aggregate_root_quality, build_collection_plan,
)
from redaction import redact_result  # type: ignore[reportMissingImports]  # noqa: E402
from result_contract import (  # type: ignore[reportMissingImports]  # noqa: E402
    ResultContractError, enforce_root_quality, normalize_result,
)

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
from manager_report import (  # noqa: E402
    build_manager_report, validate_manager_report, write_manager_html,
    write_manager_reports,
)


SKILL_ID = "orchestrator"
MANAGER_REPORT_VERSION = "1.0"
DEFAULT_PRODUCT_TIMEOUT = 240
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_FINDINGS_PER_PRODUCT = 20
MAX_SCAN_DIRECTORIES = 2_000
MAX_SCAN_FILES = 10_000
MAX_SCAN_HEAD_BYTES = 1024 * 1024
PRODUCTS = ("IIS", "Edge")


@dataclass
class ManagerInput:
    mode: str
    payload: dict[str, Any] = field(default_factory=dict)
    folder: Path | None = None
    label: str = "input"


@dataclass
class RoutingDecision:
    products: list[str]
    signals: dict[str, list[str]] = field(default_factory=dict)
    inventory: dict[str, Any] = field(default_factory=dict)
    ambiguous: bool = False
    reason: str | None = None


@dataclass
class ProductRun:
    product: str
    result: dict[str, Any]
    invocation_nodes: list[dict[str, Any]] = field(default_factory=list)
    manager_gaps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ManagerOptions:
    product_timeout: int = DEFAULT_PRODUCT_TIMEOUT
    validator_timeout: int = 90
    allow_live_host: bool = False
    procmon_exe: str | None = None
    procmon_time_basis: str | None = None
    skip_cross_validation: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_text(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("manager context must be a JSON object")
    return parsed


def load_manager_input(argument: str | None) -> ManagerInput:
    if argument:
        if argument.startswith("@"):
            path = Path(argument[1:])
            return ManagerInput(
                mode="context", payload=_load_json_text(path.read_text(encoding="utf-8")),
                label=path.name,
            )
        path = Path(argument)
        if path.exists() and path.is_dir():
            return ManagerInput(mode="folder", folder=path.resolve(), label=path.name)
        if path.exists() and path.is_file() and path.suffix.casefold() == ".json":
            return ManagerInput(
                mode="context", payload=_load_json_text(path.read_text(encoding="utf-8")),
                label=path.name,
            )
        try:
            return ManagerInput(mode="context", payload=_load_json_text(argument))
        except json.JSONDecodeError:
            return ManagerInput(
                mode="symptom",
                payload={"question": argument, "extra": {"symptoms": argument}},
                label="symptom",
            )
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            return ManagerInput(mode="context", payload=_load_json_text(raw), label="stdin")
    return ManagerInput(mode="context", payload={}, label="empty")


def _canonical_product(value: str) -> str | None:
    normalized = value.strip().casefold()
    if normalized == "iis":
        return "IIS"
    if normalized in {"edge", "microsoft edge", "msedge"}:
        return "Edge"
    return None


def _payload_text(payload: dict[str, Any]) -> str:
    fields: list[str] = []
    for key in ("question", "summary", "symptom", "title", "problem_type"):
        value = payload.get(key)
        if isinstance(value, str):
            fields.append(value)
    extra = payload.get("extra") or {}
    if isinstance(extra, dict) and isinstance(extra.get("symptoms"), str):
        fields.append(extra["symptoms"])
    for problem in payload.get("problems") or []:
        if isinstance(problem, dict):
            fields.extend(str(problem.get(key) or "") for key in ("type", "summary"))
    return " ".join(fields).casefold()


def _read_head(path: Path, limit: int = MAX_SCAN_HEAD_BYTES) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="replace").casefold()
    except OSError:
        return ""


def inventory_folder(folder: Path) -> dict[str, Any]:
    """Bounded, non-symlink-following product evidence inventory."""
    kinds: dict[str, list[str]] = {}
    directories_scanned = 0
    files_scanned = 0
    truncated = False

    def add(kind: str, path: Path) -> None:
        values = kinds.setdefault(kind, [])
        if len(values) < 200:
            values.append(str(path))

    for current, directory_names, file_names in os.walk(folder, followlinks=False):
        directories_scanned += 1
        if directories_scanned > MAX_SCAN_DIRECTORIES:
            truncated = True
            break
        current_path = Path(current)
        directory_names[:] = sorted(
            (name for name in directory_names if not (current_path / name).is_symlink()),
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            files_scanned += 1
            if files_scanned > MAX_SCAN_FILES:
                truncated = True
                break
            path = current_path / name
            lower_name = name.casefold()
            suffix = path.suffix.casefold()
            path_text = str(path).casefold()
            if lower_name in {"preferences", "secure preferences", "local state"}:
                add("edge_profile", path)
                continue
            if suffix == ".log":
                head = _read_head(path, 128 * 1024)
                if lower_name.startswith("httperr") or "#software: microsoft http api" in head:
                    add("iis_httperr", path)
                elif "#software:" in head and "ftp" in head:
                    add("iis_ftp", path)
                elif "#software:" in head and (
                    "internet information services" in head or "microsoft iis" in head
                ):
                    add("iis_w3c", path)
                continue
            if suffix == ".json":
                head = _read_head(path)
                if '"constants"' in head[:1024] and (
                    '"logsourcetype"' in head or '"logeventtypes"' in head
                ):
                    add("edge_netlog", path)
                continue
            if suffix == ".csv":
                head = _read_head(path, 256 * 1024)
                if "process name" in head and "operation" in head and "result" in head:
                    if "msedge.exe" in head or "microsoftedge" in head:
                        add("procmon_edge", path)
                    elif "w3wp.exe" in head or "iisexpress.exe" in head:
                        add("procmon_iis", path)
                    else:
                        add("procmon_generic", path)
                continue
            if suffix == ".pml":
                add("procmon_generic", path)
                continue
            if suffix == ".dmp":
                add(
                    "edge_dump" if any(token in path_text for token in ("edge", "msedge", "crashpad"))
                    else "generic_dump",
                    path,
                )
                continue
            if suffix == ".evtx":
                add(
                    "edge_evtx" if any(token in path_text for token in ("edge", "msedge", "chrome"))
                    else "generic_evtx",
                    path,
                )
        if truncated:
            break
    return {
        "root": str(folder),
        "kinds": kinds,
        "directories_scanned": min(directories_scanned, MAX_SCAN_DIRECTORIES),
        "files_scanned": min(files_scanned, MAX_SCAN_FILES),
        "truncated": truncated,
        "limits": {
            "max_directories": MAX_SCAN_DIRECTORIES,
            "max_files": MAX_SCAN_FILES,
            "max_head_bytes": MAX_SCAN_HEAD_BYTES,
        },
    }


def _problem_type_products(payload: dict[str, Any]) -> dict[str, list[str]]:
    observed = set()
    if payload.get("problem_type"):
        observed.add(str(payload["problem_type"]))
    for item in payload.get("problems") or []:
        if isinstance(item, dict) and item.get("type"):
            observed.add(str(item["type"]))
    result: dict[str, list[str]] = {"IIS": [], "Edge": []}
    for product in PRODUCTS:
        registry_path = ROOT / product / "_shared" / "registry.json"
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        known = set((registry.get("problem_types") or {}).keys())
        result[product] = sorted(observed & known)
    return result


def _observed_problem_types(payload: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    if payload.get("problem_type"):
        values.add(str(payload["problem_type"]))
    for item in payload.get("problems") or []:
        if isinstance(item, dict) and item.get("type"):
            values.add(str(item["type"]))
    return sorted(values)


def _prepare_iis_payload(payload: dict[str, Any], *, allow_live_host: bool) -> dict[str, Any]:
    prepared = dict(payload)
    if prepared.get("skills_to_trigger"):
        return prepared
    problem_types = _observed_problem_types(prepared)
    try:
        registry = json.loads(
            (ROOT / "IIS" / "_shared" / "registry.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return prepared
    extra_value = prepared.get("extra")
    extra: dict[str, Any] = extra_value if isinstance(extra_value, dict) else {}
    extra_kind_map = registry.get("extra_keys_to_kind") or {}
    provided_kinds = {
        str(kind) for key, kind in extra_kind_map.items() if extra.get(key)
    }
    follow_ups: list[str] = []
    for problem_type in problem_types:
        definition = (registry.get("problem_types") or {}).get(problem_type) or {}
        for skill in definition.get("follow_ups") or []:
            if skill == "validator" or skill in follow_ups:
                continue
            metadata = (registry.get("skills") or {}).get(skill) or {}
            if metadata.get("folder_dispatch") == "live_only" and not allow_live_host:
                continue
            required = set(metadata.get("folder_evidence_kinds") or [])
            if required and not allow_live_host and not (required & provided_kinds):
                continue
            follow_ups.append(skill)
    for skill, kind in (("netlog", "netlog"), ("procmon", "procmon"), ("event_log", "evtx")):
        if kind in provided_kinds and skill not in follow_ups:
            follow_ups.append(skill)
    prepared["skills_to_trigger"] = follow_ups
    if problem_types and not prepared.get("problems"):
        prepared["problems"] = [{"type": value} for value in problem_types]
    if prepared.get("time_range") and not prepared.get("metrics"):
        prepared["metrics"] = {"time_range": prepared["time_range"]}
    return prepared


def _prepare_edge_payload(payload: dict[str, Any], *, allow_live_host: bool) -> dict[str, Any]:
    prepared = dict(payload)
    if prepared.get("skills_to_trigger"):
        return prepared
    problem_types = _observed_problem_types(prepared)
    try:
        registry = json.loads(
            (ROOT / "Edge" / "_shared" / "registry.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return prepared
    extra_value = prepared.get("extra")
    extra: dict[str, Any] = extra_value if isinstance(extra_value, dict) else {}
    extra_kind_map = registry.get("extra_keys_to_kind") or {}
    provided_kinds = {
        str(kind) for key, kind in extra_kind_map.items() if extra.get(key)
    }
    skills = registry.get("skills") or {}
    source_registry = registry.get("source_registry") or {}
    follow_ups: list[str] = []

    def add_if_available(skill: str) -> None:
        if skill == "validator" or skill in follow_ups:
            return
        metadata = skills.get(skill) or {}
        role = str(metadata.get("role") or "") if isinstance(metadata, dict) else ""
        required = {
            str(value) for value in (
                list(metadata.get("consumes_log_kind") or [])
                + list(source_registry.get(skill) or [])
            )
        } if isinstance(metadata, dict) else set()
        if (
            "knowledge_base" in role
            or not required
            or allow_live_host
            or bool(required & provided_kinds)
        ):
            follow_ups.append(skill)

    for problem_type in problem_types:
        definition = (registry.get("problem_types") or {}).get(problem_type) or {}
        for skill in definition.get("follow_ups") or []:
            add_if_available(str(skill))
    for skill, required_kinds in source_registry.items():
        if set(str(value) for value in required_kinds or []) & provided_kinds:
            add_if_available(str(skill))

    prepared["skills_to_trigger"] = follow_ups
    if problem_types and not prepared.get("problems"):
        prepared["problems"] = [{"type": value} for value in problem_types]
    return prepared


def _filter_requested_skills(
    product: str, payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prepared = dict(payload)
    requested = [str(value) for value in prepared.get("skills_to_trigger") or []]
    if not requested:
        return prepared, []
    try:
        registry = json.loads(
            (ROOT / product / "_shared" / "registry.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        registry = {}
    skills = registry.get("skills") or {}
    allowed: list[str] = []
    rejected: list[str] = []
    for skill in requested:
        metadata = skills.get(skill)
        role = str((metadata or {}).get("role") or "") if isinstance(metadata, dict) else ""
        safe_role = "follow_up" in role or "knowledge_base" in role
        if (
            not safe_role
            or skill in {"orchestrator", "edge_diagnostics", "iis_logs", "ftp_logs"}
        ):
            rejected.append(skill)
            continue
        if skill not in allowed:
            allowed.append(skill)
    prepared["skills_to_trigger"] = allowed
    gaps = [{
        "code": "manager_rejected_skill_dispatch",
        "summary": f"Manager rejected unsafe or non-follow-up skill dispatch: {skill}",
        "severity": "warning", "blocking": False,
        "details": {"product": product, "skill": skill},
    } for skill in sorted(set(rejected))]
    return prepared, gaps


def _product_playbook_advisories(
    products: Iterable[str], payload: dict[str, Any], analyzed_products: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    solutions: list[dict[str, Any]] = []
    next_steps: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    problem_types = _observed_problem_types(payload)
    for product in products:
        try:
            playbook = json.loads(
                (ROOT / product / "_shared" / "playbook.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        definitions = playbook.get("problem_types") or {}
        for problem_type in problem_types:
            definition = definitions.get(problem_type)
            if not isinstance(definition, dict):
                continue
            for solution in definition.get("solutions") or []:
                if isinstance(solution, dict):
                    solutions.append({
                        **solution, "problem_ref": solution.get("problem_ref") or problem_type,
                        "_source_product": product,
                    })
            for step in definition.get("next_steps") or []:
                if isinstance(step, dict):
                    next_steps.append({**step, "_source_product": product})
            if product not in analyzed_products:
                for request in definition.get("additional_logs_needed") or []:
                    if isinstance(request, dict):
                        logs.append({**request, "_source_product": product})
    return solutions, next_steps, logs


def detect_products(manager_input: ManagerInput, explicit: str = "auto") -> RoutingDecision:
    if explicit.casefold() != "auto":
        requested = PRODUCTS if explicit.casefold() == "both" else (_canonical_product(explicit),)
        products = [product for product in requested if product]
        return RoutingDecision(products=products, signals={"explicit": products})

    payload = manager_input.payload
    declared: list[str] = []
    raw_products = payload.get("products") or payload.get("product")
    values = raw_products if isinstance(raw_products, list) else [raw_products]
    for value in values:
        if isinstance(value, str):
            product = _canonical_product(value)
            if product and product not in declared:
                declared.append(product)
    if declared:
        return RoutingDecision(products=declared, signals={"payload": declared})

    signals: dict[str, list[str]] = {"IIS": [], "Edge": []}
    inventory: dict[str, Any] = {}
    if manager_input.mode == "folder" and manager_input.folder:
        inventory = inventory_folder(manager_input.folder)
        kinds = inventory.get("kinds") or {}
        if any(kinds.get(kind) for kind in ("iis_w3c", "iis_ftp", "iis_httperr", "procmon_iis")):
            signals["IIS"].append("folder evidence")
        if any(kinds.get(kind) for kind in ("edge_netlog", "edge_profile", "edge_dump", "edge_evtx", "procmon_edge")):
            signals["Edge"].append("folder evidence")
        generic_procmon = bool(kinds.get("procmon_generic"))
        if generic_procmon and len([product for product in PRODUCTS if signals[product]]) == 1:
            selected = next(product for product in PRODUCTS if signals[product])
            signals[selected].append("generic Procmon evidence co-located with product evidence")
    skills = [str(value) for value in payload.get("skills_to_trigger") or []]
    if payload.get("skill") == "edge_diagnostics" or any(value.startswith("edge_") for value in skills):
        signals["Edge"].append("edge skill identity")
    iis_skill_ids = {
        "iis_logs", "ftp_logs", "httperror", "event_log", "app_crash",
        "security_audit", "resource_monitor", "firewall", "netlog", "procmon",
    }
    if any(value in iis_skill_ids for value in skills):
        signals["IIS"].append("IIS skill identity")

    problem_products = _problem_type_products(payload)
    for product, problem_types in problem_products.items():
        if problem_types and not any(problem_type in problem_products[other] for other in PRODUCTS if other != product for problem_type in problem_types):
            signals[product].append("product-specific problem type")

    text = _payload_text(payload)
    if re.search(r"\b(edge|msedge|chromium|browser|renderer|extension|tab)\b", text):
        signals["Edge"].append("symptom text")
    if re.search(r"\b(iis|w3wp|http\.sys|httperr|app(?:lication)? pool|asp\.net|ftp)\b", text):
        signals["IIS"].append("symptom text")

    products = [product for product in PRODUCTS if signals[product]]
    if products:
        return RoutingDecision(products=products, signals=signals, inventory=inventory)
    return RoutingDecision(
        products=[], signals=signals, inventory=inventory, ambiguous=True,
        reason="Product scope is ambiguous; specify --product IIS, Edge, or both.",
    )


def _failure_result(skill: str, message: str, code: str, *, stage: str) -> dict[str, Any]:
    return {
        "schema_version": "2.4",
        "skill": skill,
        "ok": False,
        "findings": [],
        "root_cause": None,
        "confidence": "low",
        "recommendations": [],
        "solutions": [],
        "next_steps": [],
        "additional_logs_needed": [],
        "cross_validation": None,
        "evidence_state": EvidenceState.TOOL_UNAVAILABLE,
        "evidence_gaps": [{
            "code": code, "summary": message, "severity": "warning",
            "blocking": True, "details": {"stage": stage},
        }],
        "collection_plan": None,
        "raw": {
            "failure": {"code": code},
            "execution": {"stage": stage},
            "quality": {
                "direct_join": False, "independent_sources": 0,
                "sample_sufficient": False, "capture_complete": False,
                "time_comparable": False, "blocking_gaps": True,
            },
        },
        "error": message,
        "generated_at": _now(),
    }


def _command_with_context(entry: Path, payload: dict[str, Any]) -> tuple[list[str], Path | None]:
    encoded = json.dumps(payload, ensure_ascii=True)
    temporary: Path | None = None
    argument = encoded
    if len(encoded) > 7000:
        stream = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False,
        )
        with stream:
            stream.write(encoded)
        temporary = Path(stream.name)
        argument = f"@{temporary}"
    return [sys.executable, str(entry), argument], temporary


def execute_delegate(
    *,
    product: str,
    role: str,
    command: list[str],
    expected_skill: str,
    timeout: int,
    temporary_context: Path | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = _now()
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                **(environment_overrides or {}),
            },
        )
        stdout = process.stdout or ""
        stderr = process.stderr or ""
        if len(stdout.encode("utf-8", errors="replace")) > MAX_STDOUT_BYTES:
            result = _failure_result(
                expected_skill, "Delegate output exceeded the manager budget.",
                "delegate_output_too_large", stage="execute",
            )
        else:
            try:
                result = normalize_result(json.loads(stdout), expected_skill=expected_skill)
            except (json.JSONDecodeError, ResultContractError) as exc:
                result = _failure_result(
                    expected_skill, f"Delegate returned an invalid envelope: {type(exc).__name__}",
                    "delegate_contract_failure", stage="validate",
                )
        execution = result.setdefault("raw", {}).setdefault("execution", {})
        execution.update({"runtime": "python", "exit_code": process.returncode})
        if stderr:
            execution.update({
                "stderr_character_count": len(stderr),
                "stderr_sha256": hashlib.sha256(
                    stderr.encode("utf-8", errors="replace")
                ).hexdigest(),
            })
        if process.returncode != 0 and result.get("ok"):
            result = _failure_result(
                expected_skill,
                f"Delegate exited with {process.returncode} despite ok=true.",
                "delegate_exit_mismatch", stage="execute",
            )
    except subprocess.TimeoutExpired:
        result = _failure_result(
            expected_skill, f"Delegate timed out after {timeout} seconds.",
            "delegate_timeout", stage="execute",
        )
    except OSError as exc:
        result = _failure_result(
            expected_skill, f"Delegate could not start: {exc}",
            "delegate_launch_failure", stage="launch",
        )
    finally:
        if temporary_context is not None:
            temporary_context.unlink(missing_ok=True)

    execution = (result.get("raw") or {}).get("execution") or {}
    node = {
        "id": f"INV-{product.upper()}-{role.upper()}",
        "parent_id": "INV-ROOT",
        "product": product,
        "skill": expected_skill,
        "role": role,
        "runtime": execution.get("runtime", "python"),
        "status": "completed" if result.get("ok") else "failed",
        "exit_code": execution.get("exit_code"),
        "execution_stage": execution.get("stage", "execute"),
        "started_at": started,
        "completed_at": _now(),
        "stderr": {
            "character_count": execution.get("stderr_character_count", 0),
            "sha256": execution.get("stderr_sha256"),
        },
    }
    return result, node


def _edge_payload_for_folder(folder: Path, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    inventory = inventory or inventory_folder(folder)
    kinds = inventory.get("kinds") or {}
    netlogs = list(kinds.get("edge_netlog") or [])
    procmon = list(kinds.get("procmon_edge") or []) + list(kinds.get("procmon_generic") or [])
    crashpad = list(kinds.get("edge_dump") or [])
    skills: list[str] = []
    extra: dict[str, Any] = {"incident_mode": "offline_bundle"}
    if netlogs:
        skills.append("edge_netlog")
        extra["netlog_paths"] = netlogs
    if procmon:
        skills.append("edge_procmon")
        extra["procmon_paths"] = procmon
    if crashpad:
        skills.append("edge_crash")
        extra["crashpad_path"] = str(folder)
    return {"skills_to_trigger": skills, "problems": [], "extra": extra}


def run_product(
    product: str, manager_input: ManagerInput, options: ManagerOptions,
) -> ProductRun:
    nodes: list[dict[str, Any]] = []
    manager_gaps: list[dict[str, Any]] = []
    trusted_environment: dict[str, str] = {}
    if options.procmon_exe:
        trusted_environment["SKILLS_PROCMON_EXE"] = str(
            Path(options.procmon_exe).resolve()
        )
    if options.procmon_time_basis:
        trusted_environment["SKILLS_PROCMON_TIME_BASIS"] = options.procmon_time_basis
    if product == "IIS":
        entry = ROOT / "IIS" / "orchestrator" / "scripts" / "skill_orchestrator.py"
        if manager_input.mode == "folder" and manager_input.folder:
            command = [sys.executable, str(entry), str(manager_input.folder)]
            if options.allow_live_host:
                command.append("--allow-live-host")
            if options.procmon_exe:
                command.extend(["--procmon-exe", options.procmon_exe])
            if options.procmon_time_basis:
                command.extend(["--procmon-time-basis", options.procmon_time_basis])
            temporary = None
        else:
            payload = _prepare_iis_payload(
                manager_input.payload, allow_live_host=options.allow_live_host,
            )
            payload, manager_gaps = _filter_requested_skills(product, payload)
            command, temporary = _command_with_context(entry, payload)
        result, node = execute_delegate(
            product=product, role="product_orchestrator", command=command,
            expected_skill="orchestrator", timeout=options.product_timeout,
            temporary_context=temporary,
            environment_overrides=trusted_environment,
        )
        nodes.append(node)
        return ProductRun(
            product=product, result=result, invocation_nodes=nodes,
            manager_gaps=manager_gaps,
        )

    orchestrator_entry = ROOT / "Edge" / "orchestrator" / "scripts" / "edge_orchestrator.py"
    payload = (
        _edge_payload_for_folder(manager_input.folder)
        if manager_input.mode == "folder" and manager_input.folder
        else _prepare_edge_payload(
            manager_input.payload, allow_live_host=options.allow_live_host,
        )
    )
    payload, manager_gaps = _filter_requested_skills(product, payload)
    if manager_input.mode == "folder" and not payload.get("skills_to_trigger"):
        result = _failure_result(
            "orchestrator",
            "Edge evidence was discovered, but no registered folder consumer can safely analyze it.",
            "edge_folder_evidence_unmapped", stage="dispatch",
        )
        result["evidence_state"] = EvidenceState.MISSING_REQUIRED_EVIDENCE
        result["additional_logs_needed"] = [{
            "log_kind": "edge_context",
            "why": "Profile/EVTX evidence needs an explicit Edge scenario or supported consumer mapping.",
            "how_to_collect": "Provide the Edge symptom/problem type and profile or dump context.",
            "skill": "edge_diagnostics",
        }]
        node = {
            "id": "INV-EDGE-PRODUCT-ROUTER", "parent_id": "INV-ROOT",
            "product": "Edge", "skill": "orchestrator", "role": "product_router",
            "runtime": "python", "status": "skipped", "exit_code": None,
            "execution_stage": "dispatch", "started_at": _now(), "completed_at": _now(),
            "stderr": {"character_count": 0, "sha256": None},
        }
        return ProductRun(
            product=product, result=result, invocation_nodes=[node],
            manager_gaps=manager_gaps,
        )
    if not payload.get("skills_to_trigger") and manager_input.mode != "folder":
        diagnostics_entry = ROOT / "Edge" / "edge_diagnostics" / "scripts" / "edge_diagnostics.py"
        diagnostics_command, temporary = _command_with_context(diagnostics_entry, payload)
        diagnostics, node = execute_delegate(
            product=product, role="entry", command=diagnostics_command,
            expected_skill="edge_diagnostics", timeout=options.product_timeout,
            temporary_context=temporary,
            environment_overrides=trusted_environment,
        )
        nodes.append(node)
        if not diagnostics.get("ok"):
            return ProductRun(
                product=product, result=diagnostics, invocation_nodes=nodes,
                manager_gaps=manager_gaps,
            )
        payload = dict(diagnostics.get("raw") or {})
        payload, diagnostic_gaps = _filter_requested_skills(product, payload)
        manager_gaps.extend(diagnostic_gaps)
        if not payload.get("skills_to_trigger"):
            return ProductRun(
                product=product, result=diagnostics, invocation_nodes=nodes,
                manager_gaps=manager_gaps,
            )

    command, temporary = _command_with_context(orchestrator_entry, payload)
    command.extend([
        "--per-skill-timeout", str(options.product_timeout),
        "--total-timeout", str(options.product_timeout),
    ])
    result, node = execute_delegate(
        product=product, role="product_orchestrator", command=command,
        expected_skill="orchestrator", timeout=options.product_timeout,
        temporary_context=temporary,
        environment_overrides=trusted_environment,
    )
    nodes.append(node)
    return ProductRun(
        product=product, result=result, invocation_nodes=nodes,
        manager_gaps=manager_gaps,
    )


def _evidence_children(runs: list[ProductRun]) -> dict[str, dict[str, Any]]:
    children: dict[str, dict[str, Any]] = {}
    for run in runs:
        result = run.result
        raw_value = result.get("raw")
        raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        found = False
        for field in ("results", "children"):
            values = raw.get(field)
            if not isinstance(values, dict):
                continue
            for skill, child in values.items():
                if not isinstance(child, dict) or skill in {"validator", "source_logging_discovery"}:
                    continue
                children[f"{run.product}:{skill}"] = child
                found = True
        secondary = raw.get("secondary")
        secondary_raw = secondary.get("raw") if isinstance(secondary, dict) else None
        secondary_results = secondary_raw.get("results") if isinstance(secondary_raw, dict) else None
        if isinstance(secondary_results, dict):
            for skill, child in secondary_results.items():
                if isinstance(child, dict) and skill not in {"validator", "source_logging_discovery"}:
                    children[f"{run.product}:secondary:{skill}"] = child
                    found = True
        if not found:
            children[f"{run.product}:orchestrator"] = result
    return children


def _source_group(result: dict[str, Any], fallback: str) -> str:
    sources: set[str] = set()
    for finding in result.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        provenance = finding.get("provenance")
        source = provenance.get("source") if isinstance(provenance, dict) else None
        value = source.get("value") if isinstance(source, dict) else None
        if value not in (None, "", [], {}):
            sources.add(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str))
    raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    for key in (
        "source", "sources", "source_files", "evtx_paths", "netlog_paths",
        "procmon_paths", "log_files", "files",
    ):
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, "", [], {}):
            sources.add(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str))
    if not sources:
        return "SRCGROUP-UNKNOWN"
    digest = hashlib.sha256("\n".join(sorted(sources)).encode("utf-8")).hexdigest()[:20]
    return f"SRCGROUP-{digest}"


def _product_validation_round(runs: list[ProductRun]) -> dict[str, Any]:
    statuses: dict[str, str] = {}
    for run in runs:
        raw_value = run.result.get("raw")
        raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        validation = raw.get("validation")
        cross_validation = (
            validation.get("cross_validation") if isinstance(validation, dict)
            else run.result.get("cross_validation")
        )
        status = (
            cross_validation.get("status") if isinstance(cross_validation, dict)
            else "not_run"
        )
        statuses[run.product] = str(status or "not_run")
    return {
        "round": 1,
        "name": "product_local_analysis_and_validation",
        "status": "completed" if runs else "skipped",
        "evidence_sources": len(_evidence_children(runs)),
        "products": statuses,
        "notes": "Product skills parse evidence and product orchestrators apply their local validator/gates.",
    }


def run_manager_validation(
    runs: list[ProductRun], manager_input: ManagerInput, timeout: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    children = _evidence_children(runs)
    if not children:
        return None, None, {
            "round": 2, "name": "manager_cross_product_reconciliation",
            "status": "skipped", "evidence_sources": 0,
            "notes": "No evidence-producing child results were available.",
        }
    evidence_source_groups = {
        skill: _source_group(result, skill)
        for skill, result in children.items()
    }
    independent_source_count = len(set(evidence_source_groups.values()))
    validator = ROOT / "Common" / "log_skills" / "skills" / "validator" / "validator.py"
    problems = [
        item for item in manager_input.payload.get("problems") or []
        if isinstance(item, dict)
    ]
    context = {
        "problem_type": (
            manager_input.payload.get("problem_type")
            or (problems[0].get("type") if problems else None)
        ),
        "problems": problems,
        "time_range": manager_input.payload.get("time_range") or {},
        "extra": {
            "product": "SkillsCollection",
            "children": children,
            "evidence_source_groups": evidence_source_groups,
            "incident_mode": "offline_bundle" if manager_input.mode == "folder" else "live_host",
            "minimum_independent_skills": 2,
        },
    }
    command, temporary = _command_with_context(validator, context)
    result, node = execute_delegate(
        product="Manager", role="validator", command=command,
        expected_skill="validator", timeout=timeout, temporary_context=temporary,
    )
    cross_validation = result.get("cross_validation")
    status = (
        cross_validation.get("status") if isinstance(cross_validation, dict)
        else "failed" if not result.get("ok") else "completed"
    )
    return result, node, {
        "round": 2,
        "name": "manager_cross_product_reconciliation",
        "status": str(status),
        "evidence_sources": independent_source_count,
        "notes": "Common validator reconciles namespaced product evidence and independent provider claims.",
    }


def _invocation_id(product: str, skill: str, suffix: str = "") -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "-", f"{product}-{skill}-{suffix}".upper()).strip("-")
    return f"INV-{normalized}"


def _product_child_invocations(run: ProductRun) -> list[dict[str, Any]]:
    result = run.result
    raw_value = result.get("raw")
    raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
    parent_id = (
        run.invocation_nodes[-1]["id"] if run.invocation_nodes else _invocation_id(run.product, "orchestrator")
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(skill: str, child: Any, *, role: str = "follow_up", suffix: str = "") -> None:
        if not isinstance(child, dict):
            return
        node_id = _invocation_id(run.product, skill, suffix)
        if node_id in seen:
            return
        seen.add(node_id)
        child_raw = child.get("raw") if isinstance(child.get("raw"), dict) else {}
        execution = child_raw.get("execution") if isinstance(child_raw, dict) else {}
        execution = execution if isinstance(execution, dict) else {}
        failure = child_raw.get("failure") if isinstance(child_raw, dict) else {}
        failure_code = str((failure or {}).get("code") or "") if isinstance(failure, dict) else ""
        status = (
            "timeout" if "timeout" in failure_code
            else "completed" if child.get("ok")
            else "failed"
        )
        output.append({
            "id": node_id, "parent_id": parent_id, "product": run.product,
            "skill": str(child.get("skill") or skill), "role": role,
            "runtime": execution.get("runtime", "unknown"), "status": status,
            "exit_code": execution.get("exit_code"),
            "timeout_seconds": execution.get("timeout_seconds"),
            "execution_stage": execution.get("stage", "execute"),
            "started_at": None, "completed_at": None,
            "input_summary": {},
            "output_summary": {
                "ok": child.get("ok"), "evidence_state": child.get("evidence_state"),
                "finding_count": len(child.get("findings") or []),
            },
            "stderr": {
                "character_count": execution.get("stderr_character_count", 0),
                "sha256": execution.get("stderr_sha256"),
            },
        })

    for field in ("results", "children"):
        children = raw.get(field)
        if isinstance(children, dict):
            for skill, child in children.items():
                add(str(skill), child)
    secondary = raw.get("secondary")
    secondary_raw = secondary.get("raw") if isinstance(secondary, dict) else None
    secondary_results = secondary_raw.get("results") if isinstance(secondary_raw, dict) else None
    if isinstance(secondary_results, dict):
        for skill, child in secondary_results.items():
            add(str(skill), child, suffix="secondary")
    validation = raw.get("validation")
    if isinstance(validation, dict):
        add("validator", validation, role="validator")
    for skipped in raw.get("skipped_followups") or []:
        if not isinstance(skipped, dict) or not skipped.get("skill"):
            continue
        skill = str(skipped["skill"])
        node_id = _invocation_id(run.product, skill, "skipped")
        if node_id in seen:
            continue
        seen.add(node_id)
        output.append({
            "id": node_id, "parent_id": parent_id, "product": run.product,
            "skill": skill, "role": "follow_up", "runtime": "unknown",
            "status": "skipped", "exit_code": None, "timeout_seconds": None,
            "execution_stage": "dispatch", "started_at": None, "completed_at": None,
            "input_summary": {"required_kinds": skipped.get("required_kinds") or []},
            "output_summary": {"reason": skipped.get("reason")},
            "stderr": {"character_count": 0, "sha256": None},
        })
    return output


def _canonical_claim(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _manager_validation_supports_root(
    root_cause: str, manager_validation: dict[str, Any] | None,
) -> tuple[bool, str | None, list[str]]:
    cross = (
        manager_validation.get("cross_validation")
        if isinstance(manager_validation, dict) else None
    )
    if not isinstance(cross, dict):
        return False, None, []
    status = str(cross.get("status") or "")
    consensus = cross.get("consensus")
    agreed = consensus.get("agreed_claims") if isinstance(consensus, dict) else []
    conclusions = [
        str(item.get("conclusion") or "") for item in agreed or []
        if isinstance(item, dict) and item.get("conclusion")
    ]
    target = _canonical_claim(root_cause)
    aligned = bool(target) and any(_canonical_claim(value) == target for value in conclusions)
    return status == "completed" and aligned, status or None, conclusions


def _dedupe_objects(items: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        identity = tuple(str(item.get(key) or "") for key in keys)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(dict(item))
    return output


def _build_manager_collection_plan(
    requests: list[dict[str, Any]],
    *,
    mode: str,
    products: list[str],
) -> dict[str, Any] | None:
    if not requests:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        product = str(request.get("_source_product") or "Manager")
        grouped.setdefault(product, []).append(request)
    merged: dict[str, Any] | None = None
    for product in sorted(grouped):
        plan = asdict(build_collection_plan(
            grouped[product], mode=mode, products=[product],
        ))
        for item in plan.get("requests") or []:
            item["id"] = re.sub(
                r"[^a-z0-9]+", "-", f"{product}-{item.get('id', 'request')}".casefold()
            ).strip("-")
            item["product"] = product
        if merged is None:
            merged = plan
            merged["incident_scope"]["products"] = sorted(set(products))
        else:
            merged["requests"].extend(plan.get("requests") or [])
    return merged


def aggregate_product_results(
    manager_input: ManagerInput,
    decision: RoutingDecision,
    runs: list[ProductRun],
    *,
    manager_validation: dict[str, Any] | None = None,
    validation_rounds: list[dict[str, Any]] | None = None,
    manager_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    solutions: list[dict[str, Any]] = []
    next_steps: list[dict[str, Any]] = []
    log_requests: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, Any]] = []
    recommendations: list[str] = []
    products: dict[str, dict[str, Any]] = {}
    invocation_nodes = [{
        "id": "INV-ROOT", "parent_id": None, "product": "Manager",
        "skill": SKILL_ID, "role": "ai_manager", "runtime": "python",
        "status": "completed", "exit_code": 0, "execution_stage": "aggregate",
        "started_at": None, "completed_at": _now(),
        "stderr": {"character_count": 0, "sha256": None},
    }]
    analyzed_products: set[str] = set()
    for run in runs:
        result = run.result
        products[run.product] = result
        raw_value = result.get("raw")
        raw_result: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        if (
            raw_result.get("executed")
            or raw_result.get("children")
            or result.get("skill") not in {"orchestrator", "edge_diagnostics"}
        ):
            analyzed_products.add(run.product)
        invocation_nodes.extend(run.invocation_nodes)
        invocation_nodes.extend(_product_child_invocations(run))
        evidence_gaps.extend(run.manager_gaps)
        for finding in (result.get("findings") or [])[:MAX_FINDINGS_PER_PRODUCT]:
            evidence = dict(finding.get("evidence") or {})
            evidence.update({
                "source_product": run.product,
                "source_skill": result.get("skill"),
                "child_evidence_id": finding.get("evidence_id"),
            })
            findings.append({
                "summary": f"[{run.product}] {finding.get('summary', '')}",
                "severity": finding.get("severity", "info"),
                "evidence": evidence,
            })
        solutions.extend({**item, "_source_product": run.product}
                         for item in result.get("solutions") or []
                         if isinstance(item, dict))
        next_steps.extend({**item, "_source_product": run.product}
                          for item in result.get("next_steps") or []
                          if isinstance(item, dict))
        log_requests.extend({**item, "_source_product": run.product}
                            for item in result.get("additional_logs_needed") or []
                            if isinstance(item, dict))
        evidence_gaps.extend({
            **item,
            "details": {
                **(item.get("details") or {}),
                "product": (item.get("details") or {}).get("product") or run.product,
            },
        } for item in result.get("evidence_gaps") or [] if isinstance(item, dict))
        recommendations.extend(str(value) for value in result.get("recommendations") or [])
        if not result.get("ok"):
            evidence_gaps.append({
                "code": "product_delegate_failure",
                "summary": f"{run.product} delegate failed: {result.get('error') or 'unknown error'}",
                "severity": "warning", "blocking": True,
                "details": {"product": run.product},
            })

    playbook_solutions, playbook_steps, playbook_logs = _product_playbook_advisories(
        decision.products, manager_input.payload, analyzed_products,
    )
    solutions.extend(playbook_solutions)
    next_steps.extend(playbook_steps)
    log_requests.extend(playbook_logs)
    existing_gap_kinds = {
        (
            str((gap.get("details") or {}).get("product") or ""),
            str(gap.get("log_kind") or (gap.get("details") or {}).get("log_kind") or ""),
        )
        for gap in evidence_gaps if isinstance(gap, dict)
    }
    for request in playbook_logs:
        kind = str(request.get("log_kind") or "")
        product = str(request.get("_source_product") or "")
        if kind and (product, kind) not in existing_gap_kinds:
            evidence_gaps.append({
                "code": "manager_missing_log",
                "summary": f"Required {request.get('_source_product')} log is missing: {kind}",
                "severity": "warning", "blocking": True,
                "log_kind": kind, "skill": request.get("skill"),
                "details": {"product": product, "log_kind": kind},
            })

    if manager_validation:
        for finding in manager_validation.get("findings") or []:
            findings.append({
                "summary": f"[Manager validator] {finding.get('summary', '')}",
                "severity": finding.get("severity", "info"),
                "evidence": {
                    **(finding.get("evidence") or {}),
                    "source_product": "Manager",
                    "source_skill": "validator",
                    "child_evidence_id": finding.get("evidence_id"),
                },
            })
        evidence_gaps.extend(manager_validation.get("evidence_gaps") or [])
        recommendations.extend(
            str(value) for value in manager_validation.get("recommendations") or []
        )
        log_requests.extend({**item, "_source_product": "Manager"}
                    for item in manager_validation.get("additional_logs_needed") or []
                    if isinstance(item, dict))
    invocation_nodes.extend(manager_nodes or [])
    invocation_nodes.append({
        "id": "INV-MANAGER-REPORT", "parent_id": "INV-ROOT",
        "product": "Manager", "skill": "manager_report", "role": "report",
        "runtime": "python", "status": "completed", "exit_code": 0,
        "execution_stage": "report", "started_at": None, "completed_at": _now(),
        "input_summary": {"products": decision.products},
        "output_summary": {"schema_version": MANAGER_REPORT_VERSION},
        "stderr": {"character_count": 0, "sha256": None},
    })

    product_proven = [
        run.result for run in runs
        if run.result.get("root_cause")
        and run.result.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND
    ]
    proposed_product_root = str(product_proven[0].get("root_cause") or "") if len(product_proven) == 1 else ""
    manager_supports_root, manager_validation_status, agreed_conclusions = (
        _manager_validation_supports_root(proposed_product_root, manager_validation)
        if proposed_product_root else (False, None, [])
    )
    if product_proven and not manager_supports_root:
        evidence_gaps.append({
            "code": "manager_validation_incomplete",
            "summary": (
                "A product proposed a root cause, but manager cross-validation "
                "did not complete with an evidence-grounded matching claim."
            ),
            "severity": "warning", "blocking": True,
            "details": {
                "manager_validation_status": manager_validation_status or "not_run",
                "proposed_root_cause": proposed_product_root,
                "agreed_conclusions": agreed_conclusions,
                "claim_aligned": False,
            },
        })

    quality_inputs = [run.result for run in runs]
    if manager_validation:
        quality_inputs.append(manager_validation)
    quality = aggregate_root_quality(quality_inputs)
    proven = [
        result for result in quality_inputs
        if result.get("root_cause")
        and result.get("evidence_state") == EvidenceState.ROOT_CAUSE_FOUND
    ]
    root_cause = proven[0].get("root_cause") if len(proven) == 1 else None
    confidence = proven[0].get("confidence", "low") if root_cause else (
        "medium" if findings else "low"
    )
    blocking = any(
        bool(gap.get("blocking", True)) for gap in evidence_gaps
        if isinstance(gap, dict)
    ) or not runs
    quality["blocking_gaps"] = bool(quality.get("blocking_gaps")) or blocking
    if root_cause and not quality.get("blocking_gaps"):
        evidence_state = EvidenceState.ROOT_CAUSE_FOUND
    elif log_requests:
        evidence_state = EvidenceState.COLLECTION_PLAN_READY
    elif runs:
        evidence_state = EvidenceState.INCONCLUSIVE_EVIDENCE
    else:
        evidence_state = EvidenceState.MISSING_REQUIRED_EVIDENCE

    report_id = "MR-" + hashlib.sha256(
        json.dumps({
            "mode": manager_input.mode, "products": decision.products,
            "label": manager_input.label,
        }, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    envelope = {
        "schema_version": "2.4",
        "skill": SKILL_ID,
        "ok": bool(runs) and any(run.result.get("ok") for run in runs),
        "findings": findings or [{
            "summary": decision.reason or "No product analysis was executed.",
            "severity": "warning" if decision.ambiguous else "info",
            "evidence": {"routing_signals": decision.signals},
        }],
        "root_cause": root_cause,
        "confidence": confidence,
        "recommendations": list(dict.fromkeys(recommendations))[:50],
        "solutions": _dedupe_objects(
            solutions, ("_source_product", "problem_ref", "title")
        ),
        "next_steps": _dedupe_objects(
            next_steps, ("_source_product", "action", "skill")
        ),
        "additional_logs_needed": _dedupe_objects(
            log_requests, ("_source_product", "log_kind", "skill")
        ),
        "cross_validation": (
            manager_validation.get("cross_validation") if manager_validation else None
        ),
        "evidence_state": evidence_state,
        "evidence_gaps": evidence_gaps,
        "collection_plan": _build_manager_collection_plan(
            _dedupe_objects(log_requests, ("_source_product", "log_kind", "skill")),
            mode="offline_bundle" if manager_input.mode == "folder" else "live_host",
            products=decision.products,
        ),
        "raw": {
            "routing": {
                "products": decision.products, "signals": decision.signals,
                "inventory": decision.inventory,
                "ambiguous": decision.ambiguous, "reason": decision.reason,
            },
            "products": products,
            "products_run": [run.product for run in runs],
            "product_failures": [run.product for run in runs if not run.result.get("ok")],
            "manager_validation": manager_validation,
            "manager_report": {},
            "quality": quality,
        },
        "error": decision.reason if not runs else None,
        "generated_at": _now(),
    }
    normalized = normalize_result(envelope, expected_skill=SKILL_ID)
    normalized = enforce_root_quality(normalized)
    rounds = list(validation_rounds or [])
    if not any(item.get("round") == 1 for item in rounds):
        rounds.append({
            "round": 1, "name": "product_local_analysis_and_validation",
            "status": "skipped", "evidence_sources": 0,
            "notes": "No product delegate was executed.",
        })
    if not any(item.get("round") == 2 for item in rounds):
        rounds.append({
            "round": 2, "name": "manager_cross_product_reconciliation",
            "status": "skipped", "evidence_sources": 0,
            "notes": "No manager validation input was available.",
        })
    rounds.sort(key=lambda item: int(item.get("round", 0)))
    consistency_round = {
        "round": 3,
        "name": "deterministic_reference_and_root_quality_check",
        "status": "pending",
        "evidence_sources": len(_evidence_children(runs)),
        "notes": "Manager verifies evidence references, blocking gaps, and semantic root-cause proof.",
    }
    rounds.append(consistency_round)
    normalized["raw"]["manager_report"] = build_manager_report(
        report_id=report_id,
        products=products,
        status={
            "ok": normalized.get("ok"),
            "evidence_state": normalized.get("evidence_state"),
            "confidence": normalized.get("confidence"),
            "root_cause": normalized.get("root_cause"),
            "root_cause_refs": [],
        },
        routing=normalized["raw"]["routing"],
        input_summary={
            "mode": manager_input.mode,
            "label": manager_input.label,
            "description": next((
                str(manager_input.payload.get(key)).strip()
                for key in ("question", "summary", "symptom", "title")
                if isinstance(manager_input.payload.get(key), str)
                and str(manager_input.payload.get(key)).strip()
            ), str((manager_input.payload.get("extra") or {}).get("symptoms") or "").strip()),
            "time_range": manager_input.payload.get("time_range") or {},
            "problem_types": sorted({
                *(
                    [str(manager_input.payload.get("problem_type"))]
                    if manager_input.payload.get("problem_type") else []
                ),
                *[
                    str(item.get("type"))
                    for item in manager_input.payload.get("problems") or []
                    if isinstance(item, dict) and item.get("type")
                ],
            }),
            "reported_problems": [
                {
                    "type": item.get("type"),
                    "severity": item.get("severity"),
                    "summary": item.get("summary") or item.get("description"),
                }
                for item in manager_input.payload.get("problems") or []
                if isinstance(item, dict)
            ],
        },
        invocation_nodes=invocation_nodes,
        validation_rounds=rounds,
        root_quality=quality,
        evidence_gaps=evidence_gaps,
        collection_plan=normalized.get("collection_plan"),
        next_steps=normalized.get("next_steps") or [],
        manager_solutions=normalized.get("solutions") or [],
        manager_log_requests=normalized.get("additional_logs_needed") or [],
        manager_validation=manager_validation,
    )
    report_quality = normalized["raw"]["manager_report"]["quality"]
    integrity = report_quality["reference_integrity"]
    cause_integrity = report_quality["cause_integrity"]
    report_errors = validate_manager_report(normalized["raw"]["manager_report"])
    report_quality["schema_validation"] = {
        "valid": not report_errors, "errors": report_errors,
    }
    consistency_round["status"] = (
        "completed"
        if integrity.get("valid") and cause_integrity.get("valid") and not report_errors
        else "failed"
    )
    for report_round in normalized["raw"]["manager_report"]["cross_validation"]["rounds"]:
        if report_round.get("round") == 3:
            report_round["status"] = consistency_round["status"]
            report_round["confidence"] = (
                "high" if consistency_round["status"] == "completed" else "low"
            )
    if normalized.get("root_cause") and consistency_round["status"] != "completed":
        normalized["evidence_gaps"].append({
            "code": "manager_cause_integrity_failed",
            "summary": "A proposed root cause lacks complete evidence references or falsifiers.",
            "severity": "warning", "blocking": True,
            "details": {
                "reference_integrity": integrity,
                "cause_integrity": cause_integrity,
                "report_errors": report_errors,
            },
        })
        normalized["raw"]["quality"]["blocking_gaps"] = True
        normalized["evidence_state"] = EvidenceState.INCONCLUSIVE_EVIDENCE
        normalized = enforce_root_quality(normalized)
        normalized["raw"]["manager_report"]["status"].update({
            "evidence_state": normalized["evidence_state"],
            "confidence": normalized["confidence"],
            "root_cause": normalized["root_cause"],
        })
    return normalized


def orchestrate_manager(
    manager_input: ManagerInput,
    *,
    explicit_product: str = "auto",
    options: ManagerOptions | None = None,
) -> dict[str, Any]:
    decision = detect_products(manager_input, explicit_product)
    options = options or ManagerOptions()
    if decision.ambiguous or not decision.products:
        return aggregate_product_results(manager_input, decision, [])
    runs = [run_product(product, manager_input, options) for product in decision.products]
    rounds = [_product_validation_round(runs)]
    manager_validation: dict[str, Any] | None = None
    manager_node: dict[str, Any] | None = None
    if options.skip_cross_validation:
        rounds.append({
            "round": 2, "name": "manager_cross_product_reconciliation",
            "status": "skipped", "evidence_sources": len(_evidence_children(runs)),
            "notes": "Manager cross-validation was explicitly disabled by the operator.",
        })
    else:
        manager_validation, manager_node, round_two = run_manager_validation(
            runs, manager_input, options.validator_timeout,
        )
        rounds.append(round_two)
    return aggregate_product_results(
        manager_input, decision, runs,
        manager_validation=manager_validation,
        validation_rounds=rounds,
        manager_nodes=[manager_node] if manager_node else [],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SkillsCollection IIS/Edge AI manager")
    parser.add_argument("context", nargs="?", default=None,
                        help="Incident folder, JSON, @JSON file, symptom text, or stdin")
    parser.add_argument("--product", default="auto", choices=("auto", "IIS", "Edge", "both"))
    parser.add_argument("--product-timeout", type=int, default=DEFAULT_PRODUCT_TIMEOUT)
    parser.add_argument("--validator-timeout", type=int, default=90)
    parser.add_argument("--skip-cross-validation", action="store_true")
    parser.add_argument("--allow-live-host", action="store_true")
    parser.add_argument("--procmon-exe", default=None)
    parser.add_argument("--procmon-time-basis", choices=("unknown", "context"), default=None)
    parser.add_argument("--report", default=None,
                    help=("Write self-contained English and Chinese HTML reports. "
                        "The requested path is English; a .zh-CN.html sibling is also written."))
    args = parser.parse_args(argv)

    manager_input = load_manager_input(args.context)
    result = orchestrate_manager(
        manager_input,
        explicit_product=args.product,
        options=ManagerOptions(
            product_timeout=max(1, args.product_timeout),
            validator_timeout=max(1, args.validator_timeout),
            allow_live_host=args.allow_live_host,
            procmon_exe=args.procmon_exe,
            procmon_time_basis=args.procmon_time_basis,
            skip_cross_validation=args.skip_cross_validation,
        ),
    )
    if args.report:
        safe_report = redact_result({
            "raw": {"manager_report": result["raw"]["manager_report"]}
        })["raw"]["manager_report"]
        report_paths = write_manager_reports(Path(args.report), safe_report)
        result["raw"]["manager_report_html"] = str(
            report_paths["en-US"].resolve()
        )
        result["raw"]["manager_report_html_paths"] = {
            locale: str(path.resolve()) for locale, path in report_paths.items()
        }
    print(json.dumps(redact_result(result), indent=2, ensure_ascii=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())