"""Snapshot format validation, checksums, and safe on-disk inventory."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re

from .errors import ScionError
from .snapshot_files import _read_regular

_FORMAT = "open-scion-backup"
_VERSION = 1
_OBJECT_FILE = re.compile(r"objects/[0-9a-f]{2}/[0-9a-f]{62}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
