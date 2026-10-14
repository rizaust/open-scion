"""Route classification, method dispatch, and protected error handling."""

from __future__ import annotations

import logging

from ..store import ScionError
from .read_routes import ReadRoutesMixin
from .write_routes import WriteRoutesMixin


_LOGGER = logging.getLogger("open_scion.server")


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


class RoutingMixin(ReadRoutesMixin, WriteRoutesMixin):
    """Dispatch only documented endpoints after authentication and origin checks."""

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
