"""Integration and boundary tests for the native HTTP transport."""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
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
