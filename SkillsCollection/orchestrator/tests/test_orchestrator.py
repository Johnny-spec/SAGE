from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _product_result(product: str) -> dict:
    return {
        "schema_version": "2.4",
        "skill": "orchestrator",
        "ok": True,
        "findings": [{
            "summary": f"{product} evidence analyzed", "severity": "info",
            "evidence": {
                "product": product, "problem_type": "demo",
                "falsifier": "A healthy replay still fails",
            },
            "evidence_id": f"EV-{product}",
        }],
        "root_cause": None,
        "confidence": "low",
        "recommendations": [],
        "solutions": [{
            "title": "Apply targeted correction", "problem_ref": "demo",
            "severity": "warning", "steps": ["Change the proven setting"],
            "references": [],
        }],
        "next_steps": [],
        "additional_logs_needed": [{
            "log_kind": "evtx", "why": "Need independent event evidence",
            "how_to_collect": "Export the incident window", "skill": "event_log",
        }],
        "cross_validation": None,
        "evidence_state": "INCONCLUSIVE_EVIDENCE",
        "evidence_gaps": [],
        "collection_plan": None,
        "raw": {"quality": {
            "direct_join": False, "independent_sources": 1,
            "sample_sufficient": True, "capture_complete": True,
            "time_comparable": True, "blocking_gaps": False,
        }},
        "error": None,
    }


def test_explicit_iis_routes_only_iis(manager_module, monkeypatch):
    calls: list[str] = []

    def fake_run(product, manager_input, options):
        calls.append(product)
        return manager_module.ProductRun(product, _product_result(product), [])

    monkeypatch.setattr(manager_module, "run_product", fake_run)
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="symptom", payload={"question": "fails"}),
        explicit_product="IIS",
    )

    assert calls == ["IIS"]
    assert result["raw"]["products_run"] == ["IIS"]
    assert set(result["raw"]["products"]) == {"IIS"}


def test_explicit_edge_routes_only_edge(manager_module, monkeypatch):
    calls: list[str] = []

    def fake_run(product, manager_input, options):
        calls.append(product)
        return manager_module.ProductRun(product, _product_result(product), [])

    monkeypatch.setattr(manager_module, "run_product", fake_run)
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="symptom", payload={"question": "fails"}),
        explicit_product="Edge",
    )

    assert calls == ["Edge"]
    assert result["raw"]["products_run"] == ["Edge"]


def test_ambiguous_scope_is_structured_and_does_not_delegate(manager_module, monkeypatch):
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not delegate")),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={}),
    )

    assert result["ok"] is False
    assert result["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert result["raw"]["routing"]["ambiguous"] is True
    assert "--product" in result["error"]
    rounds = result["raw"]["manager_report"]["cross_validation"]["rounds"]
    assert [item["round"] for item in rounds] == [1, 2, 3]


def test_manager_report_reserves_and_populates_invocation_chain(manager_module, monkeypatch):
    node = {
        "id": "INV-IIS", "parent_id": "INV-ROOT", "product": "IIS",
        "skill": "orchestrator", "role": "product_orchestrator",
        "runtime": "python", "status": "completed", "exit_code": 0,
        "execution_stage": "execute", "started_at": None, "completed_at": None,
        "stderr": {"character_count": 0, "sha256": None},
    }
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda product, *_: manager_module.ProductRun(product, _product_result(product), [node]),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
    )
    report = result["raw"]["manager_report"]

    assert report["schema_version"] == "1.0"
    assert report["invocation_chain"]["status"] == "populated"
    assert [item["id"] for item in report["invocation_chain"]["nodes"]] == [
        "INV-ROOT", "INV-IIS", "INV-MANAGER-VALIDATOR", "INV-MANAGER-REPORT",
    ]
    rounds = report["cross_validation"]["rounds"]
    assert [item["round"] for item in rounds] == [1, 2, 3]
    assert rounds[0]["name"] == "product_local_analysis_and_validation"
    assert rounds[1]["name"] == "manager_cross_product_reconciliation"
    assert rounds[2]["status"] == "completed"


