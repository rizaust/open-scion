"""Durable actor-scoped idempotency with bounded retention and result storage."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from .actors import validate_scope
from .errors import ScionError
from .validation import _validate_oid

DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_RECORDS = 100000
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
CLEANUP_LIMIT = 256


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not 1 <= len(key) <= 200 or any(not "!" <= char <= "~" for char in key):
        raise ScionError("Idempotency key must contain 1 to 200 visible ASCII characters")
    return key


def canonical_result(result) -> str:
    try:
        return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ScionError("Idempotent result must be finite JSON data", "idempotency_result", 500) from exc


class IdempotencyStoreMixin:
    def _configure_idempotency(self, retention_seconds: int, max_records: int, max_result_bytes: int) -> None:
        for label, value in (("retention_seconds", retention_seconds), ("max_records", max_records),
                             ("max_result_bytes", max_result_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ScionError(f"Idempotency {label} must be a positive integer")
        self.idempotency_retention_seconds = retention_seconds
        self.idempotency_max_records = max_records
        self.idempotency_max_result_bytes = max_result_bytes

    @staticmethod
    def _prune_idempotency(db, now: int, limit: int = CLEANUP_LIMIT) -> None:
        db.execute("""DELETE FROM idempotency_results WHERE rowid IN (
            SELECT rowid FROM idempotency_results WHERE expires_at<=?
            ORDER BY expires_at, rowid LIMIT ?)""", (now, limit))

    def execute_idempotent(self, actor: str, operation: str, key: str, fingerprint: str, callback) -> dict:
        """Commit the callback's mutations and replay result in the same transaction."""
        actor = validate_scope(actor, "Actor", 256)
        operation = validate_scope(operation, "Operation", 8192)
        validate_key(key)
        _validate_oid(fingerprint)
        if not callable(callback):
            raise ScionError("Idempotent callback must be callable")
        _, audit_operation = self._actor_context.get()
        with self.actor_scope(actor, audit_operation), self._write_transaction() as db:
            state = self._active_write()
            namespace = (actor, operation, key)
            if namespace in state.idempotency_keys:
                raise ScionError("Idempotency key is already executing in this transaction",
                                 "idempotency_in_progress", 409)
            now = int(time.time())
            row = db.execute("""SELECT fingerprint,result_json,expires_at,result_bytes FROM idempotency_results
                WHERE actor=? AND operation=? AND key=?""", (actor, operation, key)).fetchone()
            if row is not None and row[2] > now:
                if row[0] != fingerprint:
                    raise ScionError("Idempotency key was already used with a different request",
                                     "idempotency_conflict", 409)
                try:
                    result = json.loads(row[1])
                    if canonical_result(result) != row[1] or len(row[1].encode("utf-8")) != row[3]:
                        raise ValueError("Noncanonical or truncated replay result")
                except (TypeError, ValueError, ScionError, RecursionError) as exc:
                    raise ScionError("Stored idempotency result is corrupt", "corrupt", 500) from exc
                return {"result": result, "replayed": True}
            if row is not None:
                db.execute("DELETE FROM idempotency_results WHERE actor=? AND operation=? AND key=?",
                           (actor, operation, key))
            self._prune_idempotency(db, now, CLEANUP_LIMIT - (1 if row is not None else 0))
            count = db.execute("SELECT COUNT(*) FROM idempotency_results").fetchone()[0]
            if count >= self.idempotency_max_records:
                raise ScionError("Idempotency storage is full; retry after retained keys expire",
                                 "idempotency_capacity", 503, {"max_records": self.idempotency_max_records})
            state.idempotency_keys.add(namespace)
            try:
                result_json = canonical_result(callback())
            finally:
                state.idempotency_keys.remove(namespace)
            if db.execute("SELECT COUNT(*) FROM idempotency_results").fetchone()[0] >= self.idempotency_max_records:
                raise ScionError("Nested mutations exhausted idempotency storage", "idempotency_capacity", 503,
                                 {"max_records": self.idempotency_max_records})
            result_bytes = len(result_json.encode("utf-8"))
            if result_bytes > self.idempotency_max_result_bytes:
                raise ScionError("Idempotent response exceeds the stored result limit",
                                 "idempotency_result_too_large", 413,
                                 {"max_result_bytes": self.idempotency_max_result_bytes})
            completed_at = datetime.now(timezone.utc)
            created_at = completed_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
            db.execute("""INSERT INTO idempotency_results
                (actor,operation,key,fingerprint,result_json,result_bytes,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?)""", (actor, operation, key, fingerprint, result_json, result_bytes,
                                           created_at, int(completed_at.timestamp()) + self.idempotency_retention_seconds))
            return {"result": json.loads(result_json), "replayed": False}
