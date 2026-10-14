"""Bounded request counters and redacted structured access logs."""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from .config import MAX_URL_BYTES


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


class TelemetryMixin:
    """Expose constant-cardinality metrics and one access event per connection."""

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
