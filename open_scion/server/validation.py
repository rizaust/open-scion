"""Request authentication, URL parsing, and bounded body validation."""

from __future__ import annotations

import hmac
import json
import re
import socket
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..store import ScionError
from .config import MAX_BODY_BYTES, MAX_RESULT_LIMIT, MAX_URL_BYTES, _BAD_PERCENT, _loopback_host


class RequestValidationMixin:
    """Validate transport and payload boundaries before invoking repository operations."""

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
