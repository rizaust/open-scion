"""Serialized commits, updates, integration, and shared-ref landing."""

from __future__ import annotations

from urllib.parse import quote

from ..store import ScionError
from .locking import _locked
from .transactions import apply_transaction


def _error(message, paths=None):
    raise ScionError(message, code="workspace_conflict", status=409,
                     details={"conflicts": sorted(paths)} if paths else None)


class WorkspaceMutations:
    @_locked
    def commit(self, message, author):
        old = self.tree()
        new, payloads = self._snapshot(old)
        changes = self._changes(old, new)
        if not changes:
            raise ScionError("No changes to commit", code="nothing_to_commit")
        delta = {}
        for change in changes:
            path, entry = change["path"], change["new"]
            if entry:
                oid = self.client.put_blob(payloads[path])
                if oid != entry["blob"]:
                    raise ScionError("Uploaded blob failed verification", code="integrity", status=502)
            delta[path] = entry
        record = self.client.request("POST", "/v1/changesets", {
            "parents": [self.revision] if self.revision else [], "changes": delta,
            "message": message, "author": author,
        })
        self.config["revision"] = record["id"]
        self._save()
        return record

    @_locked
    def update(self, revision=None):
        revision = revision or self.config.get("ref", "main")
        record = self.client.request("GET", "/v1/changesets/" + quote(revision, safe=""))
        target = record["id"]
        old, new = self.tree(), self.tree(target)
        current, _ = self._snapshot(old)
        dirty = [p for p in old if old[p] != current.get(p)]
        if dirty:
            _error("Commit or restore modified tracked files before updating", dirty)
        remove = {p for p in old if old[p] != new.get(p)}
        write = {p: entry for p, entry in new.items() if entry != old.get(p)}
        conflicts = []
        for relative in write:
            path = self._safe_path(relative)
            if path.exists() and relative not in remove:
                if path.is_file():
                    conflicts.append(relative)
                else:
                    descendants = list(path.rglob("*"))
                    if any(p.is_symlink() or (not p.is_dir() and p.relative_to(self.root).as_posix() not in remove)
                           for p in descendants):
                        conflicts.append(relative)
            for parent in path.parents:
                if parent == self.root:
                    break
                if parent.exists() and not parent.is_dir() and parent.relative_to(self.root).as_posix() not in remove:
                    conflicts.append(relative)
        if conflicts:
            _error("Update would overwrite untracked or ignored files", conflicts)
        payloads = {p: self.blob(entry["blob"]) for p, entry in write.items()}
        apply_transaction(self, target, {p: old[p] for p in remove}, write, payloads)
        return {"revision": target, "updated": len(write), "deleted": len(remove - write.keys()), "sparse": self.sparse}

    @_locked
    def integrate(self, operation, parameters):
        if operation not in ("merge", "rebase"):
            raise ScionError("Unsupported workspace operation")
        parameters = dict(parameters)
        revision_field = "ours" if operation == "merge" else "revision"
        if parameters.get(revision_field) is None:
            if not self.revision:
                raise ScionError("Workspace has no revision; commit first")
            parameters[revision_field] = self.revision
        dirty = [c["path"] for c in self.status()["changes"] if c["status"] != "added"]
        if dirty:
            _error("Commit or restore modified tracked files before integrating", dirty)
        result = self.client.request("POST", "/v1/" + operation, parameters)
        try:
            self.update(result["id"])
        except (ScionError, OSError) as exc:
            raise ScionError(f"Created {result['id']}, but local update failed: {exc}",
                             code="created_update_failed", status=409,
                             details={"changeset": result["id"], "local_error": getattr(exc, "details", None)}) from exc
        return result

    @_locked
    def land(self, revision=None, ref="main", expected=None, author="scion", explicit_expected=False):
        dirty = [c["path"] for c in self.status()["changes"] if c["status"] != "added"]
        if dirty:
            _error("Commit or restore modified tracked files before landing", dirty)
        revision = revision or self.revision
        if not revision:
            raise ScionError("No revision to land; commit first")
        if not explicit_expected:
            expected = self.client.request("GET", "/v1/refs").get(ref)
        result = self.client.request("POST", "/v1/land", {
            "revision": revision, "ref": ref, "expected": expected, "author": author,
        })
        target = result["changeset"]["id"]
        try:
            self.update(target)
            self.config["ref"] = ref
            self._save()
        except (ScionError, OSError) as exc:
            raise ScionError(f"Ref {ref} advanced to {target}, but local update failed: {exc}",
                             code="landed_update_failed", status=409, details={"landed": result, "local_error": getattr(exc, "details", None)}) from exc
        return result
