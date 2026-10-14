"""HTTP handler composition and per-connection accounting."""

from __future__ import annotations

import time
import uuid
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

from .responses import ResponseMixin
from .routing import RoutingMixin
from .validation import RequestValidationMixin

if TYPE_CHECKING:
    from .transport import ScionHTTPServer


class ScionRequestHandler(RoutingMixin, RequestValidationMixin, ResponseMixin, BaseHTTPRequestHandler):
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
