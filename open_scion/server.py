"""A bounded, single-tenant HTTP transport for Open Scion."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .operations import Operations
from .store import ScionError, Store


MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_URL_BYTES = 8192
REQUEST_TIMEOUT = 15
MAX_RESULT_LIMIT = 1000
_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_LOGGER = logging.getLogger(__name__)


def _allowed_methods(path: str) -> tuple[str, ...]:
    if path in {"/health", "/v1/refs", "/v1/stats", "/v1/tree", "/v1/log", "/v1/diff",
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
    """Threads do not prevent shutdown when a client abandons a request."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, store: Store, host: str, port: int, token: str | None):
        self.store = store
        self.operations = Operations(store)
        self.token = token
        self.loopback = _loopback_host(host)
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), ScionRequestHandler)


class ScionRequestHandler(BaseHTTPRequestHandler):
    """Only documented endpoints are dispatched; no reflective RPC is exposed."""

    server: ScionHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "OpenScion"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOGGER.debug("HTTP %s %r", self.client_address[0], fmt % args)

    def _send(self, status: int, body: Any = None, *, raw: bool = False,
              headers: dict[str, str] | None = None) -> None:
        data = body if raw else json.dumps(body, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")
        if status in (204, 304):
            data = b""
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else
                         "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
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
            self._body_length()
        except ScionError as exc:
            self._error(exc)
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def _body(self, content_type: str) -> bytes:
        length = self._body_length()
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != content_type:
            raise ScionError(f"Content-Type must be {content_type}", "unsupported_media_type", 415)
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
            self.close_connection = True
        except Exception:
            _LOGGER.exception("Unhandled Open Scion request error")
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
                token: str | None = None) -> ScionHTTPServer:
    """Construct a server; callers own serve_forever, shutdown, and server_close."""
    if token is not None and (not isinstance(token, str) or not token or
                              len(token) > 4096 or any(not "!" <= char <= "~" for char in token)):
        raise ScionError("Token must be a nonempty printable ASCII string without whitespace")
    if not isinstance(host, str) or not host:
        raise ScionError("Host must be a nonempty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ScionError("Port must be an integer from 0 to 65535")
    if not _loopback_host(host) and token is None:
        raise ScionError("A bearer token is required when binding beyond loopback", "forbidden", 403)
    return ScionHTTPServer(store, host, port, token)


def serve(root: str, host: str = "127.0.0.1", port: int = 7433,
          token: str | None = None) -> None:
    """Serve a repository until interrupted, then close listening/client threads."""
    server = make_server(Store(root), host, port, token)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
