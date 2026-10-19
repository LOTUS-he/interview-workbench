import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluations"))

import evaluate  # noqa: E402


DATASET_ID = "test-fixed-candidate-v1"


def make_documents(
    *,
    query_decision="pass",
    high_risk=False,
    term_decision="pass",
    relevance="pass",
    eligibility="pass",
    predictions=None,
):
    inputs = {
        "schema_version": "1.0",
        "dataset_id": DATASET_ID,
        "split": "test",
        "task_type": "fixed_candidate",
        "queries": [{"case_id": "case-a", "query_id": "Q1", "raw_query": "测试Query"}],
        "terms": [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "term": "测试term",
            }
        ],
        "candidates": [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "candidate_id": "C1",
            }
        ],
    }
    labels = {
        "schema_version": "1.0",
        "dataset_id": DATASET_ID,
        "split": "test",
        "task_type": "fixed_candidate",
        "label_source": {
            "kind": "synthetic_teacher_rubric",
            "is_real_human_gold": False,
        },
        "query_labels": [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "high_risk": high_risk,
                "decision": query_decision,
            }
        ],
        "term_labels": [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "decision": term_decision,
            }
        ],
        "candidate_labels": [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "candidate_id": "C1",
                "relevance": relevance,
                "eligibility": eligibility,
            }
        ],
    }
    if predictions is None:
        predictions = {
            "schema_version": "1.0",
            "dataset_id": DATASET_ID,
            "split": "test",
            "queries": [
                {
                    "case_id": "case-a",
                    "query_id": "Q1",
                    "status": "success",
                    "decision": "pass",
                }
            ],
            "terms": [
                {
                    "case_id": "case-a",
                    "query_id": "Q1",
                    "term_id": "T1",
                    "status": "success",
                    "decision": "pass",
                }
            ],
            "candidates": [
                {
                    "case_id": "case-a",
                    "query_id": "Q1",
                    "term_id": "T1",
                    "candidate_id": "C1",
                    "status": "success",
                    "relevance": "pass",
                    "eligibility": "pass",
                }
            ],
        }
    return inputs, labels, predictions


