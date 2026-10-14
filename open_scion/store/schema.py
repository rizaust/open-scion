"""Atomic additive migrations for repository metadata and mutation records."""
from __future__ import annotations

from .errors import ScionError


_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')",
    """CREATE TABLE IF NOT EXISTS changesets (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, tree TEXT NOT NULL, generation INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS refs (
        name TEXT PRIMARY KEY, target TEXT NOT NULL REFERENCES changesets(id))""",
    """CREATE TABLE IF NOT EXISTS ref_audit (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        old_target TEXT REFERENCES changesets(id), new_target TEXT NOT NULL REFERENCES changesets(id),
        created_at TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'legacy',
        operation TEXT NOT NULL DEFAULT 'ref-update', policy TEXT NOT NULL DEFAULT 'null')""",
    "CREATE INDEX IF NOT EXISTS ref_audit_name_sequence ON ref_audit(name, sequence)",
    """CREATE TRIGGER IF NOT EXISTS immutable_ref_audit_update
        BEFORE UPDATE ON ref_audit BEGIN SELECT RAISE(ABORT, 'ref audit records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_ref_audit_delete
        BEFORE DELETE ON ref_audit BEGIN SELECT RAISE(ABORT, 'ref audit records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_changeset_update
        BEFORE UPDATE ON changesets BEGIN SELECT RAISE(ABORT, 'changesets are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_changeset_delete
        BEFORE DELETE ON changesets BEGIN SELECT RAISE(ABORT, 'changesets are immutable'); END""",
    """CREATE TABLE IF NOT EXISTS idempotency_results (
        actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, result_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL, expires_at INTEGER NOT NULL,
        PRIMARY KEY(actor, operation, key))""",
    "CREATE INDEX IF NOT EXISTS idempotency_expiry ON idempotency_results(expires_at)",
    """CREATE TRIGGER IF NOT EXISTS immutable_idempotency_update
        BEFORE UPDATE ON idempotency_results BEGIN SELECT RAISE(ABORT, 'idempotency results are immutable'); END""",
)
_AUDIT_COLUMNS = {
    "actor": "TEXT NOT NULL DEFAULT 'legacy'",
    "operation": "TEXT NOT NULL DEFAULT 'ref-update'",
    "policy": "TEXT NOT NULL DEFAULT 'null'",
}


def initialize_schema(db) -> None:
    """Preserve existing event IDs while extending the schema in one transaction."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version != ("1",):
                raise ScionError("Unsupported storage schema", "unsupported", 500)
        for statement in _STATEMENTS:
            db.execute(statement)
        columns = {row[1] for row in db.execute("PRAGMA table_info(ref_audit)")}
        for name, definition in _AUDIT_COLUMNS.items():
            if name not in columns:
                db.execute(f"ALTER TABLE ref_audit ADD COLUMN {name} {definition}")
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def audit_projection(db) -> str:
    """Read historical snapshots before authenticated-audit columns existed."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(ref_audit)")}
    additions = [name if name in columns else fallback for name, fallback in
                 (("actor", "'legacy'"), ("operation", "'ref-update'"), ("policy", "'null'"))]
    return "sequence,name,old_target,new_target,created_at," + ",".join(additions)
