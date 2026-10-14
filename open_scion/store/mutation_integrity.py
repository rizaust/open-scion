"""Integrity checks for authenticated audit and durable replay metadata."""
from __future__ import annotations

from datetime import datetime, timedelta
import json

from .actors import policy_json, validate_scope
from .errors import ScionError
from .idempotency import canonical_result, validate_key
from .schema import audit_projection
from .validation import _validate_oid, validate_ref


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Invalid UTC timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC")
    return timestamp


def read_audit(db) -> list:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ref_audit'").fetchone():
        return []
    return list(db.execute("SELECT " + audit_projection(db) + " FROM ref_audit ORDER BY sequence"))


def verify_audit(records: list, changesets: dict, refs: dict, errors: list) -> None:
    targets = {}
    for sequence, name, old, new, created_at, actor, operation, policy in records:
        try:
            validate_ref(name)
            _timestamp(created_at)
            validate_scope(actor, "Actor", 256)
            validate_scope(operation, "Operation", 8192)
            if policy_json(json.loads(policy)) != policy:
                raise ValueError("Noncanonical policy information")
            if new not in changesets or old is not None and old not in changesets:
                raise ValueError("Missing changeset")
            if name in targets and old != targets[name]:
                raise ValueError("Discontinuous ref history")
            targets[name] = new
        except (ScionError, ValueError, TypeError, RecursionError) as exc:
            errors.append(f"Ref audit {sequence}: {exc}")
    for name, target in targets.items():
        if refs.get(name) != target:
            errors.append(f"Ref audit disagrees with current ref {name!r}")


def verify_idempotency(db, errors: list) -> int:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='idempotency_results'").fetchone():
        return 0
    count = 0
    rows = db.execute("""SELECT actor,operation,key,fingerprint,result_json,result_bytes,created_at,expires_at
        FROM idempotency_results""")
    for actor, operation, key, fingerprint, result_json, result_bytes, created_at, expires_at in rows:
        count += 1
        try:
            validate_scope(actor, "Actor", 256)
            validate_scope(operation, "Operation", 8192)
            validate_key(key)
            _validate_oid(fingerprint)
            created = _timestamp(created_at)
            if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= int(created.timestamp()):
                raise ValueError("Invalid expiry timestamp")
            if not isinstance(result_json, str) or not isinstance(result_bytes, int) or isinstance(result_bytes, bool):
                raise ValueError("Invalid result representation")
            if result_bytes != len(result_json.encode("utf-8")):
                raise ValueError("Result byte count mismatch")
            if canonical_result(json.loads(result_json)) != result_json:
                raise ValueError("Noncanonical result JSON")
        except (ScionError, ValueError, TypeError, RecursionError, UnicodeError) as exc:
            errors.append(f"Idempotency record {count}: {exc}")
    return count
