"""JSON errors, binary responses, cache headers, and request identity."""

from __future__ import annotations

import json
from typing import Any

from ..store import ScionError
from .routing import _allowed_methods


class ResponseMixin:
    """Send bounded HTTP envelopes with stable headers and error shapes."""

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

    def _immutable(self, oid: str) -> dict[str, str]:
        return {"Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{oid}"'}
