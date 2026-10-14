"""Selected server trees, local status, and working-copy diffs."""

from __future__ import annotations

import difflib
import unicodedata
from urllib.parse import urlencode

from ..store import ScionError, validate_path
from .locking import _locked


class WorkspaceInspection:


    @_locked
    def tree(self, revision=None):
        revision = self.revision if revision is None else revision
        if not revision:
            return {}
        tree = {}
        for prefix in self.sparse or [""]:
            part = self.client.request("GET", "/v1/tree?" + urlencode({"rev": revision, "prefix": prefix}))
            for path, entry in part.items():
                validate_path(path)
                if entry.get("mode") not in ("100644", "100755"):
                    raise ScionError("Server returned an invalid file mode", code="protocol", status=502)
                oid = entry.get("blob", "")
                if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
                    raise ScionError("Server returned an invalid blob ID", code="protocol", status=502)
                if self._visible(path):
                    tree[path] = {"blob": oid, "mode": entry["mode"]}
        aliases = {}
        for path in tree:
            parts = path.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                key = unicodedata.normalize("NFC", prefix).casefold()
                if key in aliases and aliases[key] != prefix:
                    raise ScionError(f"Paths collide on case-insensitive filesystems: {aliases[key]}, {prefix}")
                aliases[key] = prefix
        return tree

    @staticmethod
    def _changes(old, new):
        return [{"path": path, "status": "added" if path not in old else "deleted" if path not in new else "modified",
                 "old": old.get(path), "new": new.get(path)}
                for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]

    @_locked
    def status(self):
        old = self.tree()
        new, _ = self._snapshot(old)
        return {"root": str(self.root), "revision": self.revision, "sparse": self.sparse,
                "changes": self._changes(old, new)}

    @_locked
    def diff(self):
        old = self.tree()
        new, payloads = self._snapshot(old)
        changes = self._changes(old, new)
        for change in changes:
            path = change["path"]
            before = self.blob(change["old"]["blob"]) if change["old"] else b""
            after = payloads.get(path, b"")
            try:
                if b"\0" in before or b"\0" in after:
                    raise UnicodeError()
                a, b = before.decode("utf-8"), after.decode("utf-8")
                change["patch"] = "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                    fromfile="a/" + path, tofile="b/" + path))
                change["binary"] = False
            except UnicodeError:
                change["patch"] = None
                change["binary"] = True
        return changes
