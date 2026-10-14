"""Authenticated transport with bounded retries and durable mutation keys."""

from __future__ import annotations

from http.client import HTTPException
import json
import math
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from ..store import ScionError

_RETRYABLE = {429, 502, 503, 504}
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScionError("API redirects are not followed; use the canonical server URL", code="redirect", status=502)


class HTTPTransport:
    """Keep one key across network retries of one logical mutation."""

    def __init__(self, server: str, token: str | None = None, timeout: float = 30, retries: int = 2):
        parsed = urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ScionError("Server must be an http:// or https:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ScionError("Server URL must not contain credentials, query, or fragment")
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ScionError("Retries must be an integer from 0 to 5")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ScionError("Timeout must be a positive finite number")
        self.server = server.rstrip("/")
        self.token = token if token is not None else os.environ.get("SCION_TOKEN")
        self.timeout = timeout
        self.retries = retries
        self._idempotency_supported = False
        self._opener = build_opener(_NoRedirect())

    def _capabilities(self, headers):
        capabilities = headers.get("Scion-Capabilities")
        if capabilities is not None:
            self._idempotency_supported = "idempotency-v1" in {item.strip() for item in capabilities.split(",")}

    def _mutation_error_details(self, key, attempt, details=None):
        if key is None:
            return details
        result = dict(details) if isinstance(details, dict) else ({"server_details": details} if details is not None else {})
        result.update({"idempotency_key": key, "attempts": attempt + 1,
                       "replay_supported": self._idempotency_supported})
        return result

    @staticmethod
    def _wait(attempt, headers=None):
        delay = min(0.1 * 2 ** attempt, 1.0)
        if headers is not None:
            try:
                requested = float(headers.get("Retry-After", "0"))
                if math.isfinite(requested):
                    delay = min(2.0, max(delay, requested))
            except ValueError:
                pass
        time.sleep(delay)

    def request(self, method: str, path: str, body=None, raw: bool = False, *, idempotency_key: str | None = None):
        if not path.startswith("/") or path.startswith("//"):
            raise ScionError("API path must start with one slash")
        structured_write = method == "POST" and body is not None and not isinstance(body, bytes)
        if idempotency_key is not None and (not structured_write or not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key)):
            raise ScionError("A valid Idempotency-Key can only be used for a structured POST")
        key = idempotency_key or (uuid.uuid4().hex if structured_write else None)
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if key:
            headers["Idempotency-Key"] = key
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        intrinsically_safe = method == "GET" or method == "POST" and path == "/v1/blobs" and isinstance(body, bytes)
        for attempt in range(self.retries + 1):
            request = Request(self.server + path, data=data, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    self._capabilities(response.headers)
                    payload = response.read()
                if raw:
                    return payload
                try:
                    return json.loads(payload)
                except ValueError as exc:
                    raise ScionError("Server returned invalid JSON", code="protocol", status=502,
                                     details=self._mutation_error_details(key, attempt)) from exc
            except HTTPError as exc:
                self._capabilities(exc.headers)
                can_retry = intrinsically_safe or key is not None and self._idempotency_supported
                if exc.code in _RETRYABLE and can_retry and attempt < self.retries:
                    exc.close()
                    self._wait(attempt, exc.headers)
                    continue
                try:
                    detail = json.loads(exc.read()).get("error", {})
                    if not isinstance(detail, dict):
                        detail = {}
                except (ValueError, AttributeError, HTTPException, OSError):
                    detail = {}
                finally:
                    exc.close()
                raise ScionError(detail.get("message", f"Server returned HTTP {exc.code}"),
                                 code=detail.get("code", "http_error"), status=exc.code,
                                 details=self._mutation_error_details(key, attempt, detail.get("details"))) from exc
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                can_retry = intrinsically_safe or key is not None and self._idempotency_supported
                if can_retry and attempt < self.retries:
                    self._wait(attempt)
                    continue
                raise ScionError(f"Cannot reach {self.server}: {exc}", code="connection", status=503,
                                 details={"idempotency_key": key, "attempts": attempt + 1,
                                          "replay_supported": self._idempotency_supported}) from exc
