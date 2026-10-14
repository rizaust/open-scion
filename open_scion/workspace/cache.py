"""Content-verified local blob caching with a fixed storage budget."""

from __future__ import annotations

import os
import tempfile

from ..store import ScionError, blob_id
from .locking import _locked

CACHE_LIMIT = 64 * 1024 * 1024


class WorkspaceCache:


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

    @_locked
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
