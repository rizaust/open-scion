"""Repository lifecycle and the public Store facade."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .changesets import ChangesetStoreMixin
from .codec import _identity
from .errors import ScionError
from .integrity import IntegrityStoreMixin
from .objects import ObjectStoreMixin
from .refs import RefStoreMixin
from .trees import TreeStoreMixin


class Store(ObjectStoreMixin, TreeStoreMixin, ChangesetStoreMixin, RefStoreMixin, IntegrityStoreMixin):
    """Thread-safe storage using atomic object installation and SQLite WAL."""

    def __init__(self, root, *, read_only: bool = False):
        self.root = Path(root).expanduser().absolute()
        self.read_only = read_only
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS changesets (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    tree TEXT NOT NULL,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refs (
                    name TEXT PRIMARY KEY,
                    target TEXT NOT NULL REFERENCES changesets(id)
                );
                CREATE TABLE IF NOT EXISTS ref_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    old_target TEXT REFERENCES changesets(id),
                    new_target TEXT NOT NULL REFERENCES changesets(id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ref_audit_name_sequence
                    ON ref_audit(name, sequence);
                CREATE TRIGGER IF NOT EXISTS immutable_ref_audit_update
                BEFORE UPDATE ON ref_audit BEGIN
                    SELECT RAISE(ABORT, 'ref audit records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_ref_audit_delete
                BEFORE DELETE ON ref_audit BEGIN
                    SELECT RAISE(ABORT, 'ref audit records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_changeset_update
                BEFORE UPDATE ON changesets BEGIN
                    SELECT RAISE(ABORT, 'changesets are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_changeset_delete
                BEFORE DELETE ON changesets BEGIN
                    SELECT RAISE(ABORT, 'changesets are immutable');
                END;
            """)
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
            if version != "1":
                raise ScionError(f"Unsupported storage schema: {version}", "unsupported", 500)
        self._empty_tree = self._write_tree({})

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        location = self.db_path.as_uri() + "?mode=ro" if self.read_only else self.db_path
        db = sqlite3.connect(location, uri=self.read_only, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def backup(self, destination) -> dict:
        """Publish a verified online snapshot containing all committed changesets."""
        from .backup import create_backup
        return create_backup(self, destination)
