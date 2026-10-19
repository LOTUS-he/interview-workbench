import http.client
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workbench = load_module("medical_workbench_server_for_tests", ROOT / "workbench/server.py")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class WorkbenchHTTPTests(unittest.TestCase):
    ASYNC_TIMEOUT = 20

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="medical-workbench-test-")
        base = Path(self.tempdir.name)
        self.runtime_root = base / "runtime"
        self.history_root = base / "history"
        self._write_history_fixture()

        self.app = workbench.Workbench(
            runtime_root=self.runtime_root,
            history_root=self.history_root,
        )
        self.httpd = workbench.make_server(0, self.app)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        # close() is responsible for every subprocess started by Workbench.prepare.
        self.app.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

        deadline = time.monotonic() + 3
        while self.app.processes and time.monotonic() < deadline:
            time.sleep(0.02)
        for process in list(self.app.processes):
            self.app.stop_process(process)
        self.tempdir.cleanup()

    def _write_history_fixture(self):
        # Read-only fixture: this is not a agent output and contains no model answer.
        run = self.history_root / "recorded-001"
        write_json(
            run / "manifest.json",
            {
                "run_id": "history-run-001",
                "created_at": "2026-09-20T00:00:00+00:00",
                "mode": "mock",
                "phase": "complete",
                "steps": {},
                "model_calls": [],
                "source_hashes": workbench.source_hashes(),
            },
        )
        write_json(
            run / "evidence" / "task.json",
            {
                "query_id": "Q0001",
                "raw_query": "测试历史Query",
                "city_id": "330100",
                "region_id": "330100-A",
            },
        )

    def _request(self, method, path, body=None, headers=None, timeout=3):
        request_headers = {"Host": self.host}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=timeout)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read()
        finally:
            connection.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw
        return status, payload

    def _add_run_fixture(self, rid, candidates=None):
        """Add response-shaped data only; no semantic/model prediction is fabricated."""
        run = self.runtime_root / rid / "run"
        task = {
            "query_id": "Q0001",
            "raw_query": "测试Query",
            "city_id": "330100",
            "region_id": "330100-A",
        }
        write_json(
            run / "manifest.json",
            {
                "run_id": rid,
                "created_at": "2026-09-21T00:00:00+00:00",
                "mode": "mock",
                "phase": "awaiting_items",
                "steps": {},
                "model_calls": [],
            },
        )
        write_json(run / "evidence" / "task.json", task)
        write_json(
            run / "evidence" / "candidates.json",
            {
                "terms": [],
                "candidates": candidates or [],
                "errors": [],
                "calls": [],
                "meta": {"source": "test-fixture"},
            },
        )
        write_json(run / "evidence" / "catalog.json", {"terms": [], "calls": []})
        self.app.records[rid] = {
            "id": rid,
            "path": run,
            "task": task,
            "readonly": False,
            "label": "test-fixture",
            "scenario": "normal",
            "created_at": "2026-09-21T00:00:00+00:00",
            "job": {"status": "idle", "error": None},
            "service": None,
        }
        return run

    def _create_valid_run(self):
        status, bootstrap = self._request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        city = bootstrap["cities"][0]
        body = {
            "query_id": bootstrap["queries"][0]["query_id"],
            "city_id": city["id"],
            "region_id": city["regions"][0],
            "scenario": "normal",
        }
        status, response = self._request(
            "POST",
            "/api/runs",
            body,
            headers={"X-Workbench-Token": bootstrap["csrf_token"]},
        )
        self.assertEqual(status, 202)
        self.assertIn("id", response)
        return response["id"], bootstrap["csrf_token"]

    def _wait_for_detail(self, rid, predicate, timeout=None):
        timeout = self.ASYNC_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            status, latest = self._request("GET", f"/api/runs/{rid}", timeout=2)
            if status == 200 and predicate(latest):
                return latest
            time.sleep(0.05)
        self.fail(f"异步运行在 {timeout}s 内未达到预期状态：{latest!r}")

    def test_host_origin_and_write_token_are_enforced(self):
        allowed_origin = f"http://{self.host}"
        status, bootstrap = self._request(
            "GET", "/api/bootstrap", headers={"Origin": allowed_origin}
        )
        self.assertEqual(status, 200)
        self.assertTrue(bootstrap["csrf_token"])

        status, payload = self._request(
            "GET", "/api/bootstrap", headers={"Host": "evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

        status, payload = self._request(
            "GET", "/api/bootstrap", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

        valid_body = {
            "query_id": "Q0001",
            "city_id": "330100",
            "region_id": "330100-A",
            "scenario": "normal",
        }
        for token in (None, "wrong-token"):
            headers = {} if token is None else {"X-Workbench-Token": token}
            status, payload = self._request("POST", "/api/runs", valid_body, headers=headers)
            self.assertEqual(status, 403)
            self.assertIn("error", payload)

        status, payload = self._request(
            "POST",
            "/api/runs",
            {**valid_body, "scenario": "not-a-scenario"},
            headers={"X-Workbench-Token": bootstrap["csrf_token"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_invalid_query_and_scenario_are_rejected_without_creating_run(self):
        valid = {
            "query_id": "Q0001",
            "city_id": "330100",
            "region_id": "330100-A",
            "scenario": "normal",
        }
        before = len(self.app.records)
        cases = [
            ("query_id", "Q-does-not-exist"),
            ("scenario", "not-a-scenario"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                status, payload = self._request(
                    "POST",
                    "/api/runs",
                    {**valid, field: value},
                    headers={"X-Workbench-Token": self.app.token},
                )
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
                self.assertEqual(len(self.app.records), before)

    def test_artifact_path_traversal_is_not_readable(self):
        self._add_run_fixture("fixture-run")
        secret = Path(self.tempdir.name) / "secret-outside-run.txt"
        secret.write_text("must never be returned", encoding="utf-8")

        status, payload = self._request(
            "GET",
            "/api/runs/fixture-run/artifact?name=..%2Fsecret-outside-run.txt",
        )
        self.assertEqual(status, 404)
        self.assertIn("error", payload)
        self.assertNotIn(b"must never be returned", json.dumps(payload).encode("utf-8"))

    def test_historical_run_is_read_only_for_writes(self):
        before = sorted(
            path.relative_to(self.history_root).as_posix()
            for path in self.history_root.rglob("*")
            if path.is_file()
        )
        status, detail = self._request("GET", "/api/runs/history-recorded-001")
        self.assertEqual(status, 200)
        self.assertTrue(detail["readonly"])

        status, payload = self._request(
            "POST",
            "/api/runs/history-recorded-001/decision",
            {"decision": {}},
            headers={"X-Workbench-Token": self.app.token},
        )
        self.assertEqual(status, 403)
        self.assertIn("error", payload)
        after = sorted(
            path.relative_to(self.history_root).as_posix()
            for path in self.history_root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(after, before)

    def test_detail_keeps_two_plans_for_the_same_offer(self):
        # Shape-only fixture: two real candidate dimensions, with no predicted decision.
        candidates = [
            {
                "candidate_id": "A00001",
                "term_id": "T0001",
                "offer_id": "O00001",
                "plan": {"ad_plan_id": "A00001"},
            },
            {
                "candidate_id": "A00999",
                "term_id": "T0001",
                "offer_id": "O00001",
                "plan": {"ad_plan_id": "A00999"},
            },
        ]
        self._add_run_fixture("same-offer-run", candidates)

        status, detail = self._request("GET", "/api/runs/same-offer-run")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["candidates"]), 2)
        self.assertEqual(
            {row["candidate_id"] for row in detail["candidates"]},
            {"A00001", "A00999"},
        )
        self.assertEqual(
            {row["plan"]["ad_plan_id"] for row in detail["candidates"]},
            {"A00001", "A00999"},
        )
        self.assertEqual({row["offer_id"] for row in detail["candidates"]}, {"O00001"})

    def test_prepare_stops_at_waiting_phase_and_bad_handoff_surfaces_error(self):
        rid, token = self._create_valid_run()
        waiting = self._wait_for_detail(
            rid,
            lambda detail: detail["job"]["status"] != "running",
        )
        self.assertEqual(waiting["job"]["status"], "idle")
        self.assertEqual(waiting["phase"], "awaiting_query")
        self.assertNotEqual(waiting["phase"], "complete")
        self.assertIsNone(waiting["result"])

        status, handoff = self._request("GET", f"/api/runs/{rid}/handoff")
        self.assertEqual(status, 200)
        self.assertEqual(handoff["handoff"]["phase"], "query")

        # Deliberately malformed fixture, not a model prediction: run_demo must reject it.
        status, accepted = self._request(
            "POST",
            f"/api/runs/{rid}/decision",
            {"decision": {"fixture": "malformed-handoff"}},
            headers={"X-Workbench-Token": token},
        )
        self.assertEqual(status, 202)
        self.assertEqual(accepted["id"], rid)

        failed = self._wait_for_detail(
            rid,
            lambda detail: detail["job"]["status"] == "error",
            timeout=10,
        )
        self.assertEqual(failed["phase"], "awaiting_query")
        self.assertEqual(failed["job"]["status"], "error")
        self.assertTrue(failed["job"]["error"])
        self.assertIn("Decision keys differ from handoff contract", failed["job"]["error"])


if __name__ == "__main__":
    unittest.main()
