import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skill-source" / "medical-query-safety-review" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_evidence  # noqa: E402


TOKEN = "medical-readonly"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockService:
    def __init__(self, scenario="normal"):
        self.scenario = scenario
        self.port = _free_port()
        self.process = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        environment = os.environ.copy()
        environment["MEDICAL_DEMO_TOKEN"] = TOKEN
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "mock" / "api_server.py"),
                "--port",
                str(self.port),
                "--scenario",
                self.scenario,
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with urlopen(self.base_url + "/health", timeout=0.2) as response:
                    if response.status == 200:
                        return self
            except (OSError, URLError):
                time.sleep(0.02)
        self.__exit__(None, None, None)
        raise RuntimeError("mock service did not start")

    def __exit__(self, exc_type, exc, tb):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            self.process = None


def _config(service, **overrides):
    value = {
        "mode": "mock",
        "base_url": service.base_url,
        "token_env": "__MEDICAL_TEST_TOKEN_NOT_SET__",
        "demo_token": TOKEN,
        "timeout_seconds": 0.5,
        "max_retries": 2,
        "total_timeout_seconds": 8,
        "max_pages": 100,
        "max_page_size": 100,
        "max_batch_ids": 50,
    }
    value.update(overrides)
    return value


class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://example.invalid/external")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class RedirectService:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        self.thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FetchEvidenceTests(unittest.TestCase):
    def test_catalog_paginates_and_loads_independent_lexicon(self):
        with MockService() as service:
            result = fetch_evidence.collect_catalog(_config(service, max_page_size=7))

        self.assertEqual(len(result["terms"]), 48)
        self.assertEqual(result["lexicon"]["version"], "lex-1")
        self.assertEqual(result["meta"]["business_time"], "2026-09-20T12:00:00+08:00")
        self.assertTrue(result["meta"]["health_checked"])
        term_calls = [call for call in result["calls"] if call["path"] == "/v1/terms"]
        self.assertEqual(len(term_calls), 7)
        self.assertEqual(term_calls[0]["response_meta"]["source"], "synthetic_company_api")
        self.assertEqual(term_calls[0]["response_meta"]["total"], 48)
        self.assertEqual(term_calls[0]["response_meta"]["next_cursor"], "7")
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))

    def test_candidates_keep_plan_dimension_and_all_fact_objects(self):
        with MockService() as service:
            result = fetch_evidence.collect_candidates(
                _config(service), ["T0001"], "330100"
            )

        candidates = result["candidates"]
        self.assertEqual(len(candidates), 25)
        self.assertEqual(len({candidate["candidate_id"] for candidate in candidates}), 25)
        same_offer = [candidate for candidate in candidates if candidate["offer_id"] == "O00001"]
        self.assertEqual({candidate["candidate_id"] for candidate in same_offer}, {"A00001", "A00999"})
        for candidate in candidates:
            self.assertEqual(candidate["plan"]["ad_plan_id"], candidate["candidate_id"])
            self.assertEqual(candidate["offer"]["offer_id"], candidate["offer_id"])
            self.assertEqual(candidate["product"]["sku_id"], candidate["sku_id"])
            self.assertEqual(candidate["store"]["store_id"], candidate["store_id"])
            self.assertEqual(candidate["qualification"]["sku_id"], candidate["sku_id"])
            self.assertIn("version", candidate["offer"])
            self.assertIn("as_of", candidate["offer"])
            self.assertIn("version", candidate["plan"])
            self.assertIn("product_version", candidate["product"])
            self.assertEqual(candidate["errors"], [])

    def test_nullable_product_fields_are_preserved_as_unknown(self):
        with MockService() as service:
            result = fetch_evidence.collect_candidates(
                _config(service), ["T0001", "T0029"], "330100"
            )

        audience_unknown = next(
            candidate for candidate in result["candidates"] if candidate["sku_id"] == "SKU0008"
        )
        self.assertIsNotNone(audience_unknown["product"])
        self.assertIsNone(audience_unknown["product"]["audience"])
        self.assertFalse(
            any(error["code"] == "SCHEMA_INVALID" for error in audience_unknown["errors"])
        )

        strength_unknown = next(
            candidate
            for candidate in result["candidates"]
            if candidate["term_id"] == "T0029"
        )
        self.assertIsNotNone(strength_unknown["product"])
        self.assertIsNone(strength_unknown["product"]["strength"])
        self.assertFalse(
            any(error["code"] == "SCHEMA_INVALID" for error in strength_unknown["errors"])
        )

    def test_partial_failure_isolated_to_sku(self):
        with MockService("partial-failure") as service:
            result = fetch_evidence.collect_candidates(
                _config(service), ["T0001"], "330100"
            )

        failed = [candidate for candidate in result["candidates"] if candidate["sku_id"] == "SKU0008"]
        healthy = [candidate for candidate in result["candidates"] if candidate["sku_id"] != "SKU0008"]
        self.assertTrue(failed)
        self.assertTrue(healthy)
        for candidate in failed:
            self.assertIsNone(candidate["product"])
            self.assertIsNone(candidate["qualification"])
            self.assertIsNotNone(candidate["offer"])
            self.assertTrue(
                any(error["code"] == "UPSTREAM_TIMEOUT" for error in candidate["errors"])
            )
        self.assertTrue(all(candidate["product"] is not None for candidate in healthy))

    def test_schema_drift_is_rejected_without_coercion(self):
        with MockService("schema-drift") as service:
            result = fetch_evidence.collect_candidates(
                _config(service), ["T0001"], "330100"
            )

        drifted = [candidate for candidate in result["candidates"] if candidate["sku_id"] == "SKU0007"]
        self.assertTrue(drifted)
        self.assertTrue(all(candidate["product"] is None for candidate in drifted))
        self.assertTrue(
            all(
                any(
                    error["source"] == "product" and error["code"] == "SCHEMA_INVALID"
                    for error in candidate["errors"]
                )
                for candidate in drifted
            )
        )

    def test_401_and_400_are_not_retried(self):
        with MockService() as service:
            unauthorized = fetch_evidence.collect_catalog(
                _config(service, demo_token="wrong-token")
            )
            self.assertEqual(
                [call["attempts"] for call in unauthorized["calls"] if call["path"] == "/v1/terms"],
                [1],
            )
            self.assertTrue(any(error["code"] == "UNAUTHORIZED" for error in unauthorized["errors"]))
            self.assertNotIn("wrong-token", json.dumps(unauthorized, ensure_ascii=False))

            bad_city = fetch_evidence.collect_candidates(
                _config(service), ["T0001"], "not-a-city"
            )
            recall_calls = [call for call in bad_city["calls"] if call["path"] == "/v1/recall"]
            self.assertEqual(len(recall_calls), 1)
            self.assertEqual(recall_calls[0]["attempts"], 1)
            self.assertTrue(any(error["code"] == "INVALID_ARGUMENT" for error in bad_city["errors"]))

    def test_rate_limit_retries_are_bounded_by_count_and_deadline(self):
        with MockService("rate-limit") as service:
            result = fetch_evidence.collect_catalog(
                _config(service, max_retries=2, total_timeout_seconds=1.1)
            )

        calls = [call for call in result["calls"] if call["path"] == "/v1/terms"]
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0]["attempts"], 3)
        self.assertGreaterEqual(calls[0]["attempts"], 1)
        self.assertTrue(any(error["code"] in {"RATE_LIMITED", "DEADLINE_EXCEEDED"} for error in result["errors"]))

    def test_timeout_is_finite_and_does_not_become_success(self):
        started = time.monotonic()
        with MockService("slow") as service:
            result = fetch_evidence.collect_candidates(
                _config(
                    service,
                    timeout_seconds=0.05,
                    max_retries=1,
                    total_timeout_seconds=0.35,
                ),
                ["T0003"],
                "330100",
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        candidates = result["candidates"]
        self.assertTrue(candidates)
        self.assertTrue(any(candidate["offer"] is None for candidate in candidates))
        self.assertTrue(
            any(
                error["code"] in {"TIMEOUT", "DEADLINE_EXCEEDED"}
                for candidate in candidates
                for error in candidate["errors"]
            )
        )

    def test_redirect_is_not_followed(self):
        with RedirectService() as service:
            result = fetch_evidence.collect_catalog(
                {
                    "mode": "mock",
                    "base_url": service.base_url,
                    "token_env": "__MEDICAL_TEST_TOKEN_NOT_SET__",
                    "demo_token": TOKEN,
                    "timeout_seconds": 0.2,
                    "max_retries": 2,
                    "total_timeout_seconds": 1,
                    "max_pages": 2,
                }
            )

        self.assertTrue(any(error["code"] == "REDIRECT_BLOCKED" for error in result["errors"]))
        self.assertEqual(result["terms"], [])
        self.assertEqual(result["calls"][0]["attempts"], 1)

    def test_cli_catalog_and_candidate_modes(self):
        with MockService() as service, tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(_config(service), ensure_ascii=False), encoding="utf-8")
            catalog_path = Path(directory) / "catalog.json"
            self.assertEqual(
                fetch_evidence.main(
                    ["--config", str(config_path), "--output", str(catalog_path)]
                ),
                0,
            )
            self.assertEqual(len(json.loads(catalog_path.read_text(encoding="utf-8"))["terms"]), 48)

            candidate_path = Path(directory) / "candidates.json"
            self.assertEqual(
                fetch_evidence.main(
                    [
                        "--config",
                        str(config_path),
                        "--term-ids",
                        "T0001,T0002",
                        "--city-id",
                        "330100",
                        "--output",
                        str(candidate_path),
                    ]
                ),
                0,
            )
            candidate_output = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertTrue(candidate_output["candidates"])
            self.assertEqual(
                {candidate["term_id"] for candidate in candidate_output["candidates"]},
                {"T0001", "T0002"},
            )


if __name__ == "__main__":
    unittest.main()
