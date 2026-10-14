"""Read-only readiness checks for the repository schema."""

from __future__ import annotations

import sqlite3


class ReadinessMixin:
    """Check schema readability without an integrity scan or a write."""

    def readiness(self) -> bool:
        """Probe readable SQLite schema without scanning or repairing stored objects."""
        with self._state:
            if self._draining:
                return False
        if not self.auth_manager.configuration_ready():
            return False
        try:
            uri = self.store.db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=min(self.request_timeout, 1))
            try:
                connection.execute("PRAGMA query_only=ON")
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version != ("1",):
                    return False
                connection.execute("SELECT id,data,tree,generation FROM changesets LIMIT 0")
                connection.execute("SELECT name,target FROM refs LIMIT 0")
                connection.execute("SELECT sequence,name,old_target,new_target,created_at,actor,operation,policy "
                                   "FROM ref_audit LIMIT 0")
                connection.execute("SELECT actor,operation,key,fingerprint,result_json,result_bytes,created_at,expires_at "
                                   "FROM idempotency_results LIMIT 0")
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return False
        return True
