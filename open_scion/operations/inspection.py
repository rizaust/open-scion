"""Snapshot comparison, line attribution, and content search."""

from __future__ import annotations

import difflib

from ..store import ScionError, validate_path
from .text import _text


class InspectionOperations:
    """Read-only source inspection on a repository facade."""

    def diff(self, base: str, target: str, prefix: str = "") -> list[dict]:
        before, after = self.store.tree(base, prefix), self.store.tree(target, prefix)
        result = []
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            if old == new:
                continue
            old_text = _text(self.store.get_blob(old["blob"])) if old else ""
            new_text = _text(self.store.get_blob(new["blob"])) if new else ""
            binary = old_text is None or new_text is None
            patch, truncated = "", False
            if not binary:
                lines = list(difflib.unified_diff(
                    old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                    fromfile="a/" + path if old else "/dev/null",
                    tofile="b/" + path if new else "/dev/null"))
                truncated = len(lines) > 2000
                patch = "".join(lines[:2000])
            result.append({"path": path, "status": "added" if not old else "deleted" if not new else "modified",
                           "old": old, "new": new, "patch": patch, "binary": binary,
                           "truncated": truncated})
        return result

    def blame(self, revision: str, path: str) -> list[dict]:
        validate_path(path)
        record = self.store.get_changeset(revision)
        revision = record["id"]
        if path not in self.store.tree(record["id"], path):
            raise ScionError("Path not found", code="not_found", status=404)
        lineage = []
        while True:
            lineage.append(record)
            if not record["parents"]:
                break
            record = self.store.get_changeset(record["parents"][0])
        previous, origins = [], []
        for record in reversed(lineage):
            if path not in record["changes"]:
                continue
            entry = record["changes"][path]
            text = _text(self.store.get_blob(entry["blob"])) if entry else ""
            if text is None:
                # A later conversion back to text establishes new line origins.
                previous, origins = [], []
                continue
            lines = text.splitlines()
            new_origins = [(record["id"], record["author"])] * len(lines)
            for a, b, length in difflib.SequenceMatcher(None, previous, lines, autojunk=False).get_matching_blocks():
                new_origins[b:b + length] = origins[a:a + length]
            previous, origins = lines, new_origins
        target = self.store.tree(revision, path)[path]
        if _text(self.store.get_blob(target["blob"])) is None:
            raise ScionError("Blame supports UTF-8 text files up to 1 MiB and 20,000 lines")
        return [{"line": i + 1, "text": line, "changeset": origin[0], "author": origin[1]}
                for i, (line, origin) in enumerate(zip(previous, origins))]

    def search(self, revision: str, query: str, prefix: str = "", limit: int = 100) -> list[dict]:
        if not isinstance(query, str) or not query:
            raise ScionError("Search query must not be empty")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ScionError("Search limit must be between 1 and 1000")
        result = []
        for path, entry in sorted(self.store.tree(revision, prefix).items()):
            text = _text(self.store.get_blob(entry["blob"]))
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    result.append({"path": path, "line": number, "text": line})
                    if len(result) == limit:
                        return result
        return result
