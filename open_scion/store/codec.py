"""Canonical serialization and typed content-addressed object identities."""
from __future__ import annotations

import hashlib
import json

from .errors import ScionError


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