def test_invocation_chain_extracts_product_children_and_skips(manager_module, monkeypatch):
    product = _product_result("IIS")
    product["raw"].update({
        "results": {
            "event_log": {
                **_product_result("IIS"),
                "skill": "event_log",
                "raw": {
                    "quality": _product_result("IIS")["raw"]["quality"],
                    "execution": {
                        "runtime": "python", "exit_code": 0,
                        "stderr_character_count": 12, "stderr_sha256": "a" * 64,
                    },
                },
            }
        },
        "skipped_followups": [{
            "skill": "resource_monitor", "reason": "offline evidence absent",
            "required_kinds": ["perf_counter"],
        }],
    })
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, [{
            "id": "INV-IIS-PRODUCT", "parent_id": "INV-ROOT", "product": "IIS",
            "skill": "orchestrator", "role": "product_orchestrator",
            "runtime": "python", "status": "completed", "exit_code": 0,
            "execution_stage": "execute", "started_at": None, "completed_at": None,
            "stderr": {"character_count": 0, "sha256": None},
        }]),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    nodes = result["raw"]["manager_report"]["invocation_chain"]["nodes"]
    by_skill = {item["skill"]: item for item in nodes}

    assert by_skill["event_log"]["parent_id"] == "INV-IIS-PRODUCT"
    assert by_skill["event_log"]["stderr"]["sha256"] == "a" * 64
    assert by_skill["resource_monitor"]["status"] == "skipped"
    assert by_skill["manager_report"]["role"] == "report"


def test_manager_report_links_solutions_and_evidence(manager_module, monkeypatch):
    product = _product_result("IIS")
    product["raw"]["candidate_cause"] = "A configuration mismatch is suspected"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
    )
    report = result["raw"]["manager_report"]
    evidence_ids = {item["id"] for item in report["evidence_index"]}

    assert report["possible_causes"][0]["status"] == "candidate"
    assert report["solutions"][0]["evidence_relation"] == "direct_problem_match"
    assert set(report["solutions"][0]["evidence_refs"]) <= evidence_ids
    assert report["quality"]["reference_integrity"]["valid"] is True


def test_manager_report_deduplicates_product_wrapper_findings(manager_module, monkeypatch):
    product = _product_result("Edge")
    evidence = {
        "type": "dc_udp_response_gap", "process_name": "lsass.exe",
        "send_count": 101, "receive_count": 1,
        "signal_classification": "supportive", "falsifier": "packet capture succeeds",
    }
    product["findings"] = [{
        "summary": "[edge_procmon] Possible domain-controller UDP response gap",
        "severity": "warning", "evidence_id": "EV-WRAPPER",
        "evidence": {**evidence, "_from": "edge_procmon"},
    }]
    child = _product_result("Edge")
    child["skill"] = "edge_procmon"
    child["findings"] = [{
        "summary": "Possible domain-controller UDP response gap",
        "severity": "warning", "evidence_id": "EV-LEAF",
        "evidence": evidence,
    }]
    product["raw"]["children"] = {"edge_procmon": child}
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "Edge"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    matches = [
        item for item in result["raw"]["manager_report"]["evidence_index"]
        if "domain-controller UDP response gap" in item["summary"]
    ]

    assert len(matches) == 1
    assert matches[0]["id"] == "EV-LEAF"
    assert matches[0]["source_ref"] == "Edge/edge_procmon"


def test_manager_report_synthesizes_netlog_and_procmon_auth_evidence(
    manager_module, monkeypatch,
):
    product = _product_result("Edge")
    product["findings"] = []
    netlog = _product_result("Edge")
    netlog["skill"] = "edge_netlog"
    netlog["findings"] = [{
        "summary": (
            "Negotiate failed with SEC_E_DOWNGRADE_DETECTED 6 time(s) for SPN "
            "'HTTP/pws.example' on origin http://pws.example"
        ),
        "severity": "warning", "evidence_id": "EV-NETLOG-AUTH",
        "evidence": {
            "observed_problem_type": "auth_error", "failure_count": 6,
            "failed_spn": "HTTP/pws.example", "origin": "http://pws.example",
            "signal_classification": "supportive",
            "falsifier": "Fresh authentication succeeds while the symptom remains",
        },
    }]
    netlog["raw"]["candidate_cause"] = netlog["findings"][0]["summary"]
    procmon = _product_result("Edge")
    procmon["skill"] = "edge_procmon"
    procmon["findings"] = [{
        "summary": "Possible domain-controller UDP response gap",
        "severity": "warning", "evidence_id": "EV-PROCMON-DC",
        "evidence": {
            "type": "dc_udp_response_gap", "process_name": "lsass.exe",
            "send_count": 101, "receive_count": 1,
            "distinct_remote_endpoints": 101, "service_port": 389,
            "signal_classification": "supportive",
            "falsifier": "Packet capture shows timely DC responses",
        },
    }]
    procmon["raw"]["candidate_cause"] = procmon["findings"][0]["summary"]
    product["raw"]["children"] = {
        "edge_netlog": netlog, "edge_procmon": procmon,
    }
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "Edge"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    cause = result["raw"]["manager_report"]["possible_causes"][0]

    assert cause["label"] == (
        "Domain-controller response-path failure during Windows authentication"
    )
    assert "6 SSPI failure(s)" in cause["summary"]
    assert "101 request(s)" in cause["summary"]
    assert set(cause["evidence_refs"]) == {"EV-NETLOG-AUTH", "EV-PROCMON-DC"}
    assert cause["status"] == "supported"
    assert len(result["raw"]["manager_report"]["possible_causes"]) == 1
    supporting_ids = {
        item["id"]
        for item in result["raw"]["manager_report"]
        ["engineering_assessment"]["supporting_evidence"]
    }
    assert supporting_ids == {"EV-NETLOG-AUTH", "EV-PROCMON-DC"}
    chain = result["raw"]["manager_report"]["evidence_chain"]
    assert [item["type"] for item in chain] == [
        "analyzed", "analyzed", "synthesized_hypothesis",
    ]
    assessment = result["raw"]["manager_report"]["engineering_assessment"]
    assert "Windows 身份验证过程中出现疑似域控制器响应路径异常" in assessment["summary_zh"]
    assert ".." not in assessment["summary_en"]


