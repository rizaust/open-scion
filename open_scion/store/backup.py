"""Consistent online snapshots and verified repository restoration."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .codec import _json
from .errors import ScionError
from .repository import Store
from .snapshot_files import _copy_file, _digest_file, _new_destination, _publish_directory, _sync_directories
from .snapshot_manifest import _FORMAT, _VERSION, _load_manifest, _validate_layout


def _snapshot_objects(source: Store, snapshot: Store) -> set[str]:
    pending = [(source._empty_tree, "tree")]
    with snapshot._connection() as db:
        for row in db.execute("SELECT id FROM changesets"):
            record = snapshot._get_changeset(db, row[0])
            pending.append((record["tree"], "tree"))
            pending.extend((entry["blob"], "blob") for entry in record["changes"].values() if entry is not None)
    visited = {}
    while pending:
        oid, expected = pending.pop()
        if oid in visited:
            if visited[oid] != expected:
                raise ScionError(f"Object type mismatch: {oid}", "corrupt", 500)
            continue
        kind, _ = source._read_object(oid, expected)
        visited[oid] = kind
        if kind == "tree":
            pending.extend((entry["oid"], entry["type"]) for entry in source._read_tree(oid).values())
    return set(visited)

def create_backup(store: Store, destination) -> dict:
    """Snapshot all committed changesets and their objects, including unlanded work."""
    target = _new_destination(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.backup-", dir=target.parent))
    try:
        (staging / "objects").mkdir()
        database = staging / "scion.db"
        with store._connection() as source:
            source.execute("BEGIN")
            source.execute("SELECT COUNT(*) FROM changesets").fetchone()
            with closing(sqlite3.connect(database)) as output:
                source.backup(output, pages=256)
                output.execute("PRAGMA journal_mode=DELETE")
        with database.open("r+b") as stream:
            os.fsync(stream.fileno())
        snapshot = Store(staging, read_only=True)
        files = {"scion.db": _digest_file(database)}
        for oid in sorted(_snapshot_objects(store, snapshot)):
            relative = f"objects/{oid[:2]}/{oid[2:]}"
            files[relative] = _copy_file(store._object_path(oid), staging / relative)
        check = snapshot.fsck()
        if not check["ok"]:
            raise ScionError("Snapshot integrity verification failed", "corrupt", 500, check)
        counts = {key: check[key] for key in ("objects", "changesets", "refs", "ref_events")}
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        manifest = {"format": _FORMAT, "format_version": _VERSION, "created_at": created_at,
                    "files": files, "counts": counts}
        with (staging / "manifest.json").open("xb") as stream:
            stream.write(_json(manifest) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directories(staging)
        _publish_directory(staging, target)
        return {"path": str(target), "format_version": _VERSION, "created_at": created_at,
                "bytes": sum(item["size"] for item in files.values()), **counts}
    finally:
        if staging.exists():
            shutil.rmtree(staging)

def restore_backup(snapshot, destination) -> dict:
    """Verify a snapshot in isolation, then publish a new repository atomically."""
    source = Path(snapshot).expanduser().absolute()
    target = _new_destination(destination)
    manifest = _load_manifest(source)
    _validate_layout(source, manifest["files"])
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        (staging / "objects").mkdir()
        for name, entry in sorted(manifest["files"].items()):
            # Each parent is checked again immediately before copying.
            relative = Path(name)
            for parent in relative.parents:
                if (source / parent).is_symlink():
                    raise ScionError("Symlink in snapshot file path", "corrupt_backup", 400)
            _copy_file(source / relative, staging / relative, entry)
        try:
            check = Store(staging, read_only=True).fsck()
        except (ScionError, sqlite3.DatabaseError, OSError) as exc:
            raise ScionError(f"Snapshot database validation failed: {exc}", "corrupt_backup", 400) from exc
        if not check["ok"]:
            raise ScionError("Snapshot content integrity verification failed", "corrupt_backup", 400, check)
        if any(check[key] != value for key, value in manifest["counts"].items()):
            raise ScionError("Snapshot counts disagree with manifest", "corrupt_backup", 400)
        _sync_directories(staging)
        _publish_directory(staging, target)
        return {"path": str(target), "restored": True, "format_version": _VERSION, "check": check,
                **manifest["counts"]}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
