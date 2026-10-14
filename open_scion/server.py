"""A bounded, single-tenant HTTP transport for Open Scion."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import re
import signal
import socket
import sqlite3
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .operations import Operations
from .store import ScionError, Store


MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_INFLIGHT_BYTES = 64 * 1024 * 1024
MAX_URL_BYTES = 8192
REQUEST_TIMEOUT = 15
MAX_RESULT_LIMIT = 1000
_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_LOGGER = logging.getLogger(__name__)
_ACCESS_LOGGER = logging.getLogger("open_scion.access")
_PROCESS_STARTED = time.monotonic()


def _access_path(target: str) -> str:
    """Keep request queries, URL credentials, and fragments out of access logs."""
    try:
        return urlsplit(target[:MAX_URL_BYTES]).path or "/"
    except ValueError:
        return "<invalid>"


def _access_method(method: str) -> str:
    return method if method in {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE",
                                "OPTIONS", "CONNECT", "TRACE"} else "UNKNOWN"


def _allowed_methods(path: str) -> tuple[str, ...]:
    if path in {"/health", "/ready", "/v1/metrics", "/v1/ref-log",
                "/v1/refs", "/v1/stats", "/v1/tree", "/v1/log", "/v1/diff",
                "/v1/merge-base", "/v1/blame", "/v1/search"} or any(
            path.startswith(prefix) and len(path) > len(prefix)
            for prefix in ("/v1/blobs/", "/v1/changesets/")):
        return ("GET",)
    if path in {"/v1/blobs", "/v1/changesets", "/v1/merge", "/v1/rebase", "/v1/land"} or (
            path.startswith("/v1/refs/") and len(path) > len("/v1/refs/")):
        return ("POST",)
    return ()


def _loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        if host.lower() != "localhost":
            return False
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        return bool(addresses) and all(
            ipaddress.ip_address(item[4][0]).is_loopback for item in addresses
        )


class ScionHTTPServer(ThreadingHTTPServer):
    """Bound worker admission, account for every connection, and drain on close."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, store: Store, host: str, port: int, token: str | None,
                 max_workers: int, request_timeout: float, max_inflight_bytes: int):
        self.store = store
        self.operations = Operations(store)
        self.token = token
        self.loopback = _loopback_host(host)
        self.max_workers = max_workers
        self.request_timeout = float(request_timeout)
        self.max_inflight_bytes = max_inflight_bytes
        self._capacity = threading.BoundedSemaphore(max_workers)
        self._state = threading.Condition()
        self._active_sockets: set[socket.socket] = set()
        self._request_count = 0
        self._inflight_body_bytes = 0
        self._statuses: Counter[str] = Counter()
        self._draining = False
        self._drain_deadline: float | None = None
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), ScionRequestHandler)

    def metrics(self) -> dict:
        """Status is the only metric label; paths and client identities are never labels."""
        with self._state:
            return {"active": len(self._active_sockets), "requests": self._request_count,
                    "statuses": dict(sorted(self._statuses.items())),
                    "uptime_seconds": round(time.monotonic() - _PROCESS_STARTED, 3),
                    "max_workers": self.max_workers,
                    "inflight_body_bytes": self._inflight_body_bytes,
                    "max_inflight_bytes": self.max_inflight_bytes,
                    "request_timeout_seconds": self.request_timeout, "draining": self._draining}

    def reserve_body(self, size: int) -> bool:
        with self._state:
            if size > self.max_inflight_bytes - self._inflight_body_bytes:
                return False
            self._inflight_body_bytes += size
            return True

    def release_body(self, size: int) -> None:
        with self._state:
            self._inflight_body_bytes -= size

    def completed(self, *, request_id: str, method: str, path: str, status: int,
                  started: float, remote_ip: str) -> None:
        status = status if 100 <= status <= 599 else 500
        with self._state:
            self._statuses[str(status)] += 1
        record = {"request_id": request_id, "method": _access_method(method),
                  "path": _access_path(path), "status": status,
                  "duration_ms": round((time.monotonic() - started) * 1000, 3),
                  "remote_ip": remote_ip}
        _ACCESS_LOGGER.info(json.dumps(record, ensure_ascii=True, separators=(",", ":")))

    def _reject_busy(self, request: socket.socket, address: tuple) -> None:
        started, request_id = time.monotonic(), uuid.uuid4().hex
        method, path = "UNKNOWN", "<unread>"
        try:
            request.setblocking(False)
            try:
                line = request.recv(MAX_URL_BYTES + 32, socket.MSG_PEEK).split(b"\r\n", 1)[0]
                parts = line.decode("iso-8859-1").split(" ")
                if len(parts) >= 3:
                    method, path = parts[0], parts[1]
            except (BlockingIOError, OSError):
                pass
            data = b'{"error":{"code":"overloaded","message":"Server is busy; retry later","details":null}}'
            response = ("HTTP/1.1 503 Service Unavailable\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        f"Content-Length: {len(data)}\r\n"
                        f"X-Request-ID: {request_id}\r\n"
                        "Retry-After: 1\r\nCache-Control: no-store\r\n"
                        "X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n")
            request.settimeout(min(self.request_timeout, 0.1))
            request.sendall(response.encode("ascii") + data)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)
            self.completed(request_id=request_id, method=method, path=path, status=503,
                           started=started, remote_ip=address[0])

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        with self._state:
            self._request_count += 1
            admitted = not self._draining and self._capacity.acquire(blocking=False)
            if admitted:
                self._active_sockets.add(request)
        if not admitted:
            self._reject_busy(request, client_address)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release_worker(request)
            self._reject_busy(request, client_address)

    def _release_worker(self, request: socket.socket) -> None:
        with self._state:
            self._active_sockets.discard(request)
            self._capacity.release()
            self._state.notify_all()

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_worker(request)

    def readiness(self) -> bool:
        """Probe readable SQLite schema without scanning or repairing stored objects."""
        with self._state:
            if self._draining:
                return False
        try:
            uri = self.store.db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=min(self.request_timeout, 1))
            try:
                connection.execute("PRAGMA query_only=ON")
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version != ("1",):
                    return False
                connection.execute("SELECT id,data,tree,generation FROM changesets LIMIT 0")
                connection.execute("SELECT name,target FROM refs LIMIT 0")
                connection.execute("SELECT sequence,name,old_target,new_target,created_at FROM ref_audit LIMIT 0")
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return False
        return True

    def _drain(self) -> None:
        with self._state:
            self._draining = True
            if self._drain_deadline is None:
                self._drain_deadline = time.monotonic() + self.request_timeout
            while self._active_sockets:
                remaining = self._drain_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
            pending = tuple(self._active_sockets)
        for request in pending:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        with self._state:
            self._state.wait_for(lambda: not self._active_sockets, timeout=1)

    def shutdown(self) -> None:
        with self._state:
            self._draining = True
        super().shutdown()
        self._drain()

    def server_close(self) -> None:
        with self._state:
            self._draining = True
        super().server_close()
        self._drain()