def test_manager_html_contains_required_sections_and_escapes(manager_module, monkeypatch):
    product = _product_result("Edge")
    product["findings"][0]["summary"] = "unsafe <script>alert(1)</script>"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "Edge"}),
    )
    html = manager_module.write_manager_html.__globals__["render_manager_html"](
        result["raw"]["manager_report"]
    )

    for heading in (
        "Problem Description", "Troubleshooting Process", "Cause Analysis",
        "Recommended Solutions", "Next Actions and Required Logs",
        "Cross-Validation Results", "Skill Invocation Chain and Process",
        "Evidence Chain",
    ):
        assert f"<h2>{heading}</h2>" in html
    assert "<script>alert(1)</script>" not in html
    assert "unsafe &lt;script&gt;alert(1)" in html
    assert "clawpilotTheme" in html


def test_readable_report_model_contains_requested_modules(manager_module, monkeypatch):
    product = _product_result("IIS")
    product["raw"]["candidate_cause"] = "Configuration mismatch"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, [{
            "id": "INV-IIS", "parent_id": "INV-ROOT", "product": "IIS",
            "skill": "iis_logs", "role": "follow_up", "runtime": "python",
            "status": "completed", "exit_code": 0,
            "execution_stage": "execute", "started_at": None, "completed_at": None,
            "output_summary": {
                "ok": True, "evidence_state": "INCONCLUSIVE_EVIDENCE",
                "finding_count": 1,
            },
            "stderr": {"character_count": 0, "sha256": None},
        }]),
    )
    validation = _manager_validation_result("Configuration mismatch")
    validation["cross_validation"]["consensus"].update({
        "status": "conflict",
        "provider_count": 2,
        "completed_provider_count": 2,
        "independent_analysis_count": 2,
        "consensus_confidence": 0.72,
        "agreement_ratio": 0.5,
        "diversity": {"models": ["model-a", "model-b"]},
        "agreed_claims": [{
            "claim_key": "config",
            "conclusion": "Configuration mismatch",
            "analyzers": ["review-a", "review-b"],
            "evidence_refs": ["EV-IIS"],
            "common_evidence_refs": ["EV-IIS"],
            "confidence": 0.72,
        }],
        "conflicting_findings": [{
            "problem_type": "demo",
            "reason": "different cause ranking",
            "claims": [{
                "analyzer": "review-a", "claim_key": "config",
                "conclusion": "Configuration mismatch", "confidence": 0.72,
                "evidence_refs": ["EV-IIS"],
            }, {
                "analyzer": "review-b", "claim_key": "dependency",
                "conclusion": "Missing dependency", "confidence": 0.61,
                "evidence_refs": ["EV-IIS"],
            }],
        }],
        "partial_agreement": [{
            "claim_key": "scope", "conclusion": "Failure is server-side",
            "reason": "providers cited different evidence",
            "analyzers": ["review-a", "review-b"],
            "evidence_refs": ["EV-IIS"], "confidence": 0.55,
        }],
    })
    monkeypatch.setattr(
        manager_module, "run_manager_validation",
        lambda *_: (
            validation, None,
            {"round": 2, "name": "manager", "status": "conflict", "evidence_sources": 2},
        ),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={
            "product": "IIS",
            "question": "IIS returns HTTP 500 after deployment",
            "time_range": {"start": "2026-08-24T10:00:00", "end": "2026-08-24T10:10:00"},
            "problems": [{"type": "demo", "severity": "critical", "summary": "HTTP 500"}],
        }),
    )
    report = result["raw"]["manager_report"]

    assert report["problem_statement"]["description"] == "IIS returns HTTP 500 after deployment"
    assert len(report["investigation_process"]["steps"]) == 6
    assert report["possible_causes"]
    assert report["solutions"]
    assert report["next_actions"]
    assert report["cross_validation"]["similarities"][0]["claim_key"] == "config"
    assert report["cross_validation"]["differences"][0]["claims"]
    assert report["cross_validation"]["partial_agreements"][0]["claim_key"] == "scope"
    assert report["cross_validation"]["confidence"] == "medium"
    assert set(report["confidence_summary"]) == {
        "problem_statement", "engineering_assessment", "investigation_process", "possible_causes",
        "solutions", "next_actions", "cross_validation",
        "skill_invocation_chain",
    }
    iis_node = next(
        node for node in report["invocation_chain"]["nodes"]
        if node["skill"] == "iis_logs"
    )
    assert "Parsed IIS W3C" in iis_node["work_summary_en"]
    assert "解析 IIS W3C" in iis_node["work_summary_zh"]
    assert iis_node["confidence"] == "medium"

    render = manager_module.write_manager_html.__globals__["render_manager_html"]
    english = render(report, "en-US")
    chinese = render(report, "zh-CN")
    assert "Agreements" in english and "Differences" in english
    assert "相同点" in chinese and "不同点" in chinese and "部分一致" in chinese
    assert "Configuration mismatch" in english
    assert "Missing dependency" in english
    assert "Collect evtx" in english
    assert "收集 evtx" in chinese
    assert "Parsed IIS W3C" in english
    assert "解析 IIS W3C" in chinese


