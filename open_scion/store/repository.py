"""Repository lifecycle and the public Store facade."""
from __future__ import annotations

from pathlib import Path

from .actors import ActorStoreMixin
from .changesets import ChangesetStoreMixin
from .codec import _identity
from .errors import ScionError
from .idempotency import (DEFAULT_MAX_RECORDS, DEFAULT_MAX_RESULT_BYTES, DEFAULT_RETENTION_SECONDS,
                          IdempotencyStoreMixin)
from .integrity import IntegrityStoreMixin
from .objects import ObjectStoreMixin
from .refs import RefStoreMixin
from .schema import initialize_schema
from .transactions import TransactionStoreMixin
from .trees import TreeStoreMixin


class Store(ObjectStoreMixin, TreeStoreMixin, ChangesetStoreMixin, RefStoreMixin,
            IntegrityStoreMixin, IdempotencyStoreMixin, ActorStoreMixin, TransactionStoreMixin):
    """Thread-safe storage using atomic object installation and SQLite WAL."""

    def __init__(self, root, *, read_only: bool = False,
                 idempotency_retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                 idempotency_max_records: int = DEFAULT_MAX_RECORDS,
                 idempotency_max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES):
        self.root = Path(root).expanduser().absolute()
        self.read_only = read_only
        self._initialize_transactions()
        self._initialize_actors()
        self._configure_idempotency(idempotency_retention_seconds, idempotency_max_records,
                                    idempotency_max_result_bytes)
        if read_only and not self.root.is_dir():
            raise ScionError("Repository does not exist", "not_found", 404)
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        if self.objects.is_symlink():
            raise ScionError("Object directory must not be a symlink")
        if read_only:
            if not self.objects.is_dir():
                raise ScionError("Object directory is missing", "corrupt", 500)
        else:
            self.objects.mkdir(exist_ok=True)
        self.db_path = self.root / "scion.db"
        if self.db_path.is_symlink():
            raise ScionError("Database must not be a symlink")
        if read_only and not self.db_path.is_file():
            raise ScionError("Repository database is missing", "not_found", 404)
        if read_only:
            self._empty_tree = _identity("tree", {})
            with self._connection() as db:
                version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version != ("1",):
                    raise ScionError("Unsupported storage schema", "unsupported", 500)
            return
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            initialize_schema(db)
        self._empty_tree = self._write_tree({})

    def backup(self, destination) -> dict:
        """Publish a verified online snapshot containing all committed changesets."""
        from .backup import create_backup
        return create_backup(self, destination)
