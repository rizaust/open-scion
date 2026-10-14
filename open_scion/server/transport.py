"""Bounded connection admission, request-body budgets, and worker draining."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections import Counter
from http.server import ThreadingHTTPServer

from ..operations import Operations
from ..security.identity import AuthManager
from ..store import Store
from ..store.errors import ScionError
from .config import MAX_URL_BYTES, _loopback_host
from .handler import ScionRequestHandler
from .readiness import ReadinessMixin
from .telemetry import TelemetryMixin


class ScionHTTPServer(TelemetryMixin, ReadinessMixin, ThreadingHTTPServer):
    """Bound worker admission, account for every connection, and drain on close."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, store: Store, host: str, port: int, token: str | None,
                 max_workers: int, request_timeout: float, max_inflight_bytes: int,
                 auth_file=None):
        self.store = store
        self.operations = Operations(store)
        self.loopback = _loopback_host(host)
        self.auth_manager = AuthManager(token=token, auth_file=auth_file)
        self.auth_file = self.auth_manager.auth_file
        self._token = token
        if not self.loopback and not self.auth_manager.enabled:
            raise ScionError("Authentication is required when binding beyond loopback", "forbidden", 403)
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

    @property
    def token(self):
        """Retain the legacy in-memory token configuration interface."""
        return self._token

    @token.setter
    def token(self, value):
        manager = AuthManager(token=value, auth_file=self.auth_file)
        if not self.loopback and not manager.enabled:
            raise ScionError("Authentication is required when binding beyond loopback", "forbidden", 403)
        self.auth_manager = manager
        self._token = value

    def reserve_body(self, size: int) -> bool:
        with self._state:
            if size > self.max_inflight_bytes - self._inflight_body_bytes:
                return False
            self._inflight_body_bytes += size
            return True

    def release_body(self, size: int) -> None:
        with self._state:
            self._inflight_body_bytes -= size

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
                        "Scion-Capabilities: idempotency-v1\r\n"
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