def test_bilingual_writer_creates_chinese_and_english_reports(
    manager_module, monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(
            selected, _product_result(selected), [],
        ),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={
            "product": "IIS", "question": "IIS returns HTTP 500",
        }),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )

    paths = manager_module.write_manager_reports(
        tmp_path / "manager.html", result["raw"]["manager_report"],
    )
    english = paths["en-US"].read_text(encoding="utf-8")
    chinese = paths["zh-CN"].read_text(encoding="utf-8")

    assert paths["en-US"].name == "manager.html"
    assert paths["zh-CN"].name == "manager.zh-CN.html"
    assert '<html lang="en">' in english
    assert '<html lang="zh-CN">' in chinese
    assert "Problem Description" in english
    assert "问题描述" in chinese
    assert "Troubleshooting Process" in english
    assert "问题排查过程" in chinese
    assert "Agreements" in english and "Differences" in english
    assert "相同点" in chinese and "不同点" in chinese
    assert "Skills 调用链与工作说明" in chinese
    assert "--cp-bg" in english and "clawpilotTheme" in english


def test_engineering_assessment_separates_support_from_background_noise(
    manager_module, monkeypatch,
):
    product = _product_result("IIS")
    product["findings"] = [{
        "summary": "w3wp access denied on web.config",
        "severity": "warning",
        "evidence_id": "EV-SUPPORT",
        "evidence": {
            "type": "access_denied", "problem_type": "5xx_error",
            "process_name": "w3wp.exe", "operation": "CreateFile",
            "result": "ACCESS DENIED", "count": 4,
            "source_file": "trace.csv", "line_number": 42,
            "timestamp": "2026-08-24T10:01:00",
            "signal_classification": "supportive",
            "symptom_relevance": "high", "noise_risk": "low",
            "relevance_reasons": ["Matches reported 500 and w3wp target"],
            "falsifier": "The same access succeeds and the 500 remains",
        },
    }, {
        "summary": "Explorer missing-path probe",
        "severity": "info",
        "evidence_id": "EV-CONTEXTUAL",
        "evidence": {
            "type": "repeated_path_probe", "process_name": "Explorer.EXE",
            "operation": "CreateFile", "result": "NAME NOT FOUND",
            "count": 417, "signal_classification": "contextual",
            "symptom_relevance": "medium", "noise_risk": "medium",
            "falsifier": "A healthy baseline has the same probe rate",
        },
    }, {
        "summary": "Updater connection refused",
        "severity": "info",
        "evidence_id": "EV-BACKGROUND",
        "evidence": {
            "type": "network_failure", "process_name": "updater.exe",
            "operation": "TCP Connect", "result": "CONNECTION REFUSED",
            "count": 200, "source_file": "trace.csv", "line_number": 84,
            "signal_classification": "background",
            "symptom_relevance": "low", "noise_risk": "high",
            "relevance_reasons": ["Outside the scoped target process set"],
        },
    }]
    product["raw"]["candidate_cause"] = "w3wp access denied on web.config"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={
            "product": "IIS", "problem_type": "5xx_error",
            "question": "IIS w3wp returns 500 after deployment",
        }),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    report = result["raw"]["manager_report"]
    assessment = report["engineering_assessment"]

    assert assessment["supporting_evidence"][0]["id"] == "EV-SUPPORT"
    assert "EV-CONTEXTUAL" not in {
        item["id"] for item in assessment["supporting_evidence"]
    }
    assert "EV-BACKGROUND" not in (
        assessment.get("leading_hypothesis") or {}
    ).get("evidence_refs", [])
    assert assessment["noise_assessment"]["background_evidence_count"] == 1
    assert assessment["noise_assessment"]["background_examples"][0]["id"] == "EV-BACKGROUND"
    assert "event volume" in assessment["summary_en"].lower() or assessment["leading_hypothesis"]
    assert assessment["counter_evidence_and_uncertainty"]

    render = manager_module.write_manager_html.__globals__["render_manager_html"]
    chinese = render(report, "zh-CN")
    assert "高级工程师综合研判" in chinese
    assert "EV-SUPPORT" in chinese and "w3wp access denied" in chinese
    assert "trace.csv" in chinese and "line_number" in chinese
    assert "ACCESS DENIED" in chinese
    assert "EV-BACKGROUND" in chinese and "Updater connection refused" in chinese
    assert "背景观察" in chinese
    assert '<details class="audit-evidence">' in chinese
    assert "背景与审计证据" in chinese
    assert "证据支持的假设" in chinese
    assert "反证条件" in chinese
    assert "事件数量不等于因果关系" in chinese
    assert "supported_hypothesis" not in chinese
    assert "Event volume is not causality" not in chinese


