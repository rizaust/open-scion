"""Reentrant workspace operations and cross-process advisory writer locks."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import errno
import math
import os
from pathlib import Path
import stat
import threading
import time

from ..store import ScionError


def _locked(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._operation():
            return method(self, *args, **kwargs)
    return run


class WorkspaceLock:
    """A reentrant thread lock paired with an advisory process lock."""

    def __init__(self, path: Path, timeout: float = 30):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ScionError("Workspace lock timeout must be a finite nonnegative number")
        self.path = path
        self.timeout = timeout
        self._thread = threading.RLock()
        self._depth = 0
        self._fd = None

    def _busy(self):
        return ScionError("Workspace is busy; another process holds its writer lock",
                          code="workspace_busy", status=409,
                          details={"lock": str(self.path), "timeout": self.timeout})

    @staticmethod
    def _lock(fd):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    @contextmanager
    def acquire(self):
        deadline = time.monotonic() + self.timeout
        if not self._thread.acquire(timeout=self.timeout):
            raise self._busy()
        fd = None
        entered = False
        try:
            if self._depth:
                self._depth += 1
                entered = True
                yield
                return
            if self.path.is_symlink():
                raise ScionError("Workspace lock must not be a symlink")
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ScionError("Workspace lock must be a regular file")
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    self._lock(fd)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise ScionError(f"Cannot acquire workspace lock: {exc}", code="workspace_lock", status=500) from exc
                    if time.monotonic() >= deadline:
                        raise self._busy() from exc
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            self._fd = fd
            self._depth = 1
            entered = True
            yield
        finally:
            try:
                if entered:
                    self._depth -= 1
                    if not self._depth:
                        try:
                            self._unlock(self._fd)
                        finally:
                            self._fd = None
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                finally:
                    self._thread.release()
