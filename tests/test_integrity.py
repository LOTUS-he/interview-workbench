"""Integrity regressions for frozen evidence, decisions, and final artifacts."""
import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from test_pipeline import ROOT, load_module
from runtime_io import read_json, sha, write_json
from validate_outputs import validate


class IntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_module("runner_integrity", ROOT / "tools/run_demo.py")

    def prepare(self, folder):
        config = folder / "config.json"
        write_json(config, {"mode": "mock"})
        database = folder / "fixture.sqlite"
        database.touch()
        run = folder / "run"
        args = Namespace(
            input=str(ROOT / "examples/high-risk-input.json"),
            config=str(config),
            database=str(database),
            output_dir=str(run),
        )
        with patch(
            "fetch_evidence.collect_catalog",
            return_value={"terms": [], "errors": [], "calls": []},
        ), contextlib.redirect_stdout(io.StringIO()):
            self.runner.prepare(args)
        return run

    def complete_run(self, folder):
        run = self.prepare(folder)
        decision = {
            "run_id": read_json(run / "manifest.json")["run_id"],
            "phase": "query",
            "producer": {"type": "agent", "model": "integrity-test-fixture"},
            "profile": {
                "risk": "L0",
                "attributes": dict.fromkeys(
                    [
                        "brand_name",
                        "generic_name",
                        "dosage_form",
                        "pack_count",
                        "pack_unit",
                        "audience",
                    ]
                ),
                "attribute_evidence": {},
                "reason": "TEST FIXTURE: admission exclusion",
                "rule_refs": ["ADM-002"],
                "evidence_refs": ["evidence/task.json#/raw_query"],
            },
            "rewrites": [],
        }
        decision_path = folder / "decision.json"
        write_json(decision_path, decision)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.resume_query(
                Namespace(run_dir=str(run), decision=str(decision_path))
            )
        self.assertEqual(read_json(run / "manifest.json")["phase"], "complete")
        return run

    def test_deleted_hash_entry_is_rejected_by_freeze_and_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.complete_run(Path(tmp))
            manifest = read_json(run / "manifest.json")
            del manifest["evidence_hashes"]["evidence/task.json"]
            write_json(run / "manifest.json", manifest)

            with self.assertRaisesRegex(ValueError, "incomplete hash/file inventory"):
                self.runner.check_frozen(run, manifest)
            self.assertIn(
                "Evidence inventory/hash coverage differs from contract",
                validate(run),
            )

    def test_deleted_file_and_hash_entry_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.complete_run(Path(tmp))
            (run / "evidence/history.json").unlink()
            manifest = read_json(run / "manifest.json")
            del manifest["evidence_hashes"]["evidence/history.json"]
            write_json(run / "manifest.json", manifest)

            with self.assertRaisesRegex(ValueError, "incomplete hash/file inventory"):
                self.runner.check_frozen(run, manifest)
            errors = validate(run)
            self.assertIn(
                "Evidence inventory/hash coverage differs from contract", errors
            )

    def test_modified_artifact_is_rejected_by_artifact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.complete_run(Path(tmp))
            artifact = run / "steps/40_final_review.json"
            obj = read_json(artifact)
            obj["data"]["integrity_tamper_marker"] = True
            write_json(artifact, obj)

            self.assertIn(
                "Artifact hash mismatch steps/40_final_review.json", validate(run)
            )

    def test_updated_artifact_hash_does_not_authorize_wrong_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.complete_run(Path(tmp))
            final_path = run / "steps/40_final_review.json"
            report_path = run / "steps/50_decision_report.json"
            final = read_json(final_path)
            report = read_json(report_path)
            final["data"]["recommendation"] = "submit_for_human_review"
            report["data"]["recommendation"] = "submit_for_human_review"
            write_json(final_path, final)
            write_json(report_path, report)

            manifest = read_json(run / "manifest.json")
            manifest["artifact_hashes"]["steps/40_final_review.json"] = sha(final_path)
            manifest["artifact_hashes"]["steps/50_decision_report.json"] = sha(
                report_path
            )
            write_json(run / "manifest.json", manifest)

            errors = validate(run)
            self.assertNotIn(
                "Artifact hash mismatch steps/40_final_review.json", errors
            )
            self.assertNotIn(
                "Artifact hash mismatch steps/50_decision_report.json", errors
            )
            self.assertIn("Final review differs from deterministic recomputation", errors)


if __name__ == "__main__":
    unittest.main()