def test_iis_folder_evidence_routes_iis(manager_module, tmp_path: Path):
    (tmp_path / "u_ex.log").write_text(
        "#Software: Microsoft Internet Information Services 10.0\n"
        "#Fields: date time s-ip cs-method cs-uri-stem sc-status time-taken\n",
        encoding="utf-8",
    )

    decision = manager_module.detect_products(
        manager_module.ManagerInput(mode="folder", folder=tmp_path),
    )

    assert decision.products == ["IIS"]
    assert decision.inventory["kinds"]["iis_w3c"]


def test_folder_head_sniff_does_not_use_full_file_read(
    manager_module, tmp_path: Path, monkeypatch,
):
    path = tmp_path / "u_ex.log"
    path.write_text(
        "#Software: Microsoft Internet Information Services 10.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda *_: (_ for _ in ()).throw(AssertionError("full read forbidden")),
    )

    inventory = manager_module.inventory_folder(tmp_path)

    assert inventory["kinds"]["iis_w3c"]


def test_edge_netlog_folder_routes_edge(manager_module, tmp_path: Path):
    (tmp_path / "netlog.json").write_text(json.dumps({
        "constants": {"logSourceType": {}, "logEventTypes": {}},
        "events": [],
    }), encoding="utf-8")

    decision = manager_module.detect_products(
        manager_module.ManagerInput(mode="folder", folder=tmp_path),
    )

    assert decision.products == ["Edge"]
    assert decision.inventory["kinds"]["edge_netlog"]


def test_mixed_folder_routes_both_products(manager_module, tmp_path: Path):
    (tmp_path / "u_ex.log").write_text(
        "#Software: Microsoft Internet Information Services 10.0\n",
        encoding="utf-8",
    )
    (tmp_path / "netlog.json").write_text(json.dumps({
        "constants": {"logSourceType": {}, "logEventTypes": {}},
        "events": [],
    }), encoding="utf-8")

    decision = manager_module.detect_products(
        manager_module.ManagerInput(mode="folder", folder=tmp_path),
    )

    assert decision.products == ["IIS", "Edge"]


def test_generic_evtx_folder_stays_ambiguous(manager_module, tmp_path: Path):
    (tmp_path / "Application.evtx").write_bytes(b"ElfFile\x00generic")

    decision = manager_module.detect_products(
        manager_module.ManagerInput(mode="folder", folder=tmp_path),
    )

    assert decision.products == []
    assert decision.ambiguous is True
    assert decision.inventory["kinds"]["generic_evtx"]


def test_cross_product_symptom_routes_iis_and_edge(manager_module):
    decision = manager_module.detect_products(
        manager_module.ManagerInput(
            mode="symptom",
            payload={"question": "Edge browser gets an IIS app pool 500 failure"},
        ),
    )

    assert decision.products == ["IIS", "Edge"]


def test_iis_problem_routing_blocks_live_only_by_default(manager_module):
    payload = manager_module._prepare_iis_payload(
        {"problem_type": "high_latency"}, allow_live_host=False,
    )

    assert payload["skills_to_trigger"] == []
    assert payload["problems"] == [{"type": "high_latency"}]


def test_iis_problem_routing_allows_live_tools_explicitly(manager_module):
    payload = manager_module._prepare_iis_payload(
        {"problem_type": "high_latency"}, allow_live_host=True,
    )

    assert payload["skills_to_trigger"] == ["resource_monitor", "event_log"]


def test_iis_problem_routing_uses_supplied_offline_evtx(manager_module, tmp_path: Path):
    payload = manager_module._prepare_iis_payload({
        "problem_type": "high_latency",
        "extra": {"evtx_paths": [str(tmp_path / "Application.evtx")]},
    }, allow_live_host=False)

    assert payload["skills_to_trigger"] == ["event_log"]