class ScionRequestHandler(BaseHTTPRequestHandler):
    """Only documented endpoints are dispatched; no reflective RPC is exposed."""

    server: ScionHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "OpenScion"
    sys_version = ""

    def setup(self) -> None:
        self.request_id = uuid.uuid4().hex
        self._started = time.monotonic()
        self._status: int | None = None
        self._reserved_body_bytes: int | None = None
        super().setup()
        self.connection.settimeout(self.server.request_timeout)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def log_error(self, fmt: str, *args: Any) -> None:
        if any(isinstance(arg, TimeoutError) for arg in args):
            self._status = 408

    def handle(self) -> None:
        try:
            super().handle()
        except TimeoutError:
            self._status = 408
            self.close_connection = True
        except (ConnectionError, OSError):
            self._status = 499
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except (OSError, TimeoutError):
            self._status = 499
        finally:
            if self._reserved_body_bytes is not None:
                self.server.release_body(self._reserved_body_bytes)
            self.server.completed(request_id=self.request_id, method=getattr(self, "command", ""),
                                  path=getattr(self, "path", "<unread>"), status=self._status or 499,
                                  started=self._started, remote_ip=self.client_address[0])

    def _send(self, status: int, body: Any = None, *, raw: bool = False,
              headers: dict[str, str] | None = None) -> None:
        data = body if raw else json.dumps(body, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")
        if status in (204, 304):
            data = b""
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else
                         "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Cache-Control", (headers or {}).get("Cache-Control", "no-store"))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            if name != "Cache-Control":
                self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD" and data:
            self.wfile.write(data)

    def _error(self, error: ScionError) -> None:
        headers = {}
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if error.status == 405:
            path, _ = self._target()
            headers["Allow"] = ", ".join(_allowed_methods(path))
        if error.code == "overloaded":
            headers["Retry-After"] = "1"
        self._send(error.status, {"error": {"code": error.code,
                                           "message": str(error),
                                           "details": error.details}}, headers=headers)

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        self._send(code, {"error": {"code": "http_error", "message": message or
                                    self.responses.get(code, ("HTTP error",))[0],
                                    "details": None}})

    def _authenticate(self, path: str) -> None:
        if self.server.token is None or not (path == "/v1" or path.startswith("/v1/")):
            return
        provided = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.token
        if len(provided) != 1 or not hmac.compare_digest(
                provided[0].encode("utf-8"), expected.encode("utf-8")):
            raise ScionError("A valid bearer token is required", "unauthorized", 401)

    def _check_origin(self) -> None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) > 1:
            raise ScionError("Multiple Host headers are not allowed")
        host = hosts[0] if hosts else ""
        if self.request_version == "HTTP/1.1" and not host:
            raise ScionError("Host header is required")
        if self.server.loopback and self.server.token is None and host:
            try:
                parsed = urlsplit("http://" + host)
                safe = (not parsed.username and not parsed.password and not parsed.path
                        and not parsed.query and not parsed.fragment
                        and parsed.port == self.server.server_address[1]
                        and parsed.hostname is not None and _loopback_host(parsed.hostname))
            except ValueError:
                safe = False
            if not safe:
                raise ScionError("Host must identify the local server", "forbidden", 403)
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return
        if len(origins) != 1 or origins[0] not in ("http://" + host, "https://" + host):
            raise ScionError("Cross-origin requests are not allowed", "forbidden", 403)

    def _target(self) -> tuple[str, dict[str, str]]:
        if len(self.path) > MAX_URL_BYTES:
            raise ScionError("Request URL is too long", "too_large", 414)
        if _BAD_PERCENT.search(self.path):
            raise ScionError("Invalid percent encoding in URL")
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
                raise ValueError("An origin-form request target is required")
            path = unquote(parsed.path, encoding="utf-8", errors="strict")
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16,
                              encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise ScionError("Invalid request URL") from exc
        if any(len(items) != 1 for items in values.values()):
            raise ScionError("Query parameters must not be repeated")
        if any(ord(char) < 32 for char in path):
            raise ScionError("Control characters are not allowed in paths")
        return path, {key: value[0] for key, value in values.items()}

    def _body_length(self) -> int:
        if self.headers.get_all("Transfer-Encoding", []):
            raise ScionError("Transfer-Encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise ScionError("Content-Length is required", "length_required", 411)
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise ScionError("Invalid Content-Length")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise ScionError("Request body exceeds 16 MiB", "too_large", 413)
        return length

    def handle_expect_100(self) -> bool:
        try:
            path, _ = self._target()
            self._authenticate(path)
            self._check_origin()
            self._reserve_body(self._body_length())
        except ScionError as exc:
            self._error(exc)
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def _reserve_body(self, size: int) -> None:
        if self._reserved_body_bytes is not None:
            return
        if not self.server.reserve_body(size):
            raise ScionError("Request body capacity is full; retry later", "overloaded", 503)
        self._reserved_body_bytes = size

    def _body(self, content_type: str) -> bytes:
        length = self._body_length()
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != content_type:
            raise ScionError(f"Content-Type must be {content_type}", "unsupported_media_type", 415)
        self._reserve_body(length)
        try:
            data = self.rfile.read(length)
        except (TimeoutError, socket.timeout) as exc:
            raise ScionError("Timed out reading request body", "timeout", 408) from exc
        if len(data) != length:
            raise ScionError("Request body ended before Content-Length bytes arrived")
        return data

    def _json_body(self) -> dict[str, Any]:
        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON keys")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise ValueError("Nonfinite JSON number: " + value)

        try:
            result = json.loads(self._body("application/json").decode("utf-8"),
                                object_pairs_hook=unique_keys,
                                parse_constant=reject_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, ScionError):
                raise
            raise ScionError("Request body must be a valid UTF-8 JSON object") from exc
        if not isinstance(result, dict):
            raise ScionError("Request body must be a JSON object")
        return result

    @staticmethod
    def _fields(values: dict[str, Any], required: tuple[str, ...] = (),
                optional: tuple[str, ...] = ()) -> None:
        missing = set(required) - values.keys()
        unknown = values.keys() - set(required) - set(optional)
        if missing:
            raise ScionError("Missing fields: " + ", ".join(sorted(missing)))
        if unknown:
            raise ScionError("Unknown fields: " + ", ".join(sorted(unknown)))

    @staticmethod
    def _text(values: dict[str, Any], name: str, *, nullable: bool = False,
              empty: bool = False) -> None:
        if name not in values or nullable and values[name] is None:
            return
        if not isinstance(values[name], str) or not empty and not values[name]:
            raise ScionError(f"{name} must be " + ("a string" if empty else "a nonempty string"))
        if any(0xD800 <= ord(char) <= 0xDFFF for char in values[name]):
            raise ScionError(f"{name} contains an invalid Unicode surrogate")

    @staticmethod
    def _limit(query: dict[str, str], default: int) -> int:
        value = query.get("limit", str(default))
        if not re.fullmatch(r"[0-9]{1,4}", value) or not 1 <= int(value) <= MAX_RESULT_LIMIT:
            raise ScionError(f"limit must be an integer from 1 to {MAX_RESULT_LIMIT}")
        return int(value)

    def _immutable(self, oid: str) -> dict[str, str]:
        return {"Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{oid}"'}

    def _get(self, path: str, query: dict[str, str]) -> None:
        store = self.server.store
        ops = self.server.operations
        if path == "/health":
            self._fields(query)
            self._send(200, {"status": "ok"})
        elif path == "/ready":
            self._fields(query)
            if not self.server.readiness():
                raise ScionError("Repository is not ready", "not_ready", 503)
            self._send(200, {"status": "ready"})
        elif path == "/v1/metrics":
            self._fields(query)
            self._send(200, self.server.metrics())
        elif path == "/v1/ref-log":
            self._fields(query, ("name",), ("limit",))
            self._text(query, "name")
            self._send(200, store.ref_log(query["name"], self._limit(query, 50)))
        elif path == "/v1/refs":
            self._fields(query)
            self._send(200, store.list_refs())
        elif path == "/v1/stats":
            self._fields(query)
            self._send(200, store.stats())
        elif path.startswith("/v1/blobs/") and path[10:]:
            self._fields(query)
            oid = path[10:]
            data = store.get_blob(oid)
            headers = self._immutable(oid)
            if self.headers.get("If-None-Match") in ("*", headers["ETag"]):
                self._send(304, headers=headers)
            else:
                self._send(200, data, raw=True, headers=headers)
        elif path.startswith("/v1/changesets/") and path[15:]:
            self._fields(query)
            revision = path[15:]
            result = store.get_changeset(revision)
            headers = self._immutable(revision) if _OBJECT_ID.fullmatch(revision) else None
            self._send(200, result, headers=headers)
        elif path == "/v1/tree":
            self._fields(query, ("rev",), ("prefix",))
            self._text(query, "rev")
            self._send(200, store.tree(query["rev"], query.get("prefix", "")))
        elif path == "/v1/log":
            self._fields(query, ("rev",), ("limit", "path"))
            self._text(query, "rev")
            self._text(query, "path")
            self._send(200, store.log(query["rev"], self._limit(query, 50), query.get("path")))
        elif path == "/v1/diff":
            self._fields(query, ("base", "target"), ("prefix",))
            for name in ("base", "target"):
                self._text(query, name)
            self._send(200, ops.diff(query["base"], query["target"], query.get("prefix", "")))
        elif path == "/v1/merge-base":
            self._fields(query, ("left", "right"))
            for name in ("left", "right"):
                self._text(query, name)
            self._send(200, {"base": ops.merge_base(query["left"], query["right"])})
        elif path == "/v1/blame":
            self._fields(query, ("rev", "path"))
            for name in ("rev", "path"):
                self._text(query, name)
            self._send(200, ops.blame(query["rev"], query["path"]))
        elif path == "/v1/search":
            self._fields(query, ("rev", "q"), ("prefix", "limit"))
            for name in ("rev", "q"):
                self._text(query, name)
            self._send(200, ops.search(query["rev"], query["q"], query.get("prefix", ""),
                                       self._limit(query, 100)))
        else:
            raise ScionError("Endpoint not found", "not_found", 404)

    def _post(self, path: str, query: dict[str, str]) -> None:
        self._fields(query)
        store = self.server.store
        ops = self.server.operations
        if path == "/v1/blobs":
            self._send(201, {"id": store.put_blob(self._body("application/octet-stream"))})
            return
        if path not in ("/v1/changesets", "/v1/merge", "/v1/rebase", "/v1/land") and not (
                path.startswith("/v1/refs/") and path[9:]):
            raise ScionError("Endpoint not found", "not_found", 404)
        body = self._json_body()
        if path == "/v1/changesets":
            self._fields(body, ("parents", "changes", "message", "author"))
            self._text(body, "message", empty=True)
            self._text(body, "author")
            parents = body["parents"]
            if not isinstance(parents, list) or len(parents) > 2 or any(
                    not isinstance(parent, str) or not parent for parent in parents):
                raise ScionError("parents must contain at most two revision strings")
            changes = body["changes"]
            if not isinstance(changes, dict):
                raise ScionError("changes must be a path-to-entry object")
            for path_name, entry in changes.items():
                self._text({"path": path_name}, "path")
                if entry is None:
                    continue
                if not isinstance(entry, dict):
                    raise ScionError("Each change must be an entry object or null")
                self._fields(entry, ("blob", "mode"))
                self._text(entry, "blob")
                if entry["mode"] not in ("100644", "100755"):
                    raise ScionError("File mode must be 100644 or 100755")
            self._send(201, store.create_changeset(**body))
        elif path.startswith("/v1/refs/"):
            self._fields(body, ("new", "expected"))
            self._text(body, "new")
            self._text(body, "expected", nullable=True)
            self._send(200, store.update_ref(path[9:], **body))
        elif path == "/v1/merge":
            self._fields(body, ("base", "ours", "theirs", "message", "author"))
            self._text(body, "base", nullable=True)
            for name in ("ours", "theirs", "author"):
                self._text(body, name)
            self._text(body, "message", empty=True)
            self._send(201, ops.merge(**body))
        elif path == "/v1/rebase":
            self._fields(body, ("revision", "onto"), ("message", "author"))
            for name in ("revision", "onto"):
                self._text(body, name)
            self._text(body, "message", nullable=True, empty=True)
            self._text(body, "author", nullable=True)
            self._send(201, ops.rebase(**body))
        elif path == "/v1/land":
            self._fields(body, ("revision", "ref", "expected"), ("author",))
            for name in ("revision", "ref", "author"):
                self._text(body, name)
            self._text(body, "expected", nullable=True)
            self._send(200, ops.land(**body))

    def _dispatch(self, method: str) -> None:
        try:
            path, query = self._target()
            self._authenticate(path)
            self._check_origin()
            allowed = _allowed_methods(path)
            if not allowed:
                raise ScionError("Endpoint not found", "not_found", 404)
            if method not in allowed:
                raise ScionError("Method not allowed", "method_not_allowed", 405)
            if method == "GET":
                self._get(path, query)
            elif method == "POST":
                self._post(path, query)
            else:
                raise ScionError("Method not allowed", "method_not_allowed", 405)
        except ScionError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self._status = 499
            self.close_connection = True
        except Exception as exc:
            _LOGGER.error("Request %s failed with %s", self.request_id, type(exc).__name__)
            self._error(ScionError("Internal server error", "internal", 500))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD
    do_OPTIONS = do_HEAD


def make_server(store: Store, host: str = "127.0.0.1", port: int = 0,
                token: str | None = None, *, max_workers: int = 32,
                request_timeout: float = REQUEST_TIMEOUT,
                max_inflight_bytes: int = MAX_INFLIGHT_BYTES) -> ScionHTTPServer:
    """Construct a server; callers own serve_forever, shutdown, and server_close."""
    if token is not None and (not isinstance(token, str) or not token or
                              len(token) > 4096 or any(not "!" <= char <= "~" for char in token)):
        raise ScionError("Token must be a nonempty printable ASCII string without whitespace")
    if not isinstance(host, str) or not host:
        raise ScionError("Host must be a nonempty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ScionError("Port must be an integer from 0 to 65535")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 1024:
        raise ScionError("max_workers must be an integer from 1 to 1024")
    if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
            or not math.isfinite(request_timeout) or not 0 < request_timeout <= 300):
        raise ScionError("request_timeout must be a finite number greater than 0 and at most 300 seconds")
    if isinstance(max_inflight_bytes, bool) or not isinstance(max_inflight_bytes, int) or max_inflight_bytes < 1:
        raise ScionError("max_inflight_bytes must be a positive integer")
    if not _loopback_host(host) and token is None:
        raise ScionError("A bearer token is required when binding beyond loopback", "forbidden", 403)
    return ScionHTTPServer(store, host, port, token, max_workers, request_timeout, max_inflight_bytes)


def serve(root: str, host: str = "127.0.0.1", port: int = 7433,
          token: str | None = None, *, max_workers: int = 32,
          request_timeout: float = REQUEST_TIMEOUT,
          max_inflight_bytes: int = MAX_INFLIGHT_BYTES) -> None:
    """Serve a repository until interrupted, then close listening/client threads."""
    server = make_server(Store(root), host, port, token, max_workers=max_workers,
                         request_timeout=request_timeout, max_inflight_bytes=max_inflight_bytes)
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        def terminate(signum, frame):
            raise KeyboardInterrupt
        previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
