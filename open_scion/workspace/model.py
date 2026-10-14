"""Workspace identity, metadata, initialization, and operation lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from urllib.parse import quote

from ..client import Client
from ..store import ScionError
from .cache import WorkspaceCache
from .filesystem import durable_json, sync_directory
from .inspection import WorkspaceInspection
from .locking import WorkspaceLock
from .mutations import WorkspaceMutations
from .recovery import recover_transaction
from .scanning import WorkspaceScanning


class Workspace(WorkspaceMutations, WorkspaceInspection, WorkspaceScanning, WorkspaceCache):


    def __init__(self, root, client=None, lock_timeout=30, auto_recover=True, _lock=None):
        selected = Path(root).expanduser().absolute()
        if selected.is_symlink():
            raise ScionError("Workspace root must not be a symlink")
        self.root = selected.resolve()
        self.control = self.root / ".scion"
        self._check_control()
        self._client_override = client
        self._lock = _lock or WorkspaceLock(self.control / "writer.lock", timeout=lock_timeout)
        self._operation_depth = 0
        self.cache = self.control / "cache"
        self.last_recovery = {"recovered": False, "action": "none", "revision": None}
        with self._operation(recover=auto_recover):
            self.cache.mkdir(exist_ok=True)
            if self.cache.is_symlink():
                raise ScionError("Workspace cache must not be a symlink")
            self._trim_cache()

    def _check_control(self):
        for path in (self.control, self.control / "workspace.json", self.control / "cache",
                     self.control / "writer.lock", self.control / "transaction.json"):
            if path.is_symlink():
                raise ScionError("Workspace metadata must not contain symlinks")

    def _reload(self):
        self._check_control()
        try:
            config = json.loads((self.control / "workspace.json").read_text(encoding="utf-8"))
            if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("server"), str):
                raise ValueError("Invalid workspace metadata")
            if not isinstance(config.get("sparse", []), list):
                raise ValueError("Invalid sparse prefixes")
            revision = config.get("revision")
            if revision is not None and (not isinstance(revision, str) or len(revision) != 64
                                         or any(c not in "0123456789abcdef" for c in revision)):
                raise ValueError("Invalid workspace revision")
            sparse = self._normalize_sparse(config.get("sparse", []))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ScionError(f"Cannot read workspace metadata: {exc}") from exc
        self.config = config
        self.sparse = sparse
        if self._client_override is not None:
            self.client = self._client_override
        elif not hasattr(self, "client") or self.client.server != config["server"]:
            self.client = Client(config["server"])

    @contextmanager
    def _operation(self, recover=True):
        with self._lock.acquire():
            outer = self._operation_depth == 0
            if outer:
                self._reload()
                if recover:
                    outcome = recover_transaction(self)
                    if outcome["recovered"]:
                        self.last_recovery = outcome
                    self._reload()
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1

    def _checkpoint(self, name):
        """Observe transaction milestones; subclasses can instrument fault tests."""

    def recover(self):
        """Recover an interrupted update using only durable local recovery data."""
        with self._operation(recover=False):
            self.last_recovery = recover_transaction(self)
            self._reload()
            return self.last_recovery

    @classmethod
    def find(cls, start=None, **kwargs):
        path = Path(start or os.getcwd()).absolute()
        for root in (path, *path.parents):
            if (root / ".scion" / "workspace.json").exists():
                return cls(root, **kwargs)
        raise ScionError("Not inside a Scion workspace; run scion init first")

    @classmethod
    def init(cls, directory, server, revision="main", sparse=None, client=None, lock_timeout=30):
        selected = Path(directory).expanduser().absolute()
        if selected.is_symlink():
            raise ScionError("Workspace root must not be a symlink")
        root = selected.resolve()
        if root.exists() and (not root.is_dir() or any(p.name != ".scion" for p in root.iterdir())):
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
        control = root / ".scion"
        if control.is_symlink():
            raise ScionError("Workspace metadata must not contain symlinks")
        control.mkdir(exist_ok=True)
        sync_directory(root)
        lock = WorkspaceLock(control / "writer.lock", timeout=lock_timeout)
        with lock.acquire():
            if any(p.name != ".scion" for p in root.iterdir()) or any(p.name != "writer.lock" for p in control.iterdir()):
                raise ScionError("Initialization requires an empty or nonexistent directory")
            config = {"version": 1, "server": api.server, "revision": None,
                      "ref": revision if revision in refs or not refs else "main", "sparse": prefixes}
            durable_json(control / "workspace.json", config)
            workspace = cls(root, client=api, lock_timeout=lock_timeout, _lock=lock)
            if current:
                workspace.update(current)
            return workspace

    @property
    def revision(self):
        return self.config.get("revision")

    def _save(self):
        self._check_control()
        durable_json(self.control / "workspace.json", self.config)