def test_edge_auth_routing_uses_supplied_netlog_and_procmon_only_offline(
    manager_module,
):
    payload = manager_module._prepare_edge_payload({
        "problem_type": "auth_error",
        "extra": {
            "netlog_paths": ["capture.json"],
            "procmon_paths": ["trace.csv"],
        },
    }, allow_live_host=False)

    assert payload["skills_to_trigger"] == ["edge_netlog", "edge_procmon"]
    assert payload["problems"] == [{"type": "auth_error"}]


def test_edge_auth_routing_allows_policy_only_for_explicit_live_host(
    manager_module,
):
    payload = manager_module._prepare_edge_payload(
        {"problem_type": "auth_error"}, allow_live_host=True,
    )

    assert payload["skills_to_trigger"] == ["edge_netlog", "edge_policy"]


def test_root_edge_delegate_propagates_product_timeout(manager_module, monkeypatch):
    captured: list[str] = []

    def fake_execute_delegate(**kwargs):
        captured.extend(kwargs["command"])
        return _product_result("Edge"), {
            "id": "INV-EDGE", "parent_id": "INV-ROOT", "product": "Edge",
            "skill": "orchestrator", "role": "product_orchestrator",
            "runtime": "python", "status": "completed", "exit_code": 0,
            "execution_stage": "execute", "started_at": None, "completed_at": None,
            "stderr": {"character_count": 0, "sha256": None},
        }

    monkeypatch.setattr(manager_module, "execute_delegate", fake_execute_delegate)
    manager_module.run_product(
        "Edge",
        manager_module.ManagerInput(mode="context", payload={
            "problem_type": "auth_error",
            "extra": {"netlog_paths": ["capture.json"]},
        }),
        manager_module.ManagerOptions(product_timeout=345),
    )

    assert captured[-4:] == [
        "--per-skill-timeout", "345", "--total-timeout", "345",
    ]


def test_manager_playbook_adds_missing_logs_when_no_analysis_ran(manager_module, monkeypatch):
    product = _product_result("IIS")
    product["additional_logs_needed"] = []
    product["solutions"] = []
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(
            mode="context",
            payload={"product": "IIS", "problem_type": "high_latency"},
        ),
    )
    report = result["raw"]["manager_report"]

    assert "perf_counter" in {
        item["log_kind"] for item in result["additional_logs_needed"]
    }
    assert any(
        item["log_kind"] == "perf_counter" and item["status"] == "missing"
        for item in report["missing_logs"]
    )
    assert report["solutions"]


def test_delegate_invalid_json_hashes_stderr_without_copying_it(manager_module, monkeypatch):
    completed = subprocess.CompletedProcess(
        args=["python", "child.py"], returncode=1,
        stdout="not-json", stderr=r"private C:\Users\alice\incident.log",
    )
    monkeypatch.setattr(manager_module.subprocess, "run", lambda *args, **kwargs: completed)

    result, node = manager_module.execute_delegate(
        product="IIS", role="product_orchestrator",
        command=["python", "child.py"], expected_skill="orchestrator", timeout=5,
    )
    body = json.dumps(result)

    assert result["ok"] is False
    assert result["raw"]["failure"]["code"] == "delegate_contract_failure"
    assert "private" not in body and "alice" not in body
    assert node["stderr"]["character_count"] > 0
    assert len(node["stderr"]["sha256"]) == 64


