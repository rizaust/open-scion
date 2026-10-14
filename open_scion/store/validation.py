"""Portable repository paths, refs, modes, and object identities."""
from __future__ import annotations

import re

from .errors import ScionError

_OID = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_MODES = {"100644", "100755"}


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


def _validate_oid(oid: str) -> str:
    if not isinstance(oid, str) or not _OID.fullmatch(oid):
        raise ScionError("Object ID must be 64 lowercase hexadecimal characters")
    return oid
