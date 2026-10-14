"""Consistent online snapshots and verified, non-overwriting restoration."""
from __future__ import annotations

import ctypes
from contextlib import closing
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile

from .store import ScionError, Store, _json

_FORMAT = "open-scion-backup"
_VERSION = 1
_OBJECT_FILE = re.compile(r"objects/[0-9a-f]{2}/[0-9a-f]{62}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _new_destination(destination) -> Path:
    supplied = Path(destination).expanduser().absolute()
    if supplied.exists() or supplied.is_symlink():
        raise ScionError(f"Destination already exists: {supplied}", "already_exists", 409)
    if not supplied.parent.is_dir():
        raise ScionError("Destination parent directory must already exist")
    return supplied.parent.resolve() / supplied.name


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_directories(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False):
        _sync_directory(Path(directory))


def _publish_directory(staging: Path, destination: Path) -> None:
    """Atomically rename without ever replacing an existing destination."""
    if os.name == "nt":
        os.rename(staging, destination)
    elif sys.platform in {"linux", "darwin"}:
        library = ctypes.CDLL(None, use_errno=True)
        source_bytes, destination_bytes = os.fsencode(staging), os.fsencode(destination)
        if sys.platform == "darwin":
            rename = library.renamex_np
            rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            result = rename(source_bytes, destination_bytes, 0x00000004)
        else:
            rename = getattr(library, "renameat2", None)
            if rename is None:
                raise ScionError("Atomic no-replace rename is unavailable on this platform", "unsupported", 500)
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            result = rename(-100, source_bytes, -100, destination_bytes, 1)
        if result:
            number = ctypes.get_errno()
            if number in {errno.EEXIST, errno.ENOTEMPTY}:
                raise ScionError(f"Destination already exists: {destination}", "already_exists", 409)
            raise OSError(number, os.strerror(number), str(destination))
    else:
        raise ScionError("Atomic no-replace rename is unavailable on this platform", "unsupported", 500)
    _sync_directory(destination.parent)


def _read_regular(path: Path):
    """Open a regular file without following a final-component symlink."""
    if path.is_symlink():
        raise ScionError(f"Symlinks are not allowed in snapshots: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ScionError(f"Snapshot entry is not a regular file: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _copy_file(source: Path, destination: Path, expected: dict | None = None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with _read_regular(source) as incoming, destination.open("xb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    result = {"sha256": digest.hexdigest(), "size": size}
    if expected is not None and result != expected:
        raise ScionError(f"Snapshot checksum mismatch: {source.name}", "corrupt_backup", 400)
    return result


def _digest_file(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with _read_regular(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


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


def _load_manifest(snapshot: Path) -> dict:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ScionError("Snapshot must be a directory, not a symlink")
    try:
        with _read_regular(snapshot / "manifest.json") as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ScionError("Backup manifest exceeds 64 MiB")
            manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get("format") != _FORMAT or manifest.get("format_version") != _VERSION:
            raise ValueError("Unsupported backup format")
        if isinstance(manifest["format_version"], bool):
            raise ValueError("Invalid format version")
        files = manifest.get("files")
        if not isinstance(files, dict) or "scion.db" not in files:
            raise ValueError("Backup manifest must include scion.db")
        for name, entry in files.items():
            if not isinstance(name, str) or name != "scion.db" and not _OBJECT_FILE.fullmatch(name):
                raise ValueError(f"Unsafe manifest path: {name!r}")
            if not isinstance(entry, dict) or set(entry) != {"sha256", "size"}:
                raise ValueError("Invalid file manifest entry")
            if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
                raise ValueError("Invalid SHA-256 digest")
            if isinstance(entry["size"], bool) or not isinstance(entry["size"], int) or entry["size"] < 0:
                raise ValueError("Invalid file size")
        counts = manifest.get("counts")
        if not isinstance(counts, dict) or set(counts) != {"objects", "changesets", "refs", "ref_events"}:
            raise ValueError("Invalid snapshot counts")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("Invalid snapshot count")
        if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"].endswith("Z"):
            raise ValueError("Invalid snapshot timestamp")
        datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
        return manifest
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ScionError(f"Invalid backup manifest: {exc}", "corrupt_backup", 400) from exc


def _validate_layout(snapshot: Path, files: dict) -> None:
    expected = set(files) | {"manifest.json"}
    observed = set()
    for directory, subdirectories, names in os.walk(snapshot, followlinks=False):
        for name in subdirectories:
            child = Path(directory) / name
            relative = child.relative_to(snapshot).as_posix()
            if child.is_symlink() or not (relative == "objects" or re.fullmatch(r"objects/[0-9a-f]{2}", relative)):
                raise ScionError(f"Unexpected snapshot directory: {relative}", "corrupt_backup", 400)
        for name in names:
            child = Path(directory) / name
            relative = child.relative_to(snapshot).as_posix()
            if child.is_symlink() or not child.is_file() or relative not in expected:
                raise ScionError(f"Unexpected snapshot file: {relative}", "corrupt_backup", 400)
            observed.add(relative)
    if observed != expected:
        raise ScionError("Snapshot has missing files", "corrupt_backup", 400,
                         {"missing": sorted(expected - observed)})


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
