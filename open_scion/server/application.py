"""Server configuration validation and signal-aware application lifecycle."""

from __future__ import annotations

import math
from pathlib import Path
import signal
import threading

from ..security.policy import RefPolicy
from ..store import ScionError, Store
from .config import MAX_INFLIGHT_BYTES, REQUEST_TIMEOUT, _loopback_host
from .transport import ScionHTTPServer


def make_server(store: Store, host: str = "127.0.0.1", port: int = 0,
                token: str | None = None, *, max_workers: int = 32,
                request_timeout: float = REQUEST_TIMEOUT,
                max_inflight_bytes: int = MAX_INFLIGHT_BYTES,
                auth_file: str | Path | None = None,
                ref_policy: str | Path | None = None) -> ScionHTTPServer:
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
    if not _loopback_host(host) and token is None and auth_file is None:
        raise ScionError("Authentication is required when binding beyond loopback", "forbidden", 403)
    if ref_policy is not None:
        if not isinstance(ref_policy, (str, Path)) or not str(ref_policy):
            raise ScionError("Ref policy must be a nonempty file path")
        store.ref_policy = RefPolicy.from_file(ref_policy)
    return ScionHTTPServer(store, host, port, token, max_workers, request_timeout, max_inflight_bytes,
                           auth_file=auth_file)


def serve(root: str, host: str = "127.0.0.1", port: int = 7433,
          token: str | None = None, *, max_workers: int = 32,
          request_timeout: float = REQUEST_TIMEOUT,
          max_inflight_bytes: int = MAX_INFLIGHT_BYTES,
          auth_file: str | Path | None = None,
          ref_policy: str | Path | None = None,
          idempotency_retention_seconds: int = 604800,
          idempotency_max_records: int = 100000,
          idempotency_max_result_bytes: int = 1048576) -> None:
    """Serve a repository until interrupted, then close listening/client threads."""
    store = Store(root, idempotency_retention_seconds=idempotency_retention_seconds,
                  idempotency_max_records=idempotency_max_records,
                  idempotency_max_result_bytes=idempotency_max_result_bytes)
    server = make_server(store, host, port, token, max_workers=max_workers,
                         request_timeout=request_timeout, max_inflight_bytes=max_inflight_bytes,
                         auth_file=auth_file, ref_policy=ref_policy)
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
