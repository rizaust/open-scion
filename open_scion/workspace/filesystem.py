"""Durable file installation and path-safe workspace transaction primitives."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from ..store import ScionError, blob_id, validate_path


def sync_directory(path: Path) -> None:
    """Persist directory entries where the operating system exposes fsync."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_json(path: Path, value) -> None:
    """Publish a complete, flushed JSON document with an atomic rename."""
    if path.is_symlink():
        raise ScionError("Workspace metadata must not contain symlinks")
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _transaction_file(transaction, folder, relative):
    validate_path(relative)
    path = transaction / folder / relative
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ScionError("Symlink in workspace recovery data", code="recovery_required", status=409)
        if candidate == transaction:
            break
    return path


def _entry(path, native_modes):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ScionError("Workspace recovery expected a regular file", code="recovery_required", status=409)
    return {"blob": blob_id(path.read_bytes()),
            "mode": "100755" if info.st_mode & 0o111 else "100644"} if native_modes else {"blob": blob_id(path.read_bytes())}


def _matches(path, entry, native_modes):
    actual = _entry(path, native_modes)
    return actual["blob"] == entry["blob"] and (not native_modes or actual["mode"] == entry["mode"])


def _write_file(path, data, permissions, times=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.chmod(path, permissions)
        if times:
            os.utime(path, ns=times)
        os.fsync(stream.fileno())


def _sync_tree(directory):
    for root, dirs, _ in os.walk(directory, topdown=False, followlinks=False):
        if any((Path(root) / name).is_symlink() for name in dirs):
            raise ScionError("Symlink in workspace transaction")
        sync_directory(Path(root))


def _mkdir_parents(workspace, path):
    pending = []
    parent = path.parent
    while parent != workspace.root and not parent.exists():
        if parent.is_symlink():
            raise ScionError("Symlink in workspace directory")
        pending.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise ScionError("File blocks workspace directory", code="recovery_required", status=409)
    for parent in reversed(pending):
        workspace._safe_path(parent.relative_to(workspace.root).as_posix())
        parent.mkdir()
        sync_directory(parent.parent)


def _remove_empty_tree(path):
    if path.is_symlink():
        raise ScionError("Symlink blocks workspace update", code="recovery_required", status=409)
    if not path.exists():
        return
    if not path.is_dir():
        raise ScionError("File blocks workspace update", code="recovery_required", status=409)
    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        if files or any((Path(root) / name).is_symlink() for name in dirs):
            raise ScionError("Local files block workspace recovery", code="recovery_required", status=409)
        Path(root).rmdir()
        sync_directory(Path(root).parent)


def _roots(paths):
    result = []
    for path in sorted(paths, key=lambda p: (p.count("/"), p)):
        if not any(path.startswith(parent + "/") for parent in result):
            result.append(path)
    return result


def _current_files(workspace, paths):
    files = {}
    for relative in _roots(paths):
        path = workspace._safe_path(relative)
        if path.is_dir():
            for root, names, filenames in os.walk(path, followlinks=False):
                for name in names + filenames:
                    child = Path(root) / name
                    name_relative = child.relative_to(workspace.root).as_posix()
                    workspace._safe_path(name_relative)
                    if not child.is_dir():
                        files[name_relative] = child
        elif path.exists():
            files[relative] = path
    return files
