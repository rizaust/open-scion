"""Explicit mutation endpoints with validated argument schemas."""

from __future__ import annotations

from ..store import ScionError


class WriteRoutesMixin:
    """Map documented POST routes to immutable writes and atomic ref operations."""

    def _post(self, path: str, query: dict[str, str]) -> None:
        self._fields(query)
        store = self.server.store
        ops = self.server.operations
        if path == "/v1/blobs":
            self._send(201, {"id": store.put_blob(self._body("application/octet-stream"))})
            return
        if path not in ("/v1/changesets", "/v1/merge", "/v1/rebase", "/v1/land") and not (
                path.startswith("/v1/refs/") and path[9:]):
            raise ScionError("Endpoint not found", "not_found", 404)
        body = self._json_body()
        if path == "/v1/changesets":
            self._fields(body, ("parents", "changes", "message", "author"))
            self._text(body, "message", empty=True)
            self._text(body, "author")
            parents = body["parents"]
            if not isinstance(parents, list) or len(parents) > 2 or any(
                    not isinstance(parent, str) or not parent for parent in parents):
                raise ScionError("parents must contain at most two revision strings")
            changes = body["changes"]
            if not isinstance(changes, dict):
                raise ScionError("changes must be a path-to-entry object")
            for path_name, entry in changes.items():
                self._text({"path": path_name}, "path")
                if entry is None:
                    continue
                if not isinstance(entry, dict):
                    raise ScionError("Each change must be an entry object or null")
                self._fields(entry, ("blob", "mode"))
                self._text(entry, "blob")
                if entry["mode"] not in ("100644", "100755"):
                    raise ScionError("File mode must be 100644 or 100755")
            self._send(201, store.create_changeset(**body))
        elif path.startswith("/v1/refs/"):
            self._fields(body, ("new", "expected"))
            self._text(body, "new")
            self._text(body, "expected", nullable=True)
            self._send(200, store.update_ref(path[9:], **body))
        elif path == "/v1/merge":
            self._fields(body, ("base", "ours", "theirs", "message", "author"))
            self._text(body, "base", nullable=True)
            for name in ("ours", "theirs", "author"):
                self._text(body, name)
            self._text(body, "message", empty=True)
            self._send(201, ops.merge(**body))
        elif path == "/v1/rebase":
            self._fields(body, ("revision", "onto"), ("message", "author"))
            for name in ("revision", "onto"):
                self._text(body, name)
            self._text(body, "message", nullable=True, empty=True)
            self._text(body, "author", nullable=True)
            self._send(201, ops.rebase(**body))
        elif path == "/v1/land":
            self._fields(body, ("revision", "ref", "expected"), ("author",))
            for name in ("revision", "ref", "author"):
                self._text(body, name)
            self._text(body, "expected", nullable=True)
            self._send(200, ops.land(**body))
