"""Explicit read endpoints for repository state and operational probes."""

from __future__ import annotations

from ..store import ScionError
from .config import _OBJECT_ID


class ReadRoutesMixin:
    """Map documented GET routes to read operations."""

    def _get(self, path: str, query: dict[str, str]) -> None:
        store = self.server.store
        ops = self.server.operations
        if path == "/health":
            self._fields(query)
            self._send(200, {"status": "ok"})
        elif path == "/ready":
            self._fields(query)
            if not self.server.readiness():
                raise ScionError("Repository is not ready", "not_ready", 503)
            self._send(200, {"status": "ready"})
        elif path == "/v1/metrics":
            self._fields(query)
            self._send(200, self.server.metrics())
        elif path == "/v1/ref-log":
            self._fields(query, (), ("name", "limit"))
            self._text(query, "name")
            self._send(200, store.ref_log(query.get("name"), self._limit(query, 50)))
        elif path == "/v1/refs":
            self._fields(query)
            self._send(200, store.list_refs())
        elif path == "/v1/stats":
            self._fields(query)
            self._send(200, store.stats())
        elif path.startswith("/v1/blobs/") and path[10:]:
            self._fields(query)
            oid = path[10:]
            data = store.get_blob(oid)
            headers = self._immutable(oid)
            if self.headers.get("If-None-Match") in ("*", headers["ETag"]):
                self._send(304, headers=headers)
            else:
                self._send(200, data, raw=True, headers=headers)
        elif path.startswith("/v1/changesets/") and path[15:]:
            self._fields(query)
            revision = path[15:]
            result = store.get_changeset(revision)
            headers = self._immutable(revision) if _OBJECT_ID.fullmatch(revision) else None
            self._send(200, result, headers=headers)
        elif path == "/v1/tree":
            self._fields(query, ("rev",), ("prefix",))
            self._text(query, "rev")
            self._send(200, store.tree(query["rev"], query.get("prefix", "")))
        elif path == "/v1/log":
            self._fields(query, ("rev",), ("limit", "path"))
            self._text(query, "rev")
            self._text(query, "path")
            self._send(200, store.log(query["rev"], self._limit(query, 50), query.get("path")))
        elif path == "/v1/diff":
            self._fields(query, ("base", "target"), ("prefix",))
            for name in ("base", "target"):
                self._text(query, name)
            self._send(200, ops.diff(query["base"], query["target"], query.get("prefix", "")))
        elif path == "/v1/merge-base":
            self._fields(query, ("left", "right"))
            for name in ("left", "right"):
                self._text(query, name)
            self._send(200, {"base": ops.merge_base(query["left"], query["right"])})
        elif path == "/v1/blame":
            self._fields(query, ("rev", "path"))
            for name in ("rev", "path"):
                self._text(query, name)
            self._send(200, ops.blame(query["rev"], query["path"]))
        elif path == "/v1/search":
            self._fields(query, ("rev", "q"), ("prefix", "limit"))
            for name in ("rev", "q"):
                self._text(query, name)
            self._send(200, ops.search(query["rev"], query["q"], query.get("prefix", ""),
                                       self._limit(query, 100)))
        else:
            raise ScionError("Endpoint not found", "not_found", 404)
