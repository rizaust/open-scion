"""Three-way merges, delta replay, and compare-and-swap landing."""

from __future__ import annotations

from ..store import ScionError
from .text import merge_text


class IntegrationOperations:
    """Create immutable integration results before moving shared refs."""

    def merge_base(self, left: str, right: str) -> str | None:
        return self.graph.merge_base(left, right)

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
        if base and (not self.graph.is_ancestor(base, ours) or not self.graph.is_ancestor(base, theirs)):
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
        if current is None or self.graph.is_ancestor(current, revision):
            landed = self.store.get_changeset(revision)
        elif self.graph.is_ancestor(revision, current):
            landed = self.store.get_changeset(current)
        else:
            landed = self.merge(None, current, revision,
                                "Land: " + self.store.get_changeset(revision)["message"], author)
        updated = self.store.update_ref(ref, landed["id"], expected)
        return {"changeset": landed, "ref": updated}
