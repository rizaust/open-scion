"""Authenticated JSON and binary HTTP transport."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..store import ScionError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScionError("API redirects are not followed; use the canonical server URL", code="redirect", status=502)


class HTTPTransport:
    """Bind requests and credentials to one canonical server endpoint."""

    def __init__(self, server: str, token: str | None = None, timeout: float = 30):
        parsed = urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ScionError("Server must be an http:// or https:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ScionError("Server URL must not contain credentials, query, or fragment")
        self.server = server.rstrip("/")
        self.token = token if token is not None else os.environ.get("SCION_TOKEN")
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def request(self, method: str, path: str, body=None, raw: bool = False):
        if not path.startswith("/") or path.startswith("//"):
            raise ScionError("API path must start with one slash")
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        request = Request(self.server + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {})
                if not isinstance(detail, dict):
                    detail = {}
            except (ValueError, AttributeError):
                detail = {}
            raise ScionError(
                detail.get("message", f"Server returned HTTP {exc.code}"),
                code=detail.get("code", "http_error"), status=exc.code,
                details=detail.get("details"),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ScionError(f"Cannot reach {self.server}: {exc}", code="connection", status=503) from exc
        if raw:
            return payload
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise ScionError("Server returned invalid JSON", code="protocol", status=502) from exc
