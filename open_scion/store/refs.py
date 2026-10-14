"""Revision resolution, exact ref compare-and-swap, and immutable audit."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import json

from .actors import policy_json
from .errors import ScionError
from .schema import audit_projection
from .validation import _OID, _validate_oid, validate_ref


class RefStoreMixin:
    def _resolve(self, db: sqlite3.Connection, revision: str) -> str:
        if not isinstance(revision, str) or not revision:
            raise ScionError("Revision must be an ID or ref name")
        if _OID.fullmatch(revision):
            found = db.execute("SELECT id FROM changesets WHERE id=?", (revision,)).fetchone()
        else:
            validate_ref(revision)
            found = db.execute("SELECT target FROM refs WHERE name=?", (revision,)).fetchone()
        if not found:
            raise ScionError(f"Unknown revision: {revision}", "not_found", 404)
        return found[0]

    def resolve(self, revision: str) -> str:
        with self._connection() as db:
            return self._resolve(db, revision)

    def list_refs(self) -> dict[str, str]:
        with self._connection() as db:
            return dict(db.execute("SELECT name,target FROM refs ORDER BY name"))

    def update_ref(self, name: str, new: str, expected: str | None) -> dict:
        if self.read_only:
            raise ScionError("Repository is read-only", "read_only", 403)
        validate_ref(name)
        if expected is not None:
            _validate_oid(expected)
        with self._write_transaction() as db:
            target = self._resolve(db, new)
            row = db.execute("SELECT target FROM refs WHERE name=?", (name,)).fetchone()
            current = row[0] if row else None
            if current != expected:
                raise ScionError(f"Ref {name!r} changed; expected {expected}, found {current}", "stale_ref", 409,
                                 {"ref": name, "expected": expected, "actual": current})
            policy = policy_json(self._authorize_ref(name, current, target))
            actor, operation = self._actor_context.get()
            db.execute("INSERT INTO refs(name,target) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target",
                       (name, target))
            db.execute("""INSERT INTO ref_audit
                (name,old_target,new_target,created_at,actor,operation,policy) VALUES(?,?,?,?,?,?,?)""",
                       (name, current, target, datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        actor, operation, policy))
        return {"name": name, "target": target}

    def ref_log(self, name: str | None = None, limit: int = 100) -> list[dict]:
        """Read committed ref transitions, newest first; failed CAS writes no event."""
        if name is not None:
            validate_ref(name)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ScionError("Ref log limit must be between 1 and 10000")
        values = []
        with self._connection() as db:
            query = "SELECT " + audit_projection(db) + " FROM ref_audit"
            if name is not None:
                query += " WHERE name=?"
                values.append(name)
            query += " ORDER BY sequence DESC LIMIT ?"
            values.append(limit)
            records = []
            for row in db.execute(query, values):
                record = dict(zip(("sequence", "name", "old", "new", "created_at", "actor", "operation", "policy"), row))
                try:
                    record["policy"] = json.loads(record["policy"])
                except (TypeError, ValueError) as exc:
                    raise ScionError("Corrupt ref audit policy information", "corrupt", 500) from exc
                records.append(record)
            return records
