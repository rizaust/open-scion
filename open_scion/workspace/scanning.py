"""Sparse selection, ignore rules, portable paths, and working-file snapshots."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import stat

from ..store import ScionError, blob_id, validate_path, validate_prefix

NATIVE_EXECUTABLE_BITS = os.name != "nt"


class WorkspaceScanning:


    @staticmethod
    def _native_modes():
        return NATIVE_EXECUTABLE_BITS

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

    def _visible(self, path, directory=False):
        return not self.sparse or any(path == p or path.startswith(p + "/") or
                                     (directory and p.startswith(path + "/")) for p in self.sparse)

    def _safe_path(self, relative):
        validate_path(relative)
        path = self.root / relative
        if not path.is_relative_to(self.root) or path == self.root:
            raise ScionError(f"Path escapes the workspace: {relative}")
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
                mode = ("100755" if info.st_mode & 0o111 else "100644") if NATIVE_EXECUTABLE_BITS else tracked.get(path, {}).get("mode", "100644")
                files[path] = {"blob": blob_id(data), "mode": mode}
                payloads[path] = data
        return files, payloads
