"""Repository statistics and snapshot-consistent integrity verification."""
from __future__ import annotations

import sqlite3

from .errors import ScionError
from .mutation_integrity import read_audit, verify_audit, verify_idempotency
from .validation import validate_ref


class IntegrityStoreMixin:
    def stats(self) -> dict:
        counts = {"objects": 0, "blobs": 0, "trees": 0, "compressed_bytes": 0}
        for path in self._object_files():
            kind, _ = self._read_object(path.parent.name + path.name)
            counts["objects"] += 1
            counts[kind + "s"] += 1
            counts["compressed_bytes"] += path.stat().st_size
        with self._connection() as db:
            counts["changesets"] = db.execute("SELECT COUNT(*) FROM changesets").fetchone()[0]
            counts["refs"] = db.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        return counts

    def fsck(self) -> dict:
        """Verify a pinned metadata snapshot and all observed immutable objects."""
        errors = []
        changesets = {}
        refs = []
        audit = []
        idempotency_records = 0
        try:
            with self._connection() as db:
                if not db.in_transaction:
                    db.execute("BEGIN")
                # Establish the WAL snapshot before enumerating object files.
                rows = list(db.execute("SELECT id FROM changesets"))
                for result in db.execute("PRAGMA integrity_check"):
                    if result[0] != "ok":
                        errors.append(f"SQLite: {result[0]}")
                for result in db.execute("PRAGMA foreign_key_check"):
                    errors.append(f"SQLite foreign key violation: {result}")
                for row in rows:
                    try:
                        changesets[row[0]] = self._get_changeset(db, row[0])
                    except (ScionError, TypeError, KeyError, ValueError) as exc:
                        errors.append(str(exc))
                refs = list(db.execute("SELECT name,target FROM refs"))
                audit = read_audit(db)
                idempotency_records = verify_idempotency(db, errors)
        except (OSError, sqlite3.DatabaseError, ScionError) as exc:
            errors.append(f"Metadata: {exc}")
        pending = []
        for record in changesets.values():
            pending.append((record["tree"], "tree"))
            pending.extend((entry["blob"], "blob") for entry in record["changes"].values() if entry is not None)
        pending.append((self._empty_tree, "tree"))
        try:
            pending.extend((path.parent.name + path.name, None) for path in self._object_files())
        except (OSError, ScionError) as exc:
            errors.append(f"Object inventory: {exc}")
        object_kinds = {}
        failed = set()
        while pending:
            oid, expected = pending.pop()
            if oid in failed:
                continue
            if oid in object_kinds:
                if expected is not None and object_kinds[oid] != expected:
                    errors.append(f"Object {oid} has type {object_kinds[oid]}, expected {expected}")
                continue
            try:
                kind, _ = self._read_object(oid)
                object_kinds[oid] = kind
                if expected is not None and kind != expected:
                    errors.append(f"Object {oid} has type {kind}, expected {expected}")
                if kind == "tree":
                    # Follow edges directly: a concurrent tree may appear after its
                    # child directory was scanned, even though the child is durable.
                    pending.extend((entry["oid"], entry["type"]) for entry in self._read_tree(oid).values())
            except (OSError, ScionError) as exc:
                failed.add(oid)
                errors.append(str(exc))
        ref_targets = dict(refs)
        for name, target in refs:
            try:
                validate_ref(name)
            except ScionError as exc:
                errors.append(str(exc))
            if target not in changesets:
                errors.append(f"Ref {name!r} references missing changeset {target}")
        for oid, record in changesets.items():
            parents = record["parents"]
            if any(parent not in changesets for parent in parents):
                errors.append(f"Changeset {oid} references missing parent")
            elif record["generation"] != max((changesets[p]["generation"] for p in parents), default=-1) + 1:
                errors.append(f"Changeset {oid} has invalid generation")
        verify_audit(audit, changesets, ref_targets, errors)
        return {"ok": not errors, "errors": errors, "objects": len(object_kinds),
                "changesets": len(changesets), "refs": len(refs), "ref_events": len(audit),
                "idempotency_records": idempotency_records}
