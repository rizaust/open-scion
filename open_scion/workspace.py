"""Sparse working copies with verified, bounded blob caches."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unicodedata
from urllib.parse import quote, urlencode

from .client import Client
from .store import ScionError, blob_id, validate_path, validate_prefix

CACHE_LIMIT = 64 * 1024 * 1024


def _error(message, paths=None):
    raise ScionError(message, code="workspace_conflict", status=409,
                     details={"conflicts": sorted(paths)} if paths else None)


class Workspace:
    def __init__(self, root, client=None):
        selected = Path(root).expanduser().absolute()
        if selected.is_symlink():
            raise ScionError("Workspace root must not be a symlink")
        self.root = selected.resolve()
        self.control = self.root / ".scion"
        self._check_control()
        try:
            self.config = json.loads((self.control / "workspace.json").read_text())
        except (OSError, ValueError) as exc:
            raise ScionError(f"Cannot read workspace metadata: {exc}") from exc
        self.client = client or Client(self.config["server"])
        self.sparse = self._normalize_sparse(self.config.get("sparse", []))
        self.cache = self.control / "cache"
        self.cache.mkdir(exist_ok=True)
        if self.cache.is_symlink():
            raise ScionError("Workspace cache must not be a symlink")
        self._trim_cache()

    def _check_control(self):
        for path in (self.control, self.control / "workspace.json", self.control / "cache"):
            if path.is_symlink():
                raise ScionError("Workspace metadata must not contain symlinks")

    @classmethod
    def find(cls, start=None):
        path = Path(start or os.getcwd()).absolute()
        for root in (path, *path.parents):
            if (root / ".scion" / "workspace.json").exists():
                return cls(root)
        raise ScionError("Not inside a Scion workspace; run scion init first")

    @staticmethod
    def _normalize_sparse(sparse):
        result = []
        for prefix in sparse or []:
            prefix = prefix.rstrip("/")
            if prefix in ("", "."):
                return []
            validate_prefix(prefix)
            if prefix not in result:
                result.append(prefix)
        return sorted(result)

    @classmethod
    def init(cls, directory, server, revision="main", sparse=None, client=None):
        selected = Path(directory).expanduser().absolute()
        if selected.is_symlink():
            raise ScionError("Workspace root must not be a symlink")
        root = selected.resolve()
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ScionError("Initialization requires an empty or nonexistent directory")
        api = client or Client(server)
        prefixes = cls._normalize_sparse(sparse)
        refs = api.request("GET", "/v1/refs")
        try:
            current = api.request("GET", "/v1/changesets/" + quote(revision, safe=""))["id"]
        except ScionError as exc:
            if exc.status != 404 or refs or revision != "main":
                raise
            current = None
        root.mkdir(parents=True, exist_ok=True)
        (root / ".scion").mkdir()
        config = {"version": 1, "server": api.server, "revision": None,
                  "ref": revision if revision in refs or not refs else "main", "sparse": prefixes}
        (root / ".scion" / "workspace.json").write_text(json.dumps(config, indent=2) + "\n")
        workspace = cls(root, client=api)
        if current:
            workspace.update(current)
        return workspace

    @property
    def revision(self):
        return self.config.get("revision")

    def _save(self):
        self._check_control()
        fd, path = tempfile.mkstemp(prefix="workspace-", suffix=".json", dir=self.control)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.config, stream, indent=2)
                stream.write("\n")
            os.replace(path, self.control / "workspace.json")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _visible(self, path, directory=False):
        return not self.sparse or any(path == p or path.startswith(p + "/") or
                                     (directory and p.startswith(path + "/")) for p in self.sparse)

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

    def _trim_cache(self):
        files = []
        total = 0
        for path in self.cache.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ScionError("Unexpected entry in workspace blob cache")
            info = path.stat()
            total += info.st_size
            files.append((info.st_mtime_ns, path, info.st_size))
        for _, path, size in sorted(files):
            if total <= CACHE_LIMIT:
                break
            path.unlink()
            total -= size

    def blob(self, oid):
        if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
            raise ScionError("Invalid blob ID")
        path = self.cache / oid
        if path.is_symlink():
            raise ScionError("Workspace cache must not contain symlinks")
        if path.exists():
            data = path.read_bytes()
            if blob_id(data) == oid:
                os.utime(path, None)
                return data
            path.unlink()
        data = self.client.get_blob(oid)
        if blob_id(data) != oid:
            raise ScionError("Blob content failed verification", code="integrity", status=502)
        if len(data) <= CACHE_LIMIT:
            fd, temporary = tempfile.mkstemp(prefix="blob-", dir=self.cache)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._trim_cache()
        return data

    def _safe_path(self, relative):
        validate_path(relative)
        path = self.root / relative
        for candidate in (path, *path.parents):
            if candidate == self.root:
                break
            if candidate.is_symlink():
                raise ScionError(f"Symlinks are not supported: {relative}")
        return path

    def _ignore_patterns(self):
        path = self._safe_path(".scionignore")
        if not path.is_file():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("!"):
                    raise ScionError(".scionignore negation (!) is not supported")
                result.append(line)
        return result

    @staticmethod
    def _ignored(path, patterns):
        for pattern in patterns:
            anchored = pattern.startswith("/")
            pattern = pattern.lstrip("/")
            pattern = pattern.rstrip("/")
            components = path.split("/")
            candidates = ["/".join(components[:i]) for i in range(1, len(components) + 1)]
            if not anchored and "/" not in pattern:
                candidates += components
            if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                return True
        return False

    def _snapshot(self, tracked):
        files, payloads = {}, {}
        patterns = self._ignore_patterns()
        for directory, names, filenames in os.walk(self.root, followlinks=False):
            parent = Path(directory).relative_to(self.root)
            kept = []
            for name in sorted(names):
                path = (parent / name).as_posix()
                if name in (".scion", ".git") or not self._visible(path, directory=True):
                    continue
                has_tracked = any(p == path or p.startswith(path + "/") for p in tracked)
                if self._ignored(path, patterns) and not has_tracked:
                    continue
                self._safe_path(path)
                kept.append(name)
            names[:] = kept
            for name in sorted(filenames):
                path = (parent / name).as_posix()
                if name in (".scion", ".git") or not self._visible(path):
                    continue
                if path not in tracked and self._ignored(path, patterns):
                    continue
                absolute = self._safe_path(path)
                info = absolute.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ScionError(f"Only regular files are supported: {path}")
                data = absolute.read_bytes()
                files[path] = {"blob": blob_id(data), "mode": "100755" if info.st_mode & 0o111 else "100644"}
                payloads[path] = data
        return files, payloads

    @staticmethod
    def _changes(old, new):
        return [{"path": path, "status": "added" if path not in old else "deleted" if path not in new else "modified",
                 "old": old.get(path), "new": new.get(path)}
                for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]

    def status(self):
        old = self.tree()
        new, _ = self._snapshot(old)
        return {"root": str(self.root), "revision": self.revision, "sparse": self.sparse,
                "changes": self._changes(old, new)}

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
        transaction = Path(tempfile.mkdtemp(prefix="update-", dir=self.control))
        backed_up, written = [], []
        previous = self.revision
        try:
            for relative in sorted(remove, key=lambda p: (-p.count("/"), p)):
                source = self._safe_path(relative)
                backup = transaction / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, backup)
                backed_up.append(relative)
            for relative, entry in sorted(write.items()):
                path = self._safe_path(relative)
                if path.is_dir():
                    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    path.rmdir()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    written.append(relative)
                    stream.write(payloads[relative])
                path.chmod(0o755 if entry["mode"] == "100755" else 0o644)
            self.config["revision"] = target
            self._save()
        except Exception:
            self.config["revision"] = previous
            for relative in reversed(written):
                self._safe_path(relative).unlink()
            for relative in reversed(backed_up):
                path = self._safe_path(relative)
                if path.is_dir():
                    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    path.rmdir()
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(transaction / relative, path)
            raise
        finally:
            shutil.rmtree(transaction)
        for relative in remove:
            parent = (self.root / relative).parent
            while parent != self.root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return {"revision": target, "updated": len(write), "deleted": len(remove - write.keys()), "sparse": self.sparse}

    def integrate(self, operation, parameters):
        if operation not in ("merge", "rebase"):
            raise ScionError("Unsupported workspace operation")
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
