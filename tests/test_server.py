"""HTTP contract tests exercise real TCP requests and a temporary SQLite store."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from caseflow.runtime import Runtime
from caseflow.server import MAX_BODY, make_server
from caseflow.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "http.sqlite")
        self.store.seed()
        self.release = threading.Event()
        self.server = make_server(self.store, lambda: Runtime(self.store), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=4)
        request_headers = {"Host": self.server.authority}
        body = raw
        if data is not None:
            body = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            if "application/json" in response.getheader("Content-Type", ""):
                payload = json.loads(payload)
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def create(self, **extra):
        return self.request("POST", "/api/runs", {"case_id": "refund_valid", "mode": "demo", **extra})

    def wait_run(self, run_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            run = self.store.get_run(run_id)
            if run["status"] not in {"ready", "running"}:
                return run
            time.sleep(0.02)
        self.fail("Agent did not finish in four seconds")

    def blocking_factory(self):
        release = self.release

        class BlockingRuntime:
            provider = None

            def run(self, run_id):
                release.wait(timeout=8)

        return BlockingRuntime()

    def test_static_page_and_read_contract(self):
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("CaseFlow", page)
        self.assertIn("演示模式不调用大模型", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, payload, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"cases", "runs", "proposals", "outbox", "ledger"})
        self.assertGreater(len(payload["cases"]), 5)
        self.assertEqual(self.request("GET", "/../../CONTRACT.md")[0], 404)

    def test_host_boundary_is_exact_including_port(self):
        for bad in ["evil.example", "127.0.0.1", self.server.authority + ".evil", "localhost:" + str(self.server.server_port)]:
            with self.subTest(host=bad):
                status, _, _ = self.request("POST", "/api/runs", {"case_id": "refund_valid", "mode": "demo"}, {"Host": bad})
                self.assertEqual(status, 403)
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0], 403)

    def test_origin_boundary_rejects_cross_origin_and_null(self):
        for origin in ["https://evil.example", "null", self.server.origin + ".evil", "https://" + self.server.authority]:
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/runs", {"case_id": "refund_valid", "mode": "demo"}, {"Origin": origin})[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "refund_valid", "mode": "demo"}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/runs", headers={"Origin": "https://evil.example"})[0], 405)
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "refund_valid", "mode": "demo"}, {"Origin": self.server.origin})[0], 202)

    def test_json_body_limits_and_types(self):
        self.assertEqual(self.request("POST", "/api/runs", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/runs", raw=b"x" * (MAX_BODY + 1), headers={"Content-Type": "application/json"})[0], 413)
        for raw in [b"[]", b"null", b"{", b'{"mode":"demo","mode":"live"}', b'{"max_steps":NaN}']:
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/runs", raw=raw, headers={"Content-Type": "application/json"})[0], 400)
        for budget in [True, False, 1.5, "12", 0, 33]:
            with self.subTest(budget=budget):
                self.assertEqual(self.create(max_steps=budget)[0], 400)
        self.assertEqual(self.create(tenant_id="foreign")[0], 400)
        self.assertEqual(self.create(case_id="../escape")[0], 400)
        self.assertEqual(self.create(mode="unknown")[0], 400)
        self.assertEqual(self.request("POST", "/api/proposals/no_such/decision", {"approved": "true"})[0], 400)
        self.assertEqual(self.request("POST", "/api/proposals/no_such/decision", {"approved": 1})[0], 400)
        self.assertEqual(self.store.list_runs(), [])

    def test_unconfigured_live_rejected_before_run_created(self):
        self.assertEqual(self.create(mode="live")[0], 409)
        self.assertEqual(self.store.list_runs(), [])

    def test_capacity_is_checked_before_creation(self):
        self.server.runtime_factory = self.blocking_factory
        for _ in range(8):
            self.assertEqual(self.create()[0], 202)
        self.assertEqual(self.create()[0], 429)
        self.assertEqual(len(self.store.list_runs()), 8)
        self.assertEqual(len(self.server.active), 8)
        self.release.set()
        deadline = time.monotonic() + 3
        while self.server.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.create()[0], 202)

    def test_duplicate_resume_is_rejected_and_budget_is_not_reset(self):
        self.server.runtime_factory = self.blocking_factory
        run = self.store.create_run("refund_valid", "demo", 3)
        run["state"]["steps"] = 2
        run = self.store.save_run(run["id"], run["revision"], run["state"], "interrupted")
        path = "/api/runs/" + run["id"] + "/resume"
        self.assertEqual(self.request("POST", path, {})[0], 202)
        self.assertEqual(self.request("POST", path, {})[0], 409)
        self.assertEqual(self.store.get_run(run["id"])["state"]["steps"], 2)
        self.assertEqual(self.store.get_run(run["id"])["state"]["max_steps"], 3)

    def test_terminal_business_result_cannot_resume(self):
        run = self.store.create_run("refund_valid", "demo")
        run["state"]["result"] = {"terminal_status": "awaiting_approval"}
        self.store.save_run(run["id"], run["revision"], run["state"], "error")
        self.assertEqual(self.request("POST", "/api/runs/" + run["id"] + "/resume", {})[0], 409)

    def test_budget_exhausted_cannot_resume(self):
        status, result, _ = self.create(max_steps=1)
        self.assertEqual(status, 202)
        run = self.wait_run(result["run"]["id"])
        self.assertEqual(run["status"], "budget_exhausted")
        self.assertEqual(self.request("POST", "/api/runs/" + run["id"] + "/resume", {})[0], 409)

    def test_roundtrip_approval_crash_and_idempotent_retry(self):
        status, result, _ = self.create()
        self.assertEqual(status, 202)
        run = self.wait_run(result["run"]["id"])
        self.assertEqual(run["status"], "awaiting_approval")
        status, detail, _ = self.request("GET", "/api/runs/" + run["id"])
        self.assertEqual(status, 200)
        self.assertTrue(detail["events"])
        proposal = detail["proposals"][0]
        path = "/api/proposals/" + proposal["id"] + "/decision"
        self.assertEqual(self.request("POST", path, {"approved": True})[0], 200)
        self.assertEqual(self.request("POST", "/api/dispatch-crash", {})[0], 409)
        self.assertEqual(len(self.store.ledger()), 1)
        self.assertEqual(self.store.outbox()[0]["status"], "pending")
        self.assertEqual(self.request("POST", "/api/dispatch", {})[0], 200)
        self.assertEqual(self.request("POST", "/api/dispatch", {})[0], 200)
        self.assertEqual(len(self.store.ledger()), 1)
        self.assertEqual(self.store.get_run(run["id"])["status"], "completed")
        self.assertEqual(self.store.outbox()[0]["status"], "delivered")

    def test_cannot_bind_public_address(self):
        with self.assertRaises(ValueError):
            make_server(self.store, lambda: Runtime(self.store), host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
