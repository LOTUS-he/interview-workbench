import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill-source/medical-query-safety-review/scripts/check_constraints.py"
POLICY_PATH = ROOT / "skill-source/medical-query-safety-review/references/eligibility-rules.yaml"
spec = importlib.util.spec_from_file_location("check_constraints", SCRIPT)
check_constraints = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(check_constraints)


NOW = "2026-09-20T12:00:00+08:00"
TASK = {
    "query_id": "Q1",
    "raw_query": "康安维C咀嚼篇60片",
    "city_id": "330100",
    "region_id": "330100-B",
    "scene_id": "medical_search",
    "history_start": "2026-09-13T00:00:00+08:00",
    "history_end_exclusive": "2026-09-20T00:00:00+08:00",
}
ATTRS = {
    "brand_name": "康安",
    "generic_name": "维生素C",
    "dosage_form": "咀嚼片",
    "pack_count": 60,
    "pack_unit": "片",
    "audience": "通用",
}


def _product(sku_id="SKU1", pack_count=60, title="康安维生素C咀嚼片60片"):
    return {
        "sku_id": sku_id,
        "brand_name": "康安",
        "generic_name": "维生素C",
        "dosage_form": "咀嚼片",
        "pack_count": pack_count,
        "pack_unit": "片",
        "audience": "通用",
        "title": title,
        "updated_at": NOW,
        "product_version": "prod-1",
    }


def _candidate(candidate_id="A1", offer_id="O1", store_id="S1", sku_id="SKU1", **overrides):
    candidate = {
        "candidate_id": candidate_id,
        "term_id": "T1",
        "sku_id": sku_id,
        "offer_id": offer_id,
        "store_id": store_id,
        "index_version": "idx-1",
        "product": _product(sku_id=sku_id),
        "offer": {
            "offer_id": offer_id,
            "sku_id": sku_id,
            "store_id": store_id,
            "city_id": "330100",
            "region_ids": ["330100-B"],
            "on_shelf": True,
            "stock": 10,
            "price_fen": 100,
            "merchant_qualification": "valid",
            "as_of": NOW,
            "version": "offer-1",
        },
        "plan": {
            "ad_plan_id": candidate_id,
            "offer_id": offer_id,
            "status": "active",
            "remaining_budget_fen": 1000,
            "material_status": "approved",
            "as_of": NOW,
            "version": "ad-1",
        },
        "store": {
            "store_id": store_id,
            "city_id": "330100",
            "region_ids": ["330100-B"],
            "merchant_qualification": "valid",
        },
        "qualification": {
            "sku_id": sku_id,
            "qualification_status": "valid",
            "prescription_type": "OTC",
            "product_version": "prod-1",
            "as_of": NOW,
        },
    }
    for key, value in overrides.items():
        candidate[key] = value
    return candidate


def _query_decision():
    provenance = {
        key: {"raw_span": str(value), "source_ref": f"evidence/query.json#/attributes/{key}"}
        for key, value in ATTRS.items()
    }
    return {
        "run_id": "run-1",
        "phase": "query",
        "producer": {"type": "agent", "model": "gpt-5.6-luna"},
        "profile": {
            "risk": "L1",
            "attributes": copy.deepcopy(ATTRS),
            "attribute_evidence": provenance,
        },
        "rewrites": [
            {
                "term_id": "T1",
                "attributes": copy.deepcopy(ATTRS),
                "decision": "pass",
                "reason": "model semantic normalization",
                "rule_refs": ["REWRITE-001"],
                "evidence_refs": ["evidence/query.json#/attributes"],
            }
        ],
    }


def _item_decision(candidates):
    return {
        "run_id": "run-1",
        "phase": "items",
        "producer": {"type": "agent", "model": "gpt-5.6-luna"},
        "reviews": [
            {
                "candidate_id": candidate["candidate_id"],
                "term_id": candidate["term_id"],
                "term_item": "pass",
                "query_item": "pass",
                "reason": "model semantic relevance",
                "rule_refs": ["REWRITE-001"],
                "evidence_refs": [f"evidence/items.json#/{index}"],
            }
            for index, candidate in enumerate(candidates)
        ],
    }


def _evidence(candidates):
    return {
        "meta": {"business_time": NOW, "dataset_version": "assignment-1.0"},
        "terms": [
            {
                "term_id": "T1",
                "term": "康安维生素C咀嚼片60片",
                "enabled": True,
                "index_version": "idx-1",
                **copy.deepcopy(ATTRS),
            }
        ],
        "candidates": copy.deepcopy(candidates),
    }