def test_delegate_timeout_is_structured(manager_module, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(manager_module.subprocess, "run", timeout)
    result, node = manager_module.execute_delegate(
        product="Edge", role="product_orchestrator",
        command=["python", "child.py"], expected_skill="orchestrator", timeout=7,
    )

    assert result["evidence_state"] == "TOOL_UNAVAILABLE"
    assert result["raw"]["failure"]["code"] == "delegate_timeout"
    assert node["status"] == "failed"


def test_delegate_receives_trusted_procmon_environment(manager_module, monkeypatch):
    captured: dict = {}
    completed = subprocess.CompletedProcess(
        args=["python", "child.py"], returncode=0,
        stdout=json.dumps(_product_result("Edge")), stderr="",
    )

    def run(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return completed

    monkeypatch.setattr(manager_module.subprocess, "run", run)
    manager_module.execute_delegate(
        product="Edge", role="product_orchestrator",
        command=["python", "child.py"], expected_skill="orchestrator", timeout=5,
        environment_overrides={
            "SKILLS_PROCMON_EXE": r"C:\Tools\Procmon64.exe",
            "SKILLS_PROCMON_TIME_BASIS": "context",
        },
    )

    assert captured["SKILLS_PROCMON_EXE"] == r"C:\Tools\Procmon64.exe"
    assert captured["SKILLS_PROCMON_TIME_BASIS"] == "context"


def test_root_cli_delegates_iis_and_writes_manager_report(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "orchestrator" / "scripts" / "orchestrator.py"
    report = tmp_path / "manager.html"
    process = subprocess.run(
        [
            sys.executable, str(script),
            json.dumps({"skills_to_trigger": [], "problems": []}),
            "--product", "IIS", "--skip-cross-validation",
            "--report", str(report),
        ],
        capture_output=True, encoding="utf-8", timeout=60,
    )
    result = json.loads(process.stdout)

    assert process.returncode == 0, process.stderr
    assert result["skill"] == "orchestrator"
    assert result["raw"]["products_run"] == ["IIS"]
    assert report.is_file()
    chinese_report = tmp_path / "manager.zh-CN.html"
    assert chinese_report.is_file()
    assert result["raw"]["manager_report_html"].endswith("/manager.html")
    assert result["raw"]["manager_report_html_paths"]["en-US"].endswith(
        "/manager.html"
    )
    assert result["raw"]["manager_report_html_paths"]["zh-CN"].endswith(
        "/manager.zh-CN.html"
    )
    html = report.read_text(encoding="utf-8")
    chinese_html = chinese_report.read_text(encoding="utf-8")
    assert "<h2>Evidence Chain</h2>" in html
    assert "<h2>Skill Invocation Chain and Process</h2>" in html
    assert "<h2>问题描述</h2>" in chinese_html
    assert "<h2>Skills 调用链与工作说明</h2>" in chinese_html


def test_root_cli_delegates_edge_skill_and_preserves_call_chain(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "orchestrator" / "scripts" / "orchestrator.py"
    context = {
        "question": "How do I inspect Edge settings?",
        "problems": [{"type": "question", "severity": "info"}],
        "skills_to_trigger": ["edge_qa"],
    }
    process = subprocess.run(
        [
            sys.executable, str(script), json.dumps(context),
            "--product", "Edge", "--skip-cross-validation",
        ],
        capture_output=True, encoding="utf-8", timeout=60,
    )
    result = json.loads(process.stdout)
    nodes = result["raw"]["manager_report"]["invocation_chain"]["nodes"]

    assert process.returncode == 0, process.stderr
    assert result["raw"]["products_run"] == ["Edge"]
    assert "edge_qa" in {node["skill"] for node in nodes}


def test_manager_rejects_recursive_entry_and_unknown_skills(manager_module):
    payload, gaps = manager_module._filter_requested_skills("Edge", {
        "skills_to_trigger": [
            "orchestrator", "edge_diagnostics", "edge_qa", "missing_skill",
        ]
    })

    assert payload["skills_to_trigger"] == ["edge_qa"]
    assert {gap["details"]["skill"] for gap in gaps} == {
        "orchestrator", "edge_diagnostics", "missing_skill",
    }


def test_proven_product_root_is_demoted_when_manager_validation_skipped(
    manager_module, monkeypatch,
):
    product = _product_result("IIS")
    product.update({
        "root_cause": "Direct product proof",
        "confidence": "high",
        "evidence_state": "ROOT_CAUSE_FOUND",
    })
    product["raw"]["quality"]["direct_join"] = True
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )

    assert result["root_cause"] is None
    assert result["raw"]["candidate_cause"] == "Direct product proof"
    assert any(
        gap["code"] == "manager_validation_incomplete" and gap["blocking"]
        for gap in result["evidence_gaps"]
    )


def _manager_validation_result(conclusion: str) -> dict:
    return {
        "skill": "validator", "ok": True, "findings": [], "root_cause": None,
        "confidence": "low", "recommendations": [], "solutions": [],
        "next_steps": [], "additional_logs_needed": [], "evidence_gaps": [],
        "collection_plan": None, "error": None,
        "evidence_state": "INCONCLUSIVE_EVIDENCE",
        "cross_validation": {
            "status": "completed",
            "consensus": {"agreed_claims": [{"conclusion": conclusion}]},
        },
        "raw": {"quality": {
            "direct_join": False, "independent_sources": 2,
            "sample_sufficient": True, "capture_complete": False,
            "time_comparable": True, "blocking_gaps": False,
        }},
    }


def _install_proven_product_and_validation(
    manager_module, monkeypatch, conclusion: str,
):
    product = _product_result("IIS")
    product.update({
        "root_cause": "Direct product proof",
        "confidence": "high",
        "evidence_state": "ROOT_CAUSE_FOUND",
    })
    product["raw"]["quality"]["direct_join"] = True
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )
    monkeypatch.setattr(
        manager_module, "run_manager_validation",
        lambda *_: (
            _manager_validation_result(conclusion), None,
            {"round": 2, "name": "manager", "status": "completed", "evidence_sources": 2},
        ),
    )


def test_completed_manager_validation_with_different_claim_blocks_root(
    manager_module, monkeypatch,
):
    _install_proven_product_and_validation(
        manager_module, monkeypatch, "A different root cause",
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
    )

    assert result["root_cause"] is None
    gap = next(
        item for item in result["evidence_gaps"]
        if item["code"] == "manager_validation_incomplete"
    )
    assert gap["details"]["claim_aligned"] is False


def test_completed_manager_validation_with_matching_claim_allows_proven_root(
    manager_module, monkeypatch,
):
    _install_proven_product_and_validation(
        manager_module, monkeypatch, "Direct product proof",
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
    )

    assert result["root_cause"] == "Direct product proof"
    assert result["evidence_state"] == "ROOT_CAUSE_FOUND"


def test_manager_validator_finding_is_indexed_in_evidence_chain(manager_module, monkeypatch):
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(
            selected, _product_result(selected), [],
        ),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
    )
    evidence = result["raw"]["manager_report"]["evidence_index"]

    assert any(item["source_ref"] == "Manager/validator" for item in evidence)


def test_evidence_free_candidate_is_marked_unsupported(manager_module, monkeypatch):
    product = _product_result("IIS")
    product["findings"] = []
    product["raw"]["candidate_cause"] = "Unsupported candidate"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )

    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    report = result["raw"]["manager_report"]

    assert report["possible_causes"][0]["status"] == "unsupported"
    assert report["quality"]["cause_integrity"]["valid"] is False
    assert report["cross_validation"]["rounds"][2]["status"] == "failed"


