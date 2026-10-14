"""Server-side graph, merge, and source inspection operations."""

from __future__ import annotations

import difflib

from .store import ScionError, Store, validate_path

TEXT_LIMIT = 1024 * 1024
LINE_LIMIT = 20000


def _text(data: bytes) -> str | None:
    if b"\0" in data or len(data) > TEXT_LIMIT:
        return None
    try:
        result = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return result if result.count("\n") <= LINE_LIMIT else None


def _edits(base: list[str], other: list[str]) -> list[tuple]:
    return [(i, j, other[a:b]) for tag, i, j, a, b in
            difflib.SequenceMatcher(None, base, other, autojunk=False).get_opcodes()
            if tag != "equal"]


def merge_text(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
    """Combine disjoint line edits; refuse overlapping or non-text edits."""
    texts = [_text(data) for data in (base, ours, theirs)]
    if any(text is None for text in texts):
        return None
    original, left, right = [text.splitlines(keepends=True) for text in texts]
    ours_edits, theirs_edits = _edits(original, left), _edits(original, right)
    for a, b, replacement in ours_edits:
        for c, d, other in theirs_edits:
            if (a, b, replacement) == (c, d, other):
                continue
            overlap = max(a, c) < min(b, d)
            same_insertion = a == b == c == d
            inside = (a == b and c < a < d) or (c == d and a < c < b)
            if overlap or same_insertion or inside:
                return None
    edits = list(ours_edits)
    edits.extend(edit for edit in theirs_edits if edit not in edits)
    result, cursor = [], 0
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1])):
        result.extend(original[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(original[cursor:])
    return "".join(result).encode("utf-8")


class Operations:
    """Derived queries never require a client-side repository."""

    def __init__(self, store: Store):
        self.store = store

    def _ancestors(self, revision: str) -> dict[str, dict]:
        pending = [self.store.resolve(revision)]
        result = {}
        while pending:
            oid = pending.pop()
            if oid in result:
                continue
            record = self.store.get_changeset(oid)
            result[oid] = record
            pending.extend(record["parents"])
        return result

    def merge_base(self, left: str, right: str) -> str | None:
        ancestors = self._ancestors(left)
        common = ancestors.keys() & self._ancestors(right).keys()
        return max(common, key=lambda oid: (ancestors[oid]["generation"], oid)) if common else None

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

    def _merged_tree(self, base: str | None, ours: str, theirs: str) -> tuple[dict, dict]:
        original = self.store.tree(base) if base else {}
        left, right = self.store.tree(ours), self.store.tree(theirs)
        merged, conflicts = {}, []
        for path in sorted(original.keys() | left.keys() | right.keys()):
            old, a, b = original.get(path), left.get(path), right.get(path)
            if a == b or b == old:
                chosen = a
            elif a == old:
                chosen = b
            elif not old or not a or not b:
                conflicts.append(path)
                continue
            else:
                if a["mode"] == b["mode"] or b["mode"] == old["mode"]:
                    mode = a["mode"]
                elif a["mode"] == old["mode"]:
                    mode = b["mode"]
                else:
                    conflicts.append(path)
                    continue
                if a["blob"] == b["blob"] or b["blob"] == old["blob"]:
                    blob = a["blob"]
                elif a["blob"] == old["blob"]:
                    blob = b["blob"]
                else:
                    content = merge_text(*(self.store.get_blob(e["blob"]) for e in (old, a, b)))
                    if content is None:
                        conflicts.append(path)
                        continue
                    blob = self.store.put_blob(content)
                chosen = {"blob": blob, "mode": mode}
            if chosen is not None:
                merged[path] = chosen
        for path in merged:
            parts = path.split("/")
            if any("/".join(parts[:i]) in merged for i in range(1, len(parts))):
                conflicts.append(path)
        if conflicts:
            raise ScionError("Merge conflicts require an explicit resolution changeset", code="conflict",
                             status=409, details={"conflicts": sorted(set(conflicts))})
        return left, merged

    def merge(self, base: str | None, ours: str, theirs: str, message: str, author: str) -> dict:
        ours, theirs = self.store.resolve(ours), self.store.resolve(theirs)
        base = self.store.resolve(base) if base else self.merge_base(ours, theirs)
        if base and (base not in self._ancestors(ours) or base not in self._ancestors(theirs)):
            raise ScionError("Merge base must be an ancestor of both revisions")
        left, merged = self._merged_tree(base, ours, theirs)
        changes = {p: merged.get(p) for p in left.keys() | merged.keys() if left.get(p) != merged.get(p)}
        parents = [ours] if ours == theirs else [ours, theirs]
        return self.store.create_changeset(parents, changes, message, author)

    def rebase(self, revision: str, onto: str, message: str | None = None,
               author: str | None = None) -> dict:
        record = self.store.get_changeset(revision)
        onto = self.store.resolve(onto)
        base = record["parents"][0] if record["parents"] else None
        left, merged = self._merged_tree(base, onto, record["id"])
        changes = {p: merged.get(p) for p in left.keys() | merged.keys() if left.get(p) != merged.get(p)}
        return self.store.create_changeset([onto], changes,
                                          message if message is not None else record["message"],
                                          author if author is not None else record["author"])

    def land(self, revision: str, ref: str, expected: str | None, author: str = "scion") -> dict:
        revision = self.store.resolve(revision)
        current = self.store.list_refs().get(ref)
        if current != expected:
            raise ScionError("Ref changed; fetch the current target and retry", code="stale_ref", status=409,
                             details={"expected": expected, "actual": current})
        if current is None or current in self._ancestors(revision):
            landed = self.store.get_changeset(revision)
        elif revision in self._ancestors(current):
            landed = self.store.get_changeset(current)
        else:
            landed = self.merge(None, current, revision,
                                "Land: " + self.store.get_changeset(revision)["message"], author)
        updated = self.store.update_ref(ref, landed["id"], expected)
        return {"changeset": landed, "ref": updated}

    def blame(self, revision: str, path: str) -> list[dict]:
        validate_path(path)
        record = self.store.get_changeset(revision)
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
