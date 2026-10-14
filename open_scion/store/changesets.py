"""Immutable changeset creation, validation, and first-parent history."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from .codec import _identity, _json
from .errors import ScionError
from .validation import _MODES, _validate_oid, validate_path


class ChangesetStoreMixin:
    def _get_changeset(self, db: sqlite3.Connection, revision: str) -> dict:
        oid = self._resolve(db, revision)
        row = db.execute("SELECT data, tree, generation FROM changesets WHERE id=?", (oid,)).fetchone()
        try:
            record = json.loads(row[0])
            if not isinstance(record, dict) or _identity("changeset", record) != oid:
                raise ValueError("Changeset hash mismatch")
            if set(record) != {"parents", "changes", "message", "author", "created_at", "tree", "generation"}:
                raise ValueError("Invalid changeset fields")
            if record["tree"] != row[1] or record["generation"] != row[2]:
                raise ValueError("Changeset indexes disagree")
            self._validate_record(record)
            return {"id": oid, **record}
        except (ValueError, TypeError, KeyError, ScionError) as exc:
            raise ScionError(f"Corrupt changeset {oid}: {exc}", "corrupt", 500) from exc

    @staticmethod
    def _validate_record(record: dict) -> None:
        parents = record["parents"]
        if not isinstance(parents, list) or len(parents) > 2:
            raise ValueError("Invalid parent list")
        for parent in parents:
            _validate_oid(parent)
        if len(set(parents)) != len(parents):
            raise ValueError("Duplicate parents")
        _validate_oid(record["tree"])
        generation = record["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("Invalid generation")
        if not isinstance(record["message"], str) or not isinstance(record["author"], str) or not record["author"].strip():
            raise ValueError("Invalid changeset message or author")
        timestamp = record["created_at"]
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError("Invalid changeset timestamp")
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(record["changes"], dict):
            raise ValueError("Invalid changeset delta")
        for path, entry in record["changes"].items():
            validate_path(path)
            if entry is not None:
                if (not isinstance(entry, dict) or set(entry) != {"blob", "mode"}
                        or not isinstance(entry.get("mode"), str) or entry["mode"] not in _MODES):
                    raise ValueError("Invalid changeset entry")
                _validate_oid(entry["blob"])

    def get_changeset(self, revision: str) -> dict:
        with self._connection() as db:
            return self._get_changeset(db, revision)

    def create_changeset(self, parents: list[str], changes: dict[str, dict | None], message: str, author: str) -> dict:
        if self.read_only:
            raise ScionError("Repository is read-only", "read_only", 403)
        if not isinstance(parents, list) or len(parents) > 2 or not all(isinstance(p, str) for p in parents):
            raise ScionError("A changeset must have zero, one, or two parents")
        if not isinstance(changes, dict):
            raise ScionError("Changes must map paths to file entries or null")
        if not isinstance(message, str) or not isinstance(author, str) or not author.strip():
            raise ScionError("Message must be text and author must be nonempty text")
        normalized = {}
        for path, entry in changes.items():
            validate_path(path)
            if entry is None:
                normalized[path] = None
                continue
            if not isinstance(entry, dict) or set(entry) != {"blob", "mode"} or not isinstance(entry.get("mode"), str) or entry["mode"] not in _MODES:
                raise ScionError(f"Invalid regular-file entry: {path}")
            self.get_blob(entry["blob"])
            normalized[path] = {"blob": entry["blob"], "mode": entry["mode"]}
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            parent_records = [self._get_changeset(db, parent) for parent in parents]
            parent_ids = [parent["id"] for parent in parent_records]
            if len(set(parent_ids)) != len(parent_ids):
                raise ScionError("Parents must be distinct")
            tree = parent_records[0]["tree"] if parent_records else self._empty_tree
            deleted = [(path.split("/"), value) for path, value in normalized.items() if value is None]
            added = [(path.split("/"), value) for path, value in normalized.items() if value is not None]
            if deleted:
                tree = self._patch_tree(tree, deleted)
            if added:
                tree = self._patch_tree(tree, added)
            record = {
                "parents": parent_ids,
                "changes": normalized,
                "message": message,
                "author": author,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "tree": tree,
                "generation": max((parent["generation"] for parent in parent_records), default=-1) + 1,
            }
            oid = _identity("changeset", record)
            db.execute("INSERT INTO changesets(id,data,tree,generation) VALUES(?,?,?,?)",
                       (oid, _json(record).decode("utf-8"), tree, record["generation"]))
            db.commit()
        return {"id": oid, **record}

    def log(self, revision: str, limit: int = 50, path: str | None = None) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
            raise ScionError("Log limit must be between 1 and 10000")
        if path is not None:
            validate_path(path)
        output = []
        with self._connection() as db:
            db.execute("BEGIN")
            current = self._get_changeset(db, revision)
            while len(output) < limit:
                if path is None or any(name == path or name.startswith(path + "/") for name in current["changes"]):
                    output.append(current)
                if not current["parents"]:
                    break
                current = self._get_changeset(db, current["parents"][0])
        return output
