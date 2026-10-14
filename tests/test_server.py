"""Integration and boundary tests for the native HTTP transport."""

from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import quote

from open_scion.server import MAX_BODY_BYTES, make_server
from open_scion.store import ScionError, Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.server = make_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def restart_server(self, **options):
        self.stop_server()
        self.server = make_server(self.store, **options)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for server state")
            time.sleep(0.005)

    def incomplete_upload(self, length=100):
        port = self.server.server_address[1]
        connection = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(connection.close)
        connection.sendall((f"POST /v1/blobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                            "Content-Type: application/octet-stream\r\n"
                            f"Content-Length: {length}\r\n\r\nx").encode())
        return connection

    def request(self, method, path, body=None, headers=None, *, raw=False):
        request_headers = dict(headers or {})
        if body is not None and not raw:
            body = json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            request_headers.setdefault("Content-Type", "application/octet-stream")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            result = response.read()
            if result and response.getheader("Content-Type", "").startswith("application/json"):
                result = json.loads(result)
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    def root_changeset(self, content=b"hello\nworld\n"):
        blob = self.store.put_blob(content)
        return self.store.create_changeset([], {"src/hello.txt": {"blob": blob,
                                                                 "mode": "100644"}},
                                           "Initial revision", "Ada")

    def assert_error(self, result, status, code=None):
        self.assertEqual(result[0], status, result)
        self.assertEqual(set(result[2]["error"]), {"code", "message", "details"})
        if code is not None:
            self.assertEqual(result[2]["error"]["code"], code)

    def test_health_and_default_headers(self):
        status, headers, result = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Connection"], "close")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertRegex(headers["X-Request-ID"], r"^[a-f0-9]{32}$")

    def test_readiness_is_public_lightweight_and_checks_schema(self):
        self.server.token = "readiness-token"
        with patch.object(self.store, "fsck", side_effect=AssertionError("Readiness must not scan objects")):
            self.assertEqual(self.request("GET", "/ready")[2], {"status": "ready"})
        connection = sqlite3.connect(self.store.db_path)
        try:
            connection.execute("UPDATE metadata SET value='unsupported' WHERE key='schema_version'")
            connection.commit()
        finally:
            connection.close()
        result = self.request("GET", "/ready")
        self.assert_error(result, 503, "not_ready")
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertNotIn("schema", json.dumps(result[2]))

    def test_readiness_rejects_missing_database_without_creating_it(self):
        self.store.db_path = self.store.db_path.with_name("absent.sqlite")
        self.assert_error(self.request("GET", "/ready"), 503, "not_ready")
        self.assertFalse(self.store.db_path.exists())

    def test_metrics_require_auth_and_count_rejected_requests(self):
        self.server.token = "metrics-token"
        self.assert_error(self.request("GET", "/v1/metrics"), 401)
        self.assert_error(self.request("GET", "/v1/unknown",
                                       headers={"Authorization": "Bearer metrics-token"}), 404)
        self.wait_until(lambda: sum(self.server.metrics()["statuses"].values()) == 2)
        status, _, metrics = self.request("GET", "/v1/metrics",
                                          headers={"Authorization": "Bearer metrics-token"})
        self.assertEqual(status, 200)
        self.assertEqual(metrics["requests"], 3)
        self.assertEqual(metrics["active"], 1)
        self.assertEqual(metrics["statuses"], {"401": 1, "404": 1})
        self.assertEqual(metrics["max_workers"], 32)
        self.assertEqual(metrics["request_timeout_seconds"], 15)
        self.assertGreaterEqual(metrics["uptime_seconds"], 0)
        self.assertFalse(metrics["draining"])

    def test_access_logs_redact_query_body_tokens_and_untrusted_request_ids(self):
        initial = self.root_changeset()
        self.server.token = "secret-bearer-token"
        headers = {"Authorization": "Bearer secret-bearer-token", "X-Request-ID": "untrusted-client-id"}
        with self.assertLogs("open_scion.access", level="INFO") as captured:
            first = self.request("GET", f"/v1/search?rev={initial['id']}&q=secret-query-value", headers=headers)
            second = self.request("POST", "/v1/changesets", {
                "parents": [initial["id"]], "changes": {},
                "message": "secret-body-value", "author": "Ada"}, headers=headers)
            self.wait_until(lambda: len(captured.records) == 2)
        logs = [json.loads(record.getMessage()) for record in captured.records]
        text = json.dumps(logs)
        for secret in ("secret-bearer-token", "secret-query-value", "secret-body-value", "untrusted-client-id"):
            self.assertNotIn(secret, text)
        self.assertEqual([record["path"] for record in logs], ["/v1/search", "/v1/changesets"])
        self.assertEqual([record["method"] for record in logs], ["GET", "POST"])
        self.assertEqual([record["status"] for record in logs], [200, 201])
        self.assertEqual([record["request_id"] for record in logs],
                         [first[1]["X-Request-ID"], second[1]["X-Request-ID"]])
        self.assertNotEqual(first[1]["X-Request-ID"], second[1]["X-Request-ID"])
        for record in logs:
            self.assertEqual(set(record), {"request_id", "method", "path", "status", "duration_ms", "remote_ip"})
            self.assertEqual(record["remote_ip"], "127.0.0.1")
            self.assertGreaterEqual(record["duration_ms"], 0)

    def test_saturation_returns_immediate_503_and_recovers_after_clients_close(self):
        self.restart_server(max_workers=2, request_timeout=3)
        clients = [self.incomplete_upload(), self.incomplete_upload()]
        self.wait_until(lambda: self.server.metrics()["active"] == 2)
        start = time.monotonic()
        with self.assertLogs("open_scion.access", level="INFO") as captured:
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: self.request("GET", "/health?secret=query-value"), range(4)))
            self.wait_until(lambda: len(captured.records) == 4)
        self.assertLess(time.monotonic() - start, 1.5)
        for result in results:
            self.assert_error(result, 503, "overloaded")
            self.assertEqual(result[1]["Retry-After"], "1")
            self.assertRegex(result[1]["X-Request-ID"], r"^[a-f0-9]{32}$")
        self.assertNotIn("query-value", " ".join(record.getMessage() for record in captured.records))
        self.assertEqual(self.server.metrics()["active"], 2)
        self.assertEqual(self.server.metrics()["requests"], 6)
        self.assertEqual(self.server.metrics()["statuses"], {"503": 4})
        for connection in clients:
            connection.close()
        self.wait_until(lambda: self.server.metrics()["active"] == 0)
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.store.stats()["blobs"], 0)

    def test_aggregate_body_budget_rejects_before_reading_and_recovers(self):
        self.restart_server(max_workers=4, request_timeout=3, max_inflight_bytes=10)
        connection = self.incomplete_upload(length=6)
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 6)
        start = time.monotonic()
        result = self.request("POST", "/v1/blobs", b"", raw=True, headers={"Content-Length": "6"})
        self.assert_error(result, 503, "overloaded")
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(result[1]["Retry-After"], "1")
        self.assertEqual(self.server.metrics()["inflight_body_bytes"], 6)
        status, _, metrics = self.request("GET", "/v1/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(metrics["max_inflight_bytes"], 10)
        self.assertEqual(metrics["inflight_body_bytes"], 6)
        connection.close()
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 0)
        self.assertEqual(self.request("POST", "/v1/blobs", b"123456", raw=True)[0], 201)
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 0)

    def test_body_budget_is_released_after_timeout_and_json_error(self):
        self.restart_server(max_workers=2, request_timeout=0.1, max_inflight_bytes=10)
        self.incomplete_upload(length=6)
        self.wait_until(lambda: self.server.metrics()["statuses"].get("408") == 1)
        self.assertEqual(self.server.metrics()["inflight_body_bytes"], 0)
        self.assert_error(self.request("POST", "/v1/refs/main", b"bad", raw=True,
                                       headers={"Content-Type": "application/json"}), 400)
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 0)
        self.assertEqual(self.request("POST", "/v1/blobs", b"1234567890", raw=True)[0], 201)

    def test_expect_continue_reserves_body_budget_before_accepting_upload(self):
        self.restart_server(max_workers=3, request_timeout=3, max_inflight_bytes=10)
        slow = self.incomplete_upload(length=6)
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 6)
        port = self.server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall((f"POST /v1/blobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                                "Content-Type: application/octet-stream\r\n"
                                "Content-Length: 6\r\nExpect: 100-continue\r\n\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())["error"]["code"], "overloaded")
            response.close()
        slow.close()
        self.wait_until(lambda: self.server.metrics()["inflight_body_bytes"] == 0)

    def test_header_timeout_releases_worker_and_counts_timeout(self):
        self.restart_server(max_workers=1, request_timeout=0.1)
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=3) as connection:
            connection.sendall(b"GET /health HTTP/1.1\r\n")
            self.wait_until(lambda: self.server.metrics()["statuses"].get("408") == 1)
        self.wait_until(lambda: self.server.metrics()["active"] == 0)
        self.assertEqual(self.request("GET", "/health")[0], 200)

    def test_shutdown_drains_an_inflight_upload(self):
        self.restart_server(max_workers=1, request_timeout=1)
        connection = self.incomplete_upload(length=2)
        self.wait_until(lambda: self.server.metrics()["active"] == 1)
        shutdown = threading.Thread(target=self.server.shutdown)
        shutdown.start()
        self.wait_until(lambda: self.server.metrics()["draining"])
        self.assertFalse(self.server.readiness())
        connection.sendall(b"y")
        response = http.client.HTTPResponse(connection)
        response.begin()
        self.assertEqual(response.status, 201)
        oid = json.loads(response.read())["id"]
        response.close()
        shutdown.join(timeout=2)
        self.assertFalse(shutdown.is_alive())
        self.assertEqual(self.server.metrics()["active"], 0)
        self.assertEqual(self.store.get_blob(oid), b"xy")

    def test_shutdown_interrupts_slow_drip_at_drain_deadline(self):
        self.restart_server(max_workers=1, request_timeout=0.15)
        connection = self.incomplete_upload(length=1000)
        self.wait_until(lambda: self.server.metrics()["active"] == 1)
        stop = threading.Event()

        def drip():
            while not stop.wait(0.03):
                try:
                    connection.sendall(b"x")
                except OSError:
                    return

        writer = threading.Thread(target=drip, daemon=True)
        writer.start()
        try:
            start = time.monotonic()
            self.server.shutdown()
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(self.server.metrics()["active"], 0)
            self.assertEqual(self.store.stats()["blobs"], 0)
        finally:
            stop.set()
            writer.join(timeout=1)

    def test_ref_log_records_are_exposed_with_bounded_query_validation(self):
        initial = self.root_changeset()
        self.store.update_ref("main", initial["id"], None)
        status, _, records = self.request("GET", "/v1/ref-log?name=main&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "main")
        self.assertIsNone(records[0]["old"])
        self.assertEqual(records[0]["new"], initial["id"])
        for path in ("/v1/ref-log", "/v1/ref-log?name=", "/v1/ref-log?name=main&limit=1001"):
            self.assert_error(self.request("GET", path), 400)

    def test_worker_and_timeout_configuration_validation(self):
        for workers in (0, 1025, 1.5, True, "32"):
            with self.subTest(workers=workers), self.assertRaises(ScionError):
                make_server(self.store, max_workers=workers)
        for timeout in (0, -1, 301, float("nan"), float("inf"), True, "15"):
            with self.subTest(timeout=timeout), self.assertRaises(ScionError):
                make_server(self.store, request_timeout=timeout)
        for size in (0, -1, 1.5, True, "1024"):
            with self.subTest(size=size), self.assertRaises(ScionError):
                make_server(self.store, max_inflight_bytes=size)

    def test_blob_round_trip_and_immutable_cache(self):
        content = b"\x00\xff\x80binary\n"
        status, _, result = self.request("POST", "/v1/blobs", content, raw=True)
        self.assertEqual(status, 201)
        oid = result["id"]
        status, headers, result = self.request("GET", "/v1/blobs/" + oid)
        self.assertEqual(status, 200)
        self.assertEqual(result, content)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertIn("private", headers["Cache-Control"])
        status, _, result = self.request("GET", "/v1/blobs/" + oid,
                                         headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(result, b"")

    def test_empty_blob_is_supported(self):
        status, _, result = self.request("POST", "/v1/blobs", b"", raw=True)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("GET", "/v1/blobs/" + result["id"])[2], b"")

    def test_changeset_tree_ref_and_compare_and_swap(self):
        blob = self.store.put_blob("hello 世界\n".encode())
        body = {"parents": [], "changes": {"docs/你好.txt": {"blob": blob, "mode": "100644"}},
                "message": "First", "author": "Ada"}
        status, _, revision = self.request("POST", "/v1/changesets", body)
        self.assertEqual(status, 201)
        oid = revision["id"]
        status, headers, result = self.request("GET", "/v1/changesets/" + oid)
        self.assertEqual(status, 200)
        self.assertEqual(result["id"], oid)
        self.assertIn("immutable", headers["Cache-Control"])
        status, _, _ = self.request("POST", "/v1/refs/main", {"new": oid, "expected": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/v1/refs")[2], {"main": oid})
        self.assertEqual(self.request("GET", "/v1/tree?rev=main")[2], body["changes"])
        self.assertEqual(self.request("GET", "/v1/tree?rev=main&prefix=docs")[2], body["changes"])
        status, headers, _ = self.request("GET", "/v1/changesets/main")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assert_error(self.request("POST", "/v1/refs/main", {"new": oid, "expected": None}), 409)
        self.assertEqual(self.request("GET", "/v1/stats")[0], 200)

    def test_read_operations_are_wired_to_explicit_routes(self):
        initial = self.root_changeset()
        oid = initial["id"]
        self.assertEqual(self.request("GET", f"/v1/log?rev={oid}&limit=1")[2][0]["id"], oid)
        self.assertEqual(self.request("GET", f"/v1/diff?base={oid}&target={oid}")[2], [])
        self.assertEqual(self.request("GET", f"/v1/merge-base?left={oid}&right={oid}")[2],
                         {"base": oid})
        blame = self.request("GET", f"/v1/blame?rev={oid}&path=src/hello.txt")
        self.assertEqual(blame[0], 200, blame)
        self.assertEqual([item["author"] for item in blame[2]], ["Ada", "Ada"])
        search = self.request("GET", f"/v1/search?rev={oid}&q=hello&limit=1&prefix=src")
        self.assertEqual(search[0], 200, search)
        self.assertEqual(search[2][0]["path"], "src/hello.txt")

    def test_operation_posts_validate_and_pass_arguments(self):
        operations = Mock()
        self.server.operations = operations
        operations.merge.return_value = {"id": "merge-result"}
        operations.rebase.return_value = {"id": "rebase-result"}
        operations.land.return_value = {"ref": {"name": "main", "target": "land-result"}}
        merge = {"base": None, "ours": "a", "theirs": "b", "message": "Merge", "author": "Ada"}
        self.assertEqual(self.request("POST", "/v1/merge", merge)[0], 201)
        operations.merge.assert_called_once_with(**merge)
        rebase = {"revision": "a", "onto": "b", "message": None}
        self.assertEqual(self.request("POST", "/v1/rebase", rebase)[0], 201)
        operations.rebase.assert_called_once_with(**rebase)
        land = {"revision": "a", "ref": "main", "expected": None}
        self.assertEqual(self.request("POST", "/v1/land", land)[0], 200)
        operations.land.assert_called_once_with(**land)

    def test_conflict_details_survive_transport(self):
        self.server.operations = Mock()
        self.server.operations.rebase.side_effect = ScionError(
            "Conflicting paths", "conflict", 409, {"conflicts": ["src/a.py"]})
        result = self.request("POST", "/v1/rebase", {"revision": "a", "onto": "b"})
        self.assert_error(result, 409, "conflict")
        self.assertEqual(result[2]["error"]["details"], {"conflicts": ["src/a.py"]})

    def test_unknown_routes_and_methods_never_dispatch_attributes(self):
        for path in ("/v1/fsck", "/v1/__dict__", "/v1/refs/", "/v1/blobs/", "/"):
            with self.subTest(path=path):
                self.assert_error(self.request("GET", path), 404)
        self.assert_error(self.request("POST", "/v1/unknown", {}), 404)
        self.assert_error(self.request("DELETE", "/v1/refs/main"), 405)
        self.assert_error(self.request("OPTIONS", "/v1/blobs"), 405)
        result = self.request("POST", "/health", {})
        self.assert_error(result, 405)
        self.assertEqual(result[1]["Allow"], "GET")
        result = self.request("GET", "/v1/merge")
        self.assert_error(result, 405)
        self.assertEqual(result[1]["Allow"], "POST")

    def test_auth_covers_known_and_unknown_versioned_routes(self):
        self.server.token = "test-secret"
        self.assertEqual(self.request("GET", "/health")[0], 200)
        for path in ("/v1/refs", "/v1/unknown", "/v1"):
            self.assert_error(self.request("GET", path), 401, "unauthorized")
        self.assert_error(self.request("GET", "/v1/refs",
                                       headers={"Authorization": "Bearer wrong"}), 401)
        status, _, refs = self.request("GET", "/v1/refs",
                                       headers={"Authorization": "Bearer test-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(refs, {})
        result = self.request("POST", "/v1/blobs", b"unauthorized data", raw=True)
        self.assert_error(result, 401)
        self.assertEqual(result[1]["WWW-Authenticate"], "Bearer")

    def test_nonloopback_requires_token_before_binding(self):
        for host in ("0.0.0.0", "::", "192.0.2.1", "example.invalid"):
            with self.subTest(host=host), self.assertRaises(ScionError):
                make_server(self.store, host=host)
        for token in ("", "secret\n", "two words", 123, "\x00secret", "nonascii-你好"):
            with self.subTest(token=token), self.assertRaises(ScionError):
                make_server(self.store, token=token)

    def test_csrf_and_dns_rebinding_are_rejected(self):
        self.assert_error(self.request("GET", "/v1/refs", headers={"Host": "attacker.example"}), 403)
        self.assert_error(self.request("POST", "/v1/blobs", b"data", raw=True,
                                       headers={"Origin": "https://attacker.example"}), 403)
        self.assert_error(self.request("POST", "/v1/blobs", b"data", raw=True,
                                       headers={"Origin": "null"}), 403)
        port = self.server.server_address[1]
        self.assertEqual(self.request("GET", "/v1/refs",
                                      headers={"Origin": f"http://127.0.0.1:{port}"})[0], 200)

    def test_body_limit_is_checked_before_reading(self):
        result = self.request("POST", "/v1/blobs", b"", raw=True,
                              headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assert_error(result, 413, "too_large")

    def test_media_types_and_body_shape(self):
        self.assert_error(self.request("POST", "/v1/refs/main", b"{}", raw=True), 415)
        self.assert_error(self.request("POST", "/v1/blobs", b"data", raw=True,
                                       headers={"Content-Type": "text/plain"}), 415)
        for body in ([], "string", None, 42):
            raw_body = json.dumps(body).encode()
            with self.subTest(body=body):
                result = self.request("POST", "/v1/refs/main", raw_body, raw=True,
                                      headers={"Content-Type": "application/json"})
                self.assert_error(result, 400)
        for raw_body in (b'{"new": "a", "new": "b", "expected": null}', b"{bad}",
                         b'{"new": NaN}', b"\xff", b'{"new": "\\ud800", "expected": null}'):
            with self.subTest(raw_body=raw_body):
                self.assert_error(self.request("POST", "/v1/refs/main", raw_body, raw=True,
                                               headers={"Content-Type": "application/json"}), 400)

    def test_json_schema_validation(self):
        invalid = [
            ("/v1/refs/main", {"new": "a"}),
            ("/v1/refs/main", {"new": 42, "expected": None}),
            ("/v1/refs/main", {"new": "a", "expected": None, "force": True}),
            ("/v1/rebase", {"revision": "a", "onto": []}),
            ("/v1/land", {"revision": "a", "ref": "main", "expected": False}),
            ("/v1/changesets", {"parents": {}, "changes": {}, "message": "", "author": "Ada"}),
            ("/v1/changesets", {"parents": [], "changes": [], "message": "", "author": "Ada"}),
            ("/v1/changesets", {"parents": [], "changes": {"file": {"blob": "a", "mode": "120000"}},
                                "message": "", "author": "Ada"}),
        ]
        for path, body in invalid:
            with self.subTest(path=path, body=body):
                self.assert_error(self.request("POST", path, body), 400)

    def test_invalid_query_parameters(self):
        for path in ("/v1/tree", "/v1/tree?rev=", "/v1/refs?unknown=1",
                     "/v1/tree?rev=a&rev=b", "/v1/log?rev=a&limit=-1",
                     "/v1/log?rev=a&limit=0", "/v1/search?rev=a&q=x&limit=1001",
                     "/v1/log?rev=a&limit=1.5", "/v1/blame?rev=a", "/v1/tree?rev=%FF",
                     "/v1/tree?rev=%GG"):
            with self.subTest(path=path):
                self.assert_error(self.request("GET", path), 400)

    def test_invalid_path_and_absent_object_are_client_errors(self):
        initial = self.root_changeset()
        result = self.request("GET", f"/v1/blame?rev={initial['id']}&path=" + quote("../secret"))
        self.assert_error(result, 400)
        self.assert_error(self.request("GET", "/v1/blobs/" + "a" * 64), 404)

    def test_unexpected_errors_do_not_leak_private_details(self):
        with patch.object(self.store, "stats", side_effect=RuntimeError("private filesystem path")):
            with self.assertLogs("open_scion.server", level="ERROR"):
                result = self.request("GET", "/v1/stats")
        self.assert_error(result, 500, "internal")
        self.assertNotIn("private", json.dumps(result[2]))

    def test_duplicate_content_length_and_chunked_requests_are_rejected(self):
        port = self.server.server_address[1]
        for extra in ("Content-Length: 0\r\nContent-Length: 0\r\n",
                      "Transfer-Encoding: chunked\r\n", "Content-Length: -1\r\n"):
            with self.subTest(extra=extra), socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                request = (f"POST /v1/blobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Content-Type: application/octet-stream\r\n" + extra + "\r\n")
                sock.sendall(request.encode())
                with sock.makefile("rb") as stream:
                    response = stream.read()
                self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)


if __name__ == "__main__":
    unittest.main()
