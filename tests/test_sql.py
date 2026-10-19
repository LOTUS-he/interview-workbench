"""Independent SQL checks for the historical query-gap module."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = REPO_ROOT / "skill-source" / "medical-query-safety-review"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MODULE_ROOT / "scripts"))

import query_history  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"
TASK = {
    "query_id": "QTEST",
    "raw_query": "原始词",
    "city_id": "330100",
    "region_id": "330100-B",
    "scene_id": "medical_search",
    "history_start": "2026-09-13T00:00:00+08:00",
    "history_end_exclusive": "2026-09-20T00:00:00+08:00",
}


def build_fixture(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript((FIXTURES / "sql_schema.sql").read_text(encoding="utf-8"))
        db.executescript((FIXTURES / "sql_hand_sample.sql").read_text(encoding="utf-8"))


class QueryHistorySqlTests(unittest.TestCase):
    def test_hand_sample_has_request_level_funnel_and_independent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            result = query_history.analyze(db_path, TASK)

        # R1/R2/R3/R5/R7/R8 are in scope; R4 is another city and R6 is the
        # right-open end boundary.  R1 remains eligible despite C2 failing;
        # R8 remains unknown because one keyword-passed candidate lacks a
        # supply-stage log.
        self.assertEqual(
            {key: result[key] for key in (
                "request_count",
                "eligible_count",
                "keyword_miss_count",
                "supply_fail_count",
                "plan_fail_count",
                "unknown_count",
                "verified_gap_count",
            )},
            {
                "request_count": 6,
                "eligible_count": 1,
                "keyword_miss_count": 3,
                "supply_fail_count": 0,
                "plan_fail_count": 0,
                "unknown_count": 2,
                "verified_gap_count": 2,
            },
        )
        evidence = result["evidence_rows"]
        self.assertNotIn('candidate_gap_evidence',result)
        self.assertEqual(result['candidate_gap_evidence_ref'],'#/evidence_rows')
        self.assertEqual(result["candidate_gap_evidence_count"], 4)
        self.assertEqual(result["candidate_gap_evidence_total"], 4)
        self.assertEqual(result["candidate_gap_evidence_verified_count"], 3)
        self.assertTrue(all("request_id" in row and "term_id" in row and "ad_plan_id" in row and "offer_id" in row and "sku_id" in row for row in evidence))
        self.assertEqual(result['evidence_contract']['semantic_review_status'],'not_run')
        self.assertEqual(result['evidence_contract']['safe_review_gate'],'hold_pending_semantic_review')
        self.assertEqual(result['evidence_contract']['attribute_version_comparison'],'not_available')
        self.assertTrue(all('request_count' not in row and 'total_rows' not in row for row in evidence))
        self.assertEqual(len(evidence), 4)
        self.assertEqual(
            sum(row["verification_status"] == "unknown" for row in evidence),
            1,
        )
        self.assertEqual(
            sum(row["verification_status"] == "verified" for row in evidence),
            3,
        )
        self.assertEqual(
            len({row["request_id"] for row in evidence if row["verification_status"] == "verified"}),
            2,
        )

    def test_no_recall_log_is_unknown_and_no_history_does_not_create_opportunity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            with sqlite3.connect(db_path) as db:
                db.execute("DELETE FROM recall_events")
                db.execute("DELETE FROM historical_candidate_replay")
                db.execute("DELETE FROM historical_eligibility")
                db.commit()
            result = query_history.analyze(db_path, TASK)

        self.assertEqual(result["request_count"], 6)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["keyword_miss_count"], 0)
        self.assertEqual(result["unknown_count"], 6)
        self.assertEqual(result["verified_gap_count"], 0)
        self.assertEqual(result["evidence_rows"], [])
        self.assertEqual(result["candidate_gap_evidence_count"], 0)

    def test_right_open_time_boundary_and_city_region_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            task = {**TASK, "history_start": "2026-09-20T00:00:00+08:00", "history_end_exclusive": "2026-09-21T00:00:00+08:00"}
            result = query_history.analyze(db_path, task)

        # Only R6 is in this one-day window; its recall event is outside the
        # selected city only through the replay fixture, so no online log means unknown.
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["verified_gap_count"], 0)

    def test_overlapping_history_is_unknown_even_when_both_versions_are_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            result = query_history.analyze(db_path, TASK)

        overlap = [row for row in result["evidence_rows"] if row["request_id"] == "R5"]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["snapshot_count"], 2)
        self.assertEqual(overlap[0]["verification_status"], "unknown")
        self.assertEqual(result["verified_gap_count"], 2)

    def test_missing_partition_blocks_verified_count_without_making_up_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            with sqlite3.connect(db_path) as db:
                db.execute(
                    "DELETE FROM load_manifest WHERE dataset = ? AND biz_date = ? AND city_id = ?",
                    ("historical_candidate_replay", "2026-09-19", "330100"),
                )
                db.commit()
            result = query_history.analyze(db_path, TASK)

        self.assertFalse(result["complete"])
        self.assertEqual(result["verified_gap_count"], 0)
        self.assertTrue(any(gap["dataset"] == "historical_candidate_replay" for gap in result["load_gaps"]))
        self.assertTrue(result["verification_blocked"])

    def test_sql_hash_and_read_only_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            before = db_path.read_bytes()
            result = query_history.analyze(db_path, TASK)
            after = db_path.read_bytes()

        expected_hash = hashlib.sha256(query_history.SQL_PATH.read_bytes()).hexdigest()
        self.assertEqual(result["sql_sha256"], expected_hash)
        self.assertEqual(before, after)
        self.assertEqual(result["source"], "sqlite:read-only")
        self.assertEqual(result["source_details"]["database_mode"], "ro")

    def test_query_raw_query_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample.sqlite"
            build_fixture(db_path)
            with self.assertRaises(query_history.QueryHistoryError):
                query_history.analyze(db_path, {**TASK, "raw_query": "另一条词"})

    def test_output_limit_keeps_summary_and_marks_truncation(self) -> None:
        original_limit = query_history.MAX_OUTPUT_ROWS
        try:
            query_history.MAX_OUTPUT_ROWS = 3
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "sample.sqlite"
                build_fixture(db_path)
                result = query_history.analyze(db_path, TASK)
        finally:
            query_history.MAX_OUTPUT_ROWS = original_limit

        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["request_count"], 6)
        self.assertLessEqual(len(result["evidence_rows"]), 3)

    def test_q0001_contract_numbers_and_cli(self) -> None:
        from mock.init_db import initialize

        case_input = REPO_ROOT / "examples" / "case-input.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = initialize(Path(temp_dir) / "q0001.sqlite")
            task = json.loads(case_input.read_text(encoding="utf-8"))
            result = query_history.analyze(db_path, task)
            self.assertEqual(result["request_count"], 12000)
            self.assertEqual(result["eligible_count"], 3000)
            self.assertEqual(result["keyword_miss_count"], 6000)
            self.assertEqual(result["supply_fail_count"], 2000)
            self.assertEqual(result["plan_fail_count"], 1000)
            self.assertEqual(result["unknown_count"], 0)
            self.assertEqual(result["verified_gap_count"], 6000)
            self.assertEqual(len(result['evidence_rows']),500)
            self.assertEqual(result['evidence_rows_total'],6000)
            self.assertTrue(result['output_truncated'])
            self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),400_000)

            output_path = Path(temp_dir) / "analysis.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_ROOT / "scripts" / "query_history.py"),
                    "--database",
                    str(db_path),
                    "--input",
                    str(case_input),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            cli_result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(cli_result["request_count"], 12000)
            self.assertEqual(cli_result["verified_gap_count"], 6000)


if __name__ == "__main__":
    unittest.main()