class EvaluationContractTests(unittest.TestCase):
    def test_dev_fixture_declares_synthetic_labels_and_explicit_keys(self):
        inputs = evaluate.load_json(ROOT / "evaluations/dev/inputs.json")
        labels = evaluate.load_json(ROOT / "evaluations/dev/labels.json")

        self.assertEqual(len(inputs["queries"]), 8)
        self.assertEqual(len(inputs["terms"]), 13)
        self.assertEqual(len(inputs["candidates"]), 14)
        self.assertEqual(labels["label_source"]["kind"], "synthetic_teacher_rubric")
        self.assertFalse(labels["label_source"]["is_real_human_gold"])
        for row in inputs["terms"] + inputs["candidates"]:
            self.assertIn("case_id", row)
            self.assertIn("query_id", row)
            self.assertIn("term_id", row)
        for row in inputs["candidates"]:
            self.assertIn("candidate_id", row)

    def test_case_id_query_id_term_id_and_candidate_id_are_all_part_of_matching(self):
        inputs, labels, predictions = make_documents()
        # Same query_id and term_id in another case must remain a different key.
        inputs["queries"].append(
            {"case_id": "case-b", "query_id": "Q1", "raw_query": "另一个case"}
        )
        inputs["terms"].append(
            {"case_id": "case-b", "query_id": "Q1", "term_id": "T1", "term": "另一个term"}
        )
        inputs["candidates"].append(
            {
                "case_id": "case-b",
                "query_id": "Q1",
                "term_id": "T1",
                "candidate_id": "C1",
            }
        )
        labels["query_labels"].append(
            {"case_id": "case-b", "query_id": "Q1", "high_risk": False, "decision": "pass"}
        )
        labels["term_labels"].append(
            {"case_id": "case-b", "query_id": "Q1", "term_id": "T1", "decision": "pass"}
        )
        labels["candidate_labels"].append(
            {
                "case_id": "case-b",
                "query_id": "Q1",
                "term_id": "T1",
                "candidate_id": "C1",
                "relevance": "reject",
                "eligibility": "reject",
            }
        )

        report = evaluate.evaluate_documents(inputs, labels, predictions)

        self.assertEqual(report["metrics"]["qualified_mapping_recall"]["numerator"], 1)
        self.assertEqual(report["metrics"]["qualified_mapping_recall"]["denominator"], 2)
        self.assertEqual(report["metrics"]["item_mismatch_leakage_rate"]["numerator"], 0)
        self.assertEqual(report["metrics"]["item_mismatch_leakage_rate"]["denominator"], 1)
        self.assertTrue(any("MISSING_PREDICTION" in item for item in report["warnings"]))

    def test_missing_and_failed_predictions_stay_in_recall_denominator(self):
        inputs, labels, predictions = make_documents()
        predictions["terms"] = []
        predictions["candidates"] = [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "candidate_id": "C1",
                "status": "failed",
            }
        ]
        report = evaluate.evaluate_documents(inputs, labels, predictions)

        self.assertEqual(report["metrics"]["qualified_mapping_recall"], {
            "numerator": 0,
            "denominator": 1,
            "value": 0.0,
            "status": "ok",
        })
        self.assertEqual(report["metrics"]["eligibility_recall"]["numerator"], 0)
        self.assertEqual(report["metrics"]["eligibility_recall"]["denominator"], 1)
        self.assertEqual(report["counts"]["predictions"]["issues"]["failed"]["candidate_eligibility"], 1)
        self.assertTrue(any("MISSING_PREDICTION" in item for item in report["warnings"]))

    def test_skipped_is_not_semantic_or_execution_failure(self):
        inputs, labels, predictions = make_documents()
        predictions["terms"][0]["status"] = "skipped"
        predictions["terms"][0].pop("decision")

        report = evaluate.evaluate_documents(inputs, labels, predictions)

        self.assertEqual(report["metrics"]["term_decision_accuracy"], {
            "numerator": 0,
            "denominator": 0,
            "value": None,
            "status": "not_applicable",
        })
        self.assertEqual(report["metrics"]["qualified_mapping_recall"], {
            "numerator": 0,
            "denominator": 1,
            "value": 0.0,
            "status": "ok",
        })
        self.assertEqual(report["metrics"]["execution_failure_or_invalid_rate"], {
            "numerator": 0,
            "denominator": 4,
            "value": 0.0,
            "status": "ok",
        })
        self.assertEqual(report["metrics"]["skipped_rate"], {
            "numerator": 1,
            "denominator": 4,
            "value": 0.25,
            "status": "ok",
        })

    def test_duplicate_prediction_is_not_first_wins(self):
        inputs, labels, predictions = make_documents()
        predictions["terms"] = [
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "decision": "pass",
            },
            {
                "case_id": "case-a",
                "query_id": "Q1",
                "term_id": "T1",
                "decision": "reject",
            },
        ]
        report = evaluate.evaluate_documents(inputs, labels, predictions)

        self.assertEqual(report["metrics"]["qualified_mapping_recall"]["numerator"], 0)
        self.assertEqual(report["metrics"]["qualified_mapping_recall"]["denominator"], 1)
        self.assertEqual(report["metrics"]["mapping_precision_known_gold"]["denominator"], 0)
        self.assertEqual(report["counts"]["predictions"]["duplicate_keys"]["terms"], 1)
        self.assertTrue(any("DUPLICATE_PREDICTION_KEY" in item for item in report["warnings"]))

    def test_unknown_state_is_scored_separately_and_unknown_keys_are_ignored(self):
        inputs, labels, predictions = make_documents(
            query_decision="unknown",
            term_decision="unknown",
            relevance="unknown",
            eligibility="unknown",
        )
        predictions["queries"][0]["decision"] = "unknown"
        predictions["terms"][0]["decision"] = "unknown"
        predictions["candidates"][0]["relevance"] = "unknown"
        predictions["candidates"][0]["eligibility"] = "unknown"
        predictions["terms"].append(
            {"case_id": "not-in-input", "query_id": "Q1", "term_id": "T1", "decision": "pass"}
        )

        report = evaluate.evaluate_documents(inputs, labels, predictions)

        self.assertEqual(report["metrics"]["unknown_state_handling_accuracy"], {
            "numerator": 4,
            "denominator": 4,
            "value": 1.0,
            "status": "ok",
        })
        self.assertEqual(report["counts"]["predictions"]["unknown_keys"]["terms"]["rows"], 1)
        self.assertTrue(any("UNKNOWN_PREDICTION_KEY" in item for item in report["warnings"]))
        self.assertEqual(report["metrics"]["mapping_precision_known_gold"]["denominator"], 0)

    def test_zero_denominator_is_null_not_zero_or_one(self):
        inputs, labels, predictions = make_documents()
        inputs["terms"] = []
        inputs["candidates"] = []
        labels["term_labels"] = []
        labels["candidate_labels"] = []
        predictions["terms"] = []
        predictions["candidates"] = []

        report = evaluate.evaluate_documents(inputs, labels, predictions)

        for name in (
            "high_risk_leakage_rate",
            "mapping_precision_known_gold",
            "qualified_mapping_recall",
            "item_mismatch_leakage_rate",
            "eligibility_precision_known_gold",
            "eligibility_recall",
        ):
            self.assertIsNone(report["metrics"][name]["value"], name)
            self.assertEqual(report["metrics"][name]["denominator"], 0)
            self.assertEqual(report["metrics"][name]["status"], "not_applicable")
        self.assertTrue(any("METRIC_NOT_APPLICABLE" in item for item in report["warnings"]))

    def test_cli_prints_json_and_writes_the_same_report(self):
        inputs, labels, predictions = make_documents()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            input_path = directory_path / "inputs.json"
            labels_path = directory_path / "labels.json"
            predictions_path = directory_path / "predictions.json"
            output_path = directory_path / "report.json"
            for path, document in (
                (input_path, inputs),
                (labels_path, labels),
                (predictions_path, predictions),
            ):
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "evaluations/evaluate.py"),
                    "--inputs",
                    str(input_path),
                    "--labels",
                    str(labels_path),
                    "--predictions",
                    str(predictions_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            stdout_report = json.loads(completed.stdout)
            output_report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(stdout_report, output_report)
            self.assertEqual(stdout_report["evaluator"], "fixed-candidate-offline-v2")
            self.assertIn("warnings", stdout_report)
            self.assertIn("numerator", stdout_report["metrics"]["mapping_precision_known_gold"])
            self.assertIn("denominator", stdout_report["metrics"]["mapping_precision_known_gold"])


if __name__ == "__main__":
    unittest.main()
