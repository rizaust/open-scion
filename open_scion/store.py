"""Immutable content storage, persistent trees, changesets, and atomic refs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterator
import zlib


_OID = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_MODES = {"100644", "100755"}


class ScionError(Exception):
    """A stable error that can be represented by both CLI and HTTP clients."""

    def __init__(self, message: str, code: str = "invalid", status: int = 400, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details


def validate_path(path: str) -> str:
    """Require a normalized, relative, regular-file path."""
    if not isinstance(path, str) or not path or len(path.encode("utf-8", errors="replace")) > 4096:
        raise ScionError("Path must be a nonempty relative POSIX path of at most 4096 bytes")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(prefix + number for prefix in ("COM", "LPT") for number in "123456789¹²³")
    if any(ord(char) < 32 or char in '\\<>:"|?*' for char in path) or any(
        part.casefold() in {"", ".", "..", ".git", ".scion"}
        or part.rstrip(" .") != part or part.split(".")[0].upper() in reserved
        for part in path.split("/")
    ):
        raise ScionError(f"Unsafe or non-normalized path: {path!r}")
    if len(path.split("/")) > 256:
        raise ScionError("Path must not exceed 256 components")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ScionError("Path must contain valid Unicode") from exc
    return path


def validate_prefix(prefix: str) -> str:
    """Validate an optional directory or file prefix."""
    if prefix == "":
        return prefix
    return validate_path(prefix)


def validate_ref(name: str) -> str:
    """Require an unambiguous, portable ref name."""
    if not isinstance(name, str) or not _REF.fullmatch(name) or _OID.fullmatch(name):
        raise ScionError("Invalid ref name")
    if any(part in {"", ".", "..", ".git", ".scion"} or part.startswith(".") or
           part.endswith(".") or part.endswith(".lock") for part in name.split("/")):
        raise ScionError("Invalid ref name")
    if ".." in name:
        raise ScionError("Invalid ref name")
    return name


def _envelope(kind: str, payload: bytes) -> bytes:
    return kind.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0" + payload


def blob_id(data: bytes) -> str:
    """Return the SHA-256 identity of an uncompressed typed blob."""
    if not isinstance(data, bytes):
        raise ScionError("Blob data must be bytes")
    return hashlib.sha256(_envelope("blob", data)).hexdigest()


def _json(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _identity(kind: str, data) -> str:
    return hashlib.sha256(_envelope(kind, _json(data))).hexdigest()


def _validate_oid(oid: str) -> str:
    if not isinstance(oid, str) or not _OID.fullmatch(oid):
        raise ScionError("Object ID must be 64 lowercase hexadecimal characters")
    return oid


class Store:
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

    def _object_path(self, oid: str) -> Path:
        _validate_oid(oid)
        directory = self.objects / oid[:2]
        path = directory / oid[2:]
        if directory.is_symlink() or path.is_symlink():
            raise ScionError(f"Symlink in object storage: {oid}", "corrupt", 500)
        return path

    def _write_object(self, kind: str, payload: bytes) -> str:
        if self.read_only:
            raise ScionError("Repository is read-only", "read_only", 403)
        envelope = _envelope(kind, payload)
        oid = hashlib.sha256(envelope).hexdigest()
        path = self._object_path(oid)
        if path.exists():
            self._read_object(oid, kind)
            return oid
        path.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(zlib.compress(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                if os.name != "nt":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except FileExistsError:
                self._read_object(oid, kind)
        finally:
            os.unlink(temporary)
        return oid

    def _read_object(self, oid: str, kind: str | None = None) -> tuple[str, bytes]:
        path = self._object_path(oid)
        try:
            compressed = path.read_bytes()
        except FileNotFoundError as exc:
            raise ScionError(f"Object not found: {oid}", "not_found", 404) from exc
        try:
            decoder = zlib.decompressobj()
            envelope = decoder.decompress(compressed) + decoder.flush()
            if not decoder.eof or decoder.unused_data:
                raise ValueError("Invalid compressed stream")
            if hashlib.sha256(envelope).hexdigest() != oid:
                raise ValueError("Hash mismatch")
            header, payload = envelope.split(b"\0", 1)
            type_bytes, length_bytes = header.split(b" ", 1)
            stored_kind = type_bytes.decode("ascii")
            if stored_kind not in {"blob", "tree"} or length_bytes != str(len(payload)).encode("ascii"):
                raise ValueError("Invalid object header")
            if kind is not None and stored_kind != kind:
                raise ScionError(f"Expected {kind} object: {oid}")
            return stored_kind, payload
        except (zlib.error, ValueError, UnicodeError) as exc:
            raise ScionError(f"Corrupt object {oid}: {exc}", "corrupt", 500) from exc

    def put_blob(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise ScionError("Blob data must be bytes")
        return self._write_object("blob", data)

    def get_blob(self, oid: str) -> bytes:
        return self._read_object(oid, "blob")[1]

    def _write_tree(self, entries: dict) -> str:
        return self._write_object("tree", _json(entries))

    def _read_tree(self, oid: str) -> dict:
        try:
            entries = json.loads(self._read_object(oid, "tree")[1])
            if not isinstance(entries, dict):
                raise ValueError("Tree must be an object")
            for name, entry in entries.items():
                validate_path(name)
                if "/" in name or not isinstance(entry, dict):
                    raise ValueError("Invalid tree entry")
                _validate_oid(entry.get("oid"))
                if entry.get("type") == "tree":
                    if set(entry) != {"type", "oid"}:
                        raise ValueError("Invalid directory entry")
                elif entry.get("type") == "blob":
                    if set(entry) != {"type", "oid", "mode"} or not isinstance(entry.get("mode"), str) or entry["mode"] not in _MODES:
                        raise ValueError("Invalid file entry")
                else:
                    raise ValueError("Invalid tree entry type")
            return entries
        except (ValueError, TypeError, ScionError) as exc:
            if isinstance(exc, ScionError) and exc.code in {"not_found", "corrupt"}:
                raise
            raise ScionError(f"Corrupt tree {oid}: {exc}", "corrupt", 500) from exc

    def _patch_tree(self, oid: str, changes: list[tuple[list[str], dict | None]]) -> str:
        entries = self._read_tree(oid)
        original = dict(entries)
        groups: dict[str, list] = {}
        for parts, value in changes:
            groups.setdefault(parts[0], []).append((parts[1:], value))
        for name, group in sorted(groups.items()):
            direct = [(parts, value) for parts, value in group if not parts]
            nested = [(parts, value) for parts, value in group if parts]
            old = entries.get(name)
            if direct:
                if nested:
                    raise ScionError(f"File/directory collision at {name}")
                value = direct[0][1]
                if old and old["type"] == "tree":
                    raise ScionError(f"Path names a directory: {name}")
                if value is None:
                    entries.pop(name, None)
                else:
                    entries[name] = {"type": "blob", "oid": value["blob"], "mode": value["mode"]}
            else:
                if old and old["type"] != "tree":
                    raise ScionError(f"Path descends through a file: {name}")
                if old is None and all(value is None for _, value in nested):
                    continue
                child = self._patch_tree(old["oid"] if old else self._empty_tree, nested)
                if child == self._empty_tree:
                    entries.pop(name, None)
                else:
                    entries[name] = {"type": "tree", "oid": child}
        return oid if entries == original else self._write_tree(entries)

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

    def tree(self, revision: str, prefix: str = "") -> dict[str, dict]:
        validate_prefix(prefix)
        root = self.get_changeset(revision)["tree"]
        output = {}
        stack = [(root, "")]
        while stack:
            oid, base = stack.pop()
            for name, entry in self._read_tree(oid).items():
                path = f"{base}/{name}" if base else name
                if entry["type"] == "tree":
                    if not prefix or path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/"):
                        stack.append((entry["oid"], path))
                elif not prefix or path == prefix or path.startswith(prefix + "/"):
                    output[path] = {"blob": entry["oid"], "mode": entry["mode"]}
        return dict(sorted(output.items()))

    def list_refs(self) -> dict[str, str]:
        with self._connection() as db:
            return dict(db.execute("SELECT name,target FROM refs ORDER BY name"))

    def update_ref(self, name: str, new: str, expected: str | None) -> dict:
        validate_ref(name)
        if expected is not None:
            _validate_oid(expected)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._resolve(db, new)
            row = db.execute("SELECT target FROM refs WHERE name=?", (name,)).fetchone()
            current = row[0] if row else None
            if current != expected:
                raise ScionError(f"Ref {name!r} changed; expected {expected}, found {current}", "stale_ref", 409,
                                 {"ref": name, "expected": expected, "actual": current})
            db.execute("INSERT INTO refs(name,target) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target",
                       (name, target))
            db.execute("INSERT INTO ref_audit(name,old_target,new_target,created_at) VALUES(?,?,?,?)",
                       (name, current, target, datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")))
            db.commit()
        return {"name": name, "target": target}

    def ref_log(self, name: str | None = None, limit: int = 100) -> list[dict]:
        """Read committed ref transitions, newest first; failed CAS writes no event."""
        if name is not None:
            validate_ref(name)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ScionError("Ref log limit must be between 1 and 10000")
        query = "SELECT sequence,name,old_target,new_target,created_at FROM ref_audit"
        values = []
        if name is not None:
            query += " WHERE name=?"
            values.append(name)
        query += " ORDER BY sequence DESC LIMIT ?"
        values.append(limit)
        with self._connection() as db:
            return [dict(zip(("sequence", "name", "old", "new", "created_at"), row))
                    for row in db.execute(query, values)]

    def backup(self, destination) -> dict:
        """Publish a verified online snapshot containing all committed changesets."""
        from .backup import create_backup
        return create_backup(self, destination)

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

    def _object_files(self):
        for directory in sorted(self.objects.iterdir()):
            if len(directory.name) == 2 and re.fullmatch(r"[0-9a-f]{2}", directory.name) and directory.is_dir():
                for path in sorted(directory.iterdir()):
                    if len(path.name) == 62 and _OID.fullmatch(directory.name + path.name):
                        yield path

    def fsck(self) -> dict:
        """Verify a pinned metadata snapshot and all observed immutable objects."""
        errors = []
        changesets = {}
        refs = []
        audit = []
        try:
            with self._connection() as db:
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
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ref_audit'").fetchone():
                    audit = list(db.execute("SELECT sequence,name,old_target,new_target,created_at FROM ref_audit ORDER BY sequence"))
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
        audit_targets = {}
        for sequence, name, old, new, created_at in audit:
            try:
                validate_ref(name)
                if not isinstance(created_at, str) or not created_at.endswith("Z"):
                    raise ValueError("Invalid UTC timestamp")
                datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if new not in changesets or old is not None and old not in changesets:
                    raise ValueError("Missing changeset")
                if name in audit_targets and old != audit_targets[name]:
                    raise ValueError("Discontinuous ref history")
                audit_targets[name] = new
            except (ScionError, ValueError, TypeError) as exc:
                errors.append(f"Ref audit {sequence}: {exc}")
        for name, target in audit_targets.items():
            if ref_targets.get(name) != target:
                errors.append(f"Ref audit disagrees with current ref {name!r}")
        return {"ok": not errors, "errors": errors, "objects": len(object_kinds),
                "changesets": len(changesets), "refs": len(refs), "ref_events": len(audit)}
