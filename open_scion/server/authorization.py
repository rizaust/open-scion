"""Map HTTP actions to explicit principal capabilities before reading request bodies."""

from __future__ import annotations

from ..store import ScionError


class RequestAuthorizationMixin:
    def _authenticate(self, path: str) -> None:
        self.principal = None
        if self.command == "GET" and path in {"/health", "/ready"}:
            return
        provided = self.headers.get_all("Authorization", [])
        if len(provided) > 1:
            raise ScionError("A valid bearer token is required", "unauthorized", 401)
        self.principal = self.server.auth_manager.authenticate(provided[0] if provided else None)
        self._authorize_endpoint(path)

    def _require_capability(self, capability: str, ref: str | None = None) -> None:
        self.server.auth_manager.require(self.principal, capability, ref)

    def _authorize_endpoint(self, path: str) -> None:
        if self.command == "GET":
            if path == "/v1/identity":
                return
            self._require_capability("admin" if path == "/v1/metrics" else "read")
        elif self.command == "POST":
            if path == "/v1/blobs":
                self._require_capability("object-write")
            elif path in {"/v1/changesets", "/v1/merge", "/v1/rebase"}:
                self._require_capability("changeset-write")
            elif path.startswith("/v1/refs/") and path[9:]:
                self._require_capability("ref-update", path[9:])
            elif path == "/v1/land":
                self._require_capability("ref-update")
                self._require_capability("changeset-write")
