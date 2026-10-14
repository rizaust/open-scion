"""Compressed immutable object files and atomic installation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import zlib

from .codec import _envelope
from .errors import ScionError
from .validation import _OID, _validate_oid


class ObjectStoreMixin:
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
            self._sync_object_directory(path.parent)
            return oid
        path.parent.mkdir(exist_ok=True)
        # Persist the prefix directory entry before any metadata can reference it.
        self._sync_object_directory(self.objects)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(zlib.compress(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                self._read_object(oid, kind)
            self._sync_object_directory(path.parent)
        finally:
            os.unlink(temporary)
        return oid

    @staticmethod
    def _sync_object_directory(directory: Path) -> None:
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

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

    def _object_files(self):
        for directory in sorted(self.objects.iterdir()):
            if len(directory.name) == 2 and re.fullmatch(r"[0-9a-f]{2}", directory.name) and directory.is_dir():
                for path in sorted(directory.iterdir()):
                    if len(path.name) == 62 and _OID.fullmatch(directory.name + path.name):
                        yield path