def _history(rows, request_count=600, start=None, end=None):
    value = {
        "complete": True,
        "request_count": request_count,
        "verified_gap_count": len(rows),
        "unknown_count": 0,
        "load_gaps": [],
        "evidence_rows": copy.deepcopy(rows),
    }
    if start is not None or end is not None:
        value["parameters"] = {"history_start": start, "history_end_exclusive": end}
    return value


class ConstraintRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with POLICY_PATH.open(encoding="utf-8") as handle:
            cls.policy = json.load(handle)

    def run_case(self, candidates=None, history_rows=None, task=None, policy=None, history=None):
        candidates = candidates or [_candidate()]
        history = history if history is not None else _history(history_rows or [{
            "term_id": "T1",
            "sku_id": candidates[0]["sku_id"],
            "historically_qualified": True,
            "verification_status": "verified",
        }])
        return check_constraints.check(
            task or TASK,
            _query_decision(),
            _item_decision(candidates),
            _evidence(candidates),
            policy or copy.deepcopy(self.policy),
            history=history,
        )

    def run_admission_stop(self, risk):
        query = _query_decision()
        query["profile"]["risk"] = risk
        return check_constraints.check(
            TASK,
            query,
            {
                "run_id": "run-1",
                "phase": "items",
                "producer": {"type": "not_executed", "model": None},
                "reviews": [],
            },
            _evidence([]),
            copy.deepcopy(self.policy),
            history=None,
        )

    def test_policy_is_json_compatible_and_rule_values_are_not_duplicated(self):
        rules = self.policy["rules"]
        self.assertEqual({row["id"] for row in rules}, {
            "SCOPE-001", "ADMIT-001", "REWRITE-001", "BRAND-001", "FORM-001",
            "PACK-001", "AUDIENCE-001", "EVIDENCE-001", "ELIG-001", "TIME-001",
            "DECISION-001", "VALUE-001",
        })
        for row in rules:
            self.assertEqual(set(row), {"id", "description", "pointer"})
            self.assertNotIn("source_value", row)

    def test_exact_qualified_term_sku_row_allows_submit(self):
        result = self.run_case()
        self.assertEqual(result["recommendation"], "submit_for_human_review")
        self.assertEqual(result["history_status"], "verified_linked")
        self.assertEqual(result["value_tier"], "V2")

    def test_history_row_without_qualification_marker_is_only_potential(self):
        result = self.run_case(history_rows=[{"term_id": "T1", "sku_id": "SKU1", "store_id": "S1"}])
        self.assertEqual(result["history_status"], "potential_only")
        self.assertNotEqual(result["recommendation"], "submit_for_human_review")

    def test_same_store_different_sku_cannot_link_history(self):
        result = self.run_case(history_rows=[{
            "term_id": "T1",
            "sku_id": "SKU999",
            "store_id": "S1",
            "historically_qualified": True,
            "verification_status": "verified",
        }])
        self.assertEqual(result["history_status"], "potential_only")
        self.assertNotEqual(result["recommendation"], "submit_for_human_review")

    def test_history_qualification_status_must_be_confirmed(self):
        result = self.run_case(history_rows=[{
            "term_id": "T1",
            "sku_id": "SKU1",
            "historically_qualified": True,
            "verification_status": "unknown",
        }])
        self.assertEqual(result["history_status"], "potential_only")

    def test_one_day_complete_history_cannot_receive_value_tier(self):
        one_day_task = copy.deepcopy(TASK)
        one_day_task["history_start"] = "2026-09-19T00:00:00+08:00"
        one_day_task["history_end_exclusive"] = "2026-09-20T00:00:00+08:00"
        result = self.run_case(
            task=one_day_task,
            history_rows=[{
                "term_id": "T1",
                "sku_id": "SKU1",
                "historically_qualified": True,
                "verification_status": "verified",
            }],
            history=_history(
                [{
                    "term_id": "T1",
                    "sku_id": "SKU1",
                    "historically_qualified": True,
                    "verification_status": "verified",
                }],
                start=one_day_task["history_start"],
                end=one_day_task["history_end_exclusive"],
            ),
        )
        self.assertEqual(result["history_status"], "verified_linked")
        self.assertEqual(result["value_tier"], "unknown")
        self.assertTrue(any("7 natural days" in reason for row in result["review_queue"] for reason in row["reasons"]))

    def test_missing_value_threshold_does_not_fallback_to_业务_defaults(self):
        policy = copy.deepcopy(self.policy)
        del policy["value"]["v1"]["min_valid_requests"]
        result = self.run_case(policy=policy)
        self.assertEqual(result["value_tier"], "unknown")
        self.assertTrue(any("thresholds are missing" in reason for row in result["review_queue"] for reason in row["reasons"]))

    def test_plan_count_does_not_make_v1_without_two_distinct_stores(self):
        candidates = [
            _candidate(candidate_id=f"A{i}", offer_id=f"O{i}", store_id="S1")
            for i in range(1, 4)
        ]
        rows = [
            {
                "term_id": "T1",
                "sku_id": "SKU1",
                "offer_id": f"O{i}",
                "candidate_id": f"A{i}",
                "historically_qualified": True,
                "verification_status": "verified",
            }
            for i in range(1, 4)
        ]
        result = self.run_case(candidates=candidates, history_rows=rows)
        self.assertEqual(result["distinct_store_count"], 1)
        self.assertEqual(result["value_tier"], "V2")

    def test_two_plans_on_one_offer_count_as_one_offer_and_two_candidates(self):
        candidates = [
            _candidate(candidate_id="A1", offer_id="O1", store_id="S1"),
            _candidate(candidate_id="A2", offer_id="O1", store_id="S1"),
        ]
        rows = [
            {
                "term_id": "T1",
                "sku_id": "SKU1",
                "offer_id": "O1",
                "candidate_id": candidate["candidate_id"],
                "historically_qualified": True,
                "verification_status": "verified",
            }
            for candidate in candidates
        ]
        result = self.run_case(candidates=candidates, history_rows=rows)
        self.assertEqual(result["confirmed_current_offer_count"], 1)
        self.assertEqual(result["confirmed_current_candidate_count"], 2)

    def test_l0_admission_stop_skips_history_and_value_review(self):
        result = self.run_admission_stop("L0")
        self.assertEqual(result["recommendation"], "reject")
        self.assertEqual(result["history_status"], "not_evaluated")
        self.assertEqual(result["value_tier"], "unknown")
        self.assertFalse(any(
            row["type"] in {"history_review", "value_tier"}
            for row in result["review_queue"]
        ))

    def test_l2_admission_stop_skips_history_and_value_review(self):
        result = self.run_admission_stop("L2")
        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["history_status"], "not_evaluated")
        self.assertEqual(result["value_tier"], "unknown")
        self.assertFalse(any(
            row["type"] in {"history_review", "value_tier"}
            for row in result["review_queue"]
        ))

    def test_l1_missing_history_still_requests_history_and_value_evidence(self):
        candidates = [_candidate()]
        result = check_constraints.check(
            TASK,
            _query_decision(),
            _item_decision(candidates),
            _evidence(candidates),
            copy.deepcopy(self.policy),
            history=None,
        )
        queue_types = {row["type"] for row in result["review_queue"]}
        self.assertEqual(result["history_status"], "missing")
        self.assertEqual(result["value_tier"], "unknown")
        self.assertIn("history_review", queue_types)
        self.assertIn("value_tier", queue_types)

    def test_title_pack_conflict_is_unknown_even_when_model_passes(self):
        candidate = _candidate()
        candidate["product"] = _product(pack_count=100, title="康安维生素C咀嚼片60片")
        result = self.run_case(candidates=[candidate])
        self.assertEqual(result["items"][0]["relevance"], "unknown")
        self.assertTrue(any("title and structured pack_count conflict" in reason for reason in result["items"][0]["reasons"]))

    def test_not_executed_empty_items_handoff_is_allowed_only_without_candidates(self):
        result = check_constraints.check(
            TASK,
            _query_decision(),
            {
                "run_id": "run-1",
                "phase": "items",
                "producer": {"type": "not_executed", "model": None},
                "reviews": [],
            },
            _evidence([]),
            copy.deepcopy(self.policy),
            history=_history([]),
        )
        self.assertNotEqual(result["recommendation"], "submit_for_human_review")
        self.assertFalse(any(row["type"] == "model_handoff" for row in result["review_queue"]))

    def test_not_executed_items_handoff_is_rejected_when_candidates_exist(self):
        candidate = _candidate()
        result = check_constraints.check(
            TASK,
            _query_decision(),
            {
                "run_id": "run-1",
                "phase": "items",
                "producer": {"type": "not_executed", "model": None},
                "reviews": [],
            },
            _evidence([candidate]),
            copy.deepcopy(self.policy),
            history=_history([{
                "term_id": "T1",
                "sku_id": "SKU1",
                "historically_qualified": True,
                "verification_status": "verified",
            }]),
        )
        self.assertNotEqual(result["recommendation"], "submit_for_human_review")
        self.assertTrue(any(row["type"] == "model_handoff" for row in result["review_queue"]))


if __name__ == "__main__":
    unittest.main()
