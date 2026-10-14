"""Strict schemas for hashed credentials and deterministic ref scopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import stat

from ..store import ScionError, validate_ref


CAPABILITIES = frozenset({"read", "object-write", "changeset-write", "ref-update", "admin"})
MAX_AUTH_FILE_BYTES = 1024 * 1024
MAX_TOKENS = 4096
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PATTERN = re.compile(r"[A-Za-z0-9*?][A-Za-z0-9._/*?-]{0,254}\Z")


@dataclass(frozen=True, slots=True)
class TokenRecord:
    id: str
    sha256: str = field(repr=False)
    capabilities: frozenset[str]
    refs: tuple[str, ...]
    revoked: bool


def validate_ref_pattern(pattern: str) -> str:
    """Accept normalized, case-sensitive glob scopes using only * and ? wildcards."""
    if not isinstance(pattern, str) or not _PATTERN.fullmatch(pattern) or ".." in pattern:
        raise ScionError("Invalid ref scope pattern")
    if any(part in {"", ".", "..", ".git", ".scion"} or part.startswith(".")
           or part.endswith(".") or part.endswith(".lock") for part in pattern.split("/")):
        raise ScionError("Invalid ref scope pattern")
    return pattern


def ref_matches(pattern: str, name: str) -> bool:
    """Use fnmatchcase semantics: wildcards include slash; no deny-rule precedence."""
    return fnmatchcase(validate_ref(name), validate_ref_pattern(pattern))


def validate_token_record(record: dict) -> TokenRecord:
    """Validate a CLI-generated or configured record without retaining plaintext secrets."""
    if not isinstance(record, dict) or set(record) != {"id", "sha256", "capabilities", "refs", "revoked"}:
        raise ScionError("Token record fields must be id, sha256, capabilities, refs, and revoked")
    identity = record["id"]
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or identity in {"local", "legacy"}:
        raise ScionError("Token identity must be a valid nonreserved identifier")
    digest = record["sha256"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ScionError("Token sha256 must be 64 lowercase hexadecimal characters")
    capabilities = record["capabilities"]
    if (not isinstance(capabilities, list) or len(capabilities) > len(CAPABILITIES)
            or any(not isinstance(value, str) or value not in CAPABILITIES for value in capabilities)
            or len(set(capabilities)) != len(capabilities)):
        raise ScionError("Token capabilities must be distinct supported capability names")
    refs = record["refs"]
    if not isinstance(refs, list) or len(refs) > 64:
        raise ScionError("Token refs must contain at most 64 scope patterns")
    for pattern in refs:
        validate_ref_pattern(pattern)
    if len(set(refs)) != len(refs):
        raise ScionError("Token ref scopes must not be repeated")
    if not isinstance(record["revoked"], bool):
        raise ScionError("Token revoked must be a boolean")
    return TokenRecord(identity, digest, frozenset(capabilities), tuple(refs), record["revoked"])


def _signature(info: os.stat_result) -> tuple:
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUTH_FILE_BYTES:
        raise ScionError("Authentication config must be a regular file of at most 1 MiB")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def file_signature(path: Path) -> tuple:
    return _signature(path.stat())


def _unique_keys(pairs: list[tuple]) -> dict:
    output = {}
    for key, value in pairs:
        if key in output:
            raise ScionError("Authentication config contains duplicate JSON keys")
        output[key] = value
    return output


def load_auth_config(path: Path) -> tuple[tuple, tuple[TokenRecord, ...]]:
    """Read one stable file generation; never substitute a previous valid snapshot."""
    for _ in range(2):
        before = file_signature(path)
        with path.open("rb") as stream:
            opened = _signature(os.fstat(stream.fileno()))
            payload = stream.read(MAX_AUTH_FILE_BYTES + 1)
            after = _signature(os.fstat(stream.fileno()))
        if len(payload) > MAX_AUTH_FILE_BYTES:
            raise ScionError("Authentication config exceeds 1 MiB")
        if before != opened or opened != after or after != file_signature(path):
            continue
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_keys)
        if (not isinstance(document, dict) or set(document) != {"version", "tokens"}
                or type(document["version"]) is not int or document["version"] != 1):
            raise ScionError("Unsupported authentication config schema")
        tokens = document["tokens"]
        if not isinstance(tokens, list) or len(tokens) > MAX_TOKENS:
            raise ScionError("Authentication config must contain at most 4096 token records")
        records = tuple(validate_token_record(record) for record in tokens)
        if len({record.id for record in records}) != len(records):
            raise ScionError("Authentication config contains duplicate token identities")
        if len({record.sha256 for record in records}) != len(records):
            raise ScionError("Authentication config contains duplicate credential hashes")
        return after, records
    raise ScionError("Authentication config changed while it was being read")
