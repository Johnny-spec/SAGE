import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "law/scripts"))

from rules import aggregate, asset, condition, core_triggers, lookup_rules, normalize
from schemas import gate, resume, validate_final
from retrieval import backfill, chroma_retrieve, metadata_validation, neo4j_retrieve, reasoning, rerank
from validate import validate_bundle


def stage(name, status="DONE", version=1):
    data = {
        "identification": {"is_major": True, "matched_items": ["33"], "matched_categories": ["重大决策"]},
        "legal": {"legal_facts": ["Verified investment"], "laws_involved": ["Verified law"], "uncertainty_notes": []},
        "capital": {"financial_involvement": {}, "asset_involvement": {}, "equity_structure": {}, "risk_points": [], "insufficient_information": []},
        "procedures": {"required_procedures": [], "detected_procedures": [], "missing_procedures": [], "reflecting_documents": [], "compliance_status": "COMPLIANT"},
        "materials": {"required_documents": [], "provided_documents": [], "missing_documents": []},
    }[name]
    return {"version": version, "status": status, "data": data, "reasoning": "Fixture supported by supplied evidence", "evidence_refs": ["doc-1", "law-1"] if name == "legal" else ["doc-1"], "needs": []}


def state():
    return {"run_id": "test", "version": 1, "input_text": "Investment document", "evidence": [
        {"id": "doc-1", "source": "fixture", "content": "Investment and verified procedures", "locator": "paragraph 1", "kind": "document", "verified": True},
        {"id": "law-1", "source": "Synthetic test basis, not real law", "content": "Test review requirement", "locator": "test clause", "kind": "rule", "verified": True}
    ], "stages": {name: stage(name) for name in ("identification", "legal", "capital", "procedures", "materials")}}


