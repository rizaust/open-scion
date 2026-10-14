"""Durable snapshot file copying and atomic no-replace publication."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys

from .errors import ScionError


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