def test_direct_html_writer_enforces_redaction(manager_module, monkeypatch, tmp_path: Path):
    product = _product_result("IIS")
    product["raw"]["candidate_cause"] = r"failed C:\Users\alice\private\secret.log"
    monkeypatch.setattr(
        manager_module, "run_product",
        lambda selected, *_: manager_module.ProductRun(selected, product, []),
    )
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"product": "IIS"}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    path = manager_module.write_manager_html(
        tmp_path / "manager.html", result["raw"]["manager_report"],
    )
    html = path.read_text(encoding="utf-8")

    assert "alice" not in html and r"C:\Users" not in html
    assert "secret.log" in html


def test_edge_profile_only_folder_returns_explicit_missing_evidence(
    manager_module, tmp_path: Path,
):
    (tmp_path / "Preferences").write_text("{}", encoding="utf-8")
    manager_input = manager_module.ManagerInput(mode="folder", folder=tmp_path)
    decision = manager_module.detect_products(manager_input)

    run = manager_module.run_product("Edge", manager_input, manager_module.ManagerOptions())

    assert decision.products == ["Edge"]
    assert run.result["ok"] is False
    assert run.result["evidence_state"] == "MISSING_REQUIRED_EVIDENCE"
    assert run.result["raw"]["failure"]["code"] == "edge_folder_evidence_unmapped"


def test_manager_report_keeps_root_source_inventory_when_delegate_fails(
    manager_module, tmp_path: Path,
):
    (tmp_path / "Preferences").write_text("{}", encoding="utf-8")
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="folder", folder=tmp_path),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )
    sources = result["raw"]["manager_report"]["source_inventory"]["sources"]

    assert any(
        item["product"] == "Edge" and item["kind"] == "edge_profile"
        and item["count"] == 1
        for item in sources
    )


def test_collection_plan_preserves_same_log_request_for_both_products(manager_module):
    requests = [{
        "_source_product": product,
        "log_kind": "evtx",
        "why": f"Need {product} events",
        "skill": "event_log",
    } for product in ("IIS", "Edge")]

    plan = manager_module._build_manager_collection_plan(
        requests, mode="offline_bundle", products=["IIS", "Edge"],
    )

    assert plan is not None
    assert len(plan["requests"]) == 2
    assert {item["product"] for item in plan["requests"]} == {"IIS", "Edge"}
    assert len({item["id"] for item in plan["requests"]}) == 2


def test_root_envelope_preserves_same_product_requests_for_iis_and_edge(
    manager_module, monkeypatch,
):
    def product_run(product, *_):
        result = _product_result(product)
        result["additional_logs_needed"] = [{
            "log_kind": "evtx", "why": f"Need {product} events",
            "skill": "event_log",
        }]
        return manager_module.ProductRun(product, result, [])

    monkeypatch.setattr(manager_module, "run_product", product_run)
    result = manager_module.orchestrate_manager(
        manager_module.ManagerInput(mode="context", payload={"products": ["IIS", "Edge"]}),
        options=manager_module.ManagerOptions(skip_cross_validation=True),
    )

    requests = [
        item for item in result["additional_logs_needed"]
        if item["log_kind"] == "evtx" and item["skill"] == "event_log"
    ]
    assert len(requests) == 2
    assert {item["_source_product"] for item in requests} == {"IIS", "Edge"}