class BundleTests(unittest.TestCase):
    def test_assets_and_ids(self):
        self.assertEqual(len(asset("major.json")["items"]), 90)
        self.assertEqual(asset("manifest.json")["unmapped_item_ids"], [])
        self.assertEqual(len(lookup_rules({"item_ids": ["33", "33"]})["rules"]), 1)
        with self.assertRaises(ValueError):
            lookup_rules({"item_ids": ["MD-02"]})

    def test_missing_attributes_do_not_trigger(self):
        self.assertFalse(condition(None, "=0"))
        self.assertTrue(condition(0.5, ">=0.5"))
        self.assertFalse(condition(50000000, ">50000000"))

    def test_normalize_and_both_rule_families(self):
        result = normalize({"chunk_id": "c1", "extraction": {"entities": [{"id": "a", "type": "Company", "name": "A"}, {"id": "b", "type": "Company", "name": "B"}],
            "relations": [{"from": "a", "to": "b", "type": "CONTROLS", "attributes": {"ratio": 0.6}}]}})
        self.assertEqual(result["normalized_actions"][0]["from"], "c1:a")
        self.assertIn("TR001", [hit["rule_id"] for hit in result["post_ner_trigger_results"]["fired_rules"]])
        self.assertNotIn("A", result["post_ner_trigger_results"]["triggered_concepts"])
        self.assertIn("A", result["retrieval_filters"]["keywords"])
        core = core_triggers({"entities": [], "relations": [{"type": "CONTROL", "properties": {"ratio": 0.6}}]})
        self.assertIn("R001", [hit["rule_id"] for hit in core["fired_rules"]])

    def test_entity_and_relation_joint_match(self):
        result = core_triggers({"entities": [{"id": "a", "type": "Company", "properties": {"is_state_owned": True}}], "relations": []})
        self.assertEqual(result["fired_rules"], [])

    def test_gate_requires_review(self):
        self.assertEqual(gate(state())["action"], "REVIEW")

    def test_nonmajor_and_error(self):
        value = state()
        value["stages"] = {"identification": stage("identification")}
        value["stages"]["identification"]["data"]["is_major"] = False
        self.assertEqual(gate(value)["action"], "FINAL_SKIP")
        value["stages"]["identification"]["status"] = "ERROR"
        self.assertEqual(gate(value)["action"], "ERROR")

    def test_supplement_resume_invalidates_all(self):
        value = state()
        value["stages"]["capital"]["status"] = "INSUFFICIENT"
        value["stages"]["capital"]["needs"] = ["Funding source"]
        self.assertEqual(gate(value)["action"], "SUPPLEMENT")
        result = resume({"state": value, "addition": {"input_text": "Funding from cash reserves"}})
        self.assertEqual(result["state"]["version"], 2)
        self.assertEqual(result["state"]["stages"], {})
        with self.assertRaises(ValueError):
            resume({"state": value, "addition": {"input_text": value["input_text"]}})

    def test_supplement_limit_and_unverified_evidence(self):
        value = state()
        value["stages"]["materials"]["status"] = "INSUFFICIENT"
        value["stages"]["materials"]["needs"] = ["Resolution"]
        value["supplement_round"] = 3
        self.assertEqual(gate(value)["action"], "MANUAL_REVIEW")
        value = state()
        value["evidence"][0]["verified"] = False
        self.assertEqual(gate(value)["action"], "MANUAL_REVIEW")

    def test_confirmed_violation_and_outage(self):
        value = state()
        value["stages"]["procedures"]["status"] = "NON_COMPLIANT"
        value["stages"]["procedures"]["data"]["compliance_status"] = "NON_COMPLIANT"
        value["review"] = {"version": 1, "approval_status": "APPROVED", "final_risk_assessment": "LOW", "reasoning": "fixture", "evidence_refs": ["doc-1"], "key_findings": [], "recommendations": []}
        with self.assertRaises(ValueError):
            gate(value)
        value["review"]["approval_status"] = "REJECTED"
        self.assertEqual(gate(value)["action"], "FINAL")
        value["stages"]["legal"]["status"] = "BLOCKED"
        self.assertEqual(gate(value)["action"], "BLOCKED")

    def test_final_cannot_change_review(self):
        value = state()
        review = {"version": 1, "approval_status": "APPROVED", "final_risk_assessment": "LOW", "reasoning": "fixture", "evidence_refs": ["doc-1"], "key_findings": ["Checked"], "recommendations": []}
        value["review"] = review
        result = {key: review[key] for key in ("approval_status", "final_risk_assessment", "key_findings", "recommendations")}
        result["compliance_summary"] = "合法合规性审核意见书\n一、决策事项基本情况\n二、主要依据\n三、法律分析\n四、审核结论\n五、建议"
        self.assertTrue(validate_final({"state": value, "final_result": result})["valid"])
        result["approval_status"] = "REJECTED"
        with self.assertRaises(ValueError):
            validate_final({"state": value, "final_result": result})

    def test_aggregate_preserves_reasons_and_provenance(self):
        result = aggregate({"chunks": [{"chunk_id": "c1", "legal_facts": ["Fact"], "evidence": state()["evidence"]}, {"chunk_id": "c2", "legal_facts": ["Fact"], "evidence": state()["evidence"]}]})
        self.assertEqual(result["legal_facts"], ["Fact"])
        self.assertNotIn("step1_reasoning", result)
        self.assertEqual(len(result["evidence"]), 2)

    def test_backfill_dry_run_never_connects(self):
        with patch("retrieval.driver") as connection:
            self.assertFalse(backfill({"run_id": "test", "concept_ids": []})["write_performed"])
            connection.assert_not_called()

    def test_read_queries_are_parameterized(self):
        connection = MagicMock()
        session = connection.__enter__.return_value.session.return_value.__enter__.return_value
        session.run.return_value = []
        with patch("retrieval.driver", return_value=connection), patch("retrieval.session_options", return_value={"default_access_mode": "READ"}):
            self.assertTrue(neo4j_retrieve({"keywords": ["' DELETE n"]})["empty"])
        for call in session.run.call_args_list:
            self.assertNotIn("' DELETE n", call.args[0])
            self.assertNotIn("MERGE", call.args[0])

    def test_chroma_fallback_and_rerank(self):
        collection = MagicMock()
        collection.count.return_value = 1
        collection.query.side_effect = [
            {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]},
            {"ids": [["one"]], "documents": [["Full source text"]], "metadatas": [[{"source": "law source"}]], "distances": [[0.2]]},
        ]
        with patch("retrieval.open_collection", return_value=collection), patch("retrieval.embed", return_value=[[0.1, 0.2]]):
            result = chroma_retrieve({"queries": ["investment"], "retrieval_filters": {"strict_filters": [{"law_name": "X"}], "fallback_filters": [{"concept": "Y"}]}})
        self.assertEqual(result["retrieval_mode"], "structured_fallback")
        self.assertFalse(result["evidence"][0]["verified"])
        self.assertEqual(rerank({"candidates": result["results"], "selected_ids": ["one"]})["results"][0]["content"], "Full source text")
        with self.assertRaises(ValueError):
            rerank({"candidates": result["results"], "selected_ids": ["invented"]})

    def test_service_error_not_empty_success(self):
        with patch("retrieval.open_collection", side_effect=RuntimeError("private service failure")):
            with self.assertRaises(RuntimeError):
                chroma_retrieve({"queries": ["investment"]})

    def test_empty_metadata_is_not_a_match(self):
        self.assertFalse(metadata_validation({"law_name": ""}, {"law_name": ["law"]})["soft_pass"])

    def test_reasoning_is_not_legal_judgment(self):
        result = reasoning({"canonical_relations": [], "rules": [{"id": "r1", "required_relations": ["APPROVED_BY"]}]})
        self.assertTrue(result["missing_requirements"][0]["requires_agent_verification"])
        self.assertFalse(result["write_performed"])

    def test_bundle_resource_checks(self):
        self.assertEqual(len(validate_bundle()["skills"]), 8)

    def test_unverified_skip_and_missing_legal_basis(self):
        value = state()
        value["stages"]["identification"]["data"]["is_major"] = False
        value["evidence"][0]["verified"] = False
        self.assertEqual(gate(value)["action"], "MANUAL_REVIEW")
        value = state()
        value["stages"]["legal"]["evidence_refs"] = ["doc-1"]
        self.assertEqual(gate(value)["action"], "MANUAL_REVIEW")

    def test_complete_supplement_to_report(self):
        value = state()
        value["stages"]["materials"]["status"] = "INSUFFICIENT"
        value["stages"]["materials"]["needs"] = ["Signed resolution"]
        resumed = resume({"state": value, "addition": {"input_text": "Signed resolution supplied", "file_list": ["resolution.txt"]}})["state"]
        for name in ("identification", "legal", "capital", "procedures", "materials"):
            resumed["stages"][name] = stage(name, version=2)
        self.assertEqual(gate(resumed)["action"], "REVIEW")
        resumed["review"] = {"version": 2, "approval_status": "APPROVED", "final_risk_assessment": "LOW", "reasoning": "Synthetic test only", "evidence_refs": ["doc-1", "law-1"], "key_findings": [], "recommendations": []}
        self.assertEqual(gate(resumed)["action"], "FINAL")

    def test_stale_or_unknown_evidence_rejected(self):
        value = state()
        value["stages"]["legal"]["version"] = 2
        with self.assertRaises(ValueError):
            gate(value)
        value = state()
        value["stages"]["legal"]["evidence_refs"] = ["fabricated"]
        with self.assertRaises(ValueError):
            gate(value)

    def test_backfill_apply_requires_explicit_enable(self):
        with patch.dict(os.environ, {"LAW_ALLOW_BACKFILL": "0"}), patch("retrieval.driver") as connection:
            with self.assertRaises(RuntimeError):
                backfill({"run_id": "test"}, apply=True)
            connection.assert_not_called()

    def test_copied_bundle_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "portable"
            shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns("__pycache__", ".env", ".venv", "runs"))
            cli = destination / "law/scripts/cli.py"
            for arguments in (["preflight", "--offline"], ["init", "--input", str(destination / "examples/input.json")],
                              ["gate", "--input", str(destination / "examples/input.json")], ["schema"], ["--help"]):
                output = subprocess.run([sys.executable, str(cli), *arguments], cwd=temporary, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(output.returncode, 0, output.stdout + output.stderr)
                if arguments[0] != "--help":
                    json.loads(output.stdout)

    def test_cli_errors_do_not_echo_sensitive_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text(json.dumps({"run_id": {"private": "test-secret-never-log"}, "input_text": "fixture"}), encoding="utf-8")
            output = subprocess.run([sys.executable, str(ROOT / "law/scripts/cli.py"), "init", "--input", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertNotEqual(output.returncode, 0)
            self.assertNotIn("test-secret-never-log", output.stdout + output.stderr)
            self.assertIn("diagnostics", json.loads(output.stdout))


if __name__ == "__main__":
    unittest.main()