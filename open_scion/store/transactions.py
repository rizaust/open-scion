"""Context-local SQLite connections and reentrant atomic mutation scopes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import sqlite3
import threading
from typing import Iterator

from .errors import ScionError


@dataclass
class _WriteState:
    connection: sqlite3.Connection
    thread: int
    failed: bool = False
    idempotency_keys: set = field(default_factory=set)


class TransactionStoreMixin:
    def _initialize_transactions(self) -> None:
        self._write_context = ContextVar(f"scion-write-{id(self)}", default=None)

    def _active_write(self) -> _WriteState | None:
        state = self._write_context.get()
        if state is not None and state.thread != threading.get_ident():
            raise ScionError("An active transaction cannot cross threads", "transaction_context", 409)
        return state

    def _open_connection(self) -> sqlite3.Connection:
        location = self.db_path.as_uri() + "?mode=ro" if self.read_only else self.db_path
        db = sqlite3.connect(location, uri=self.read_only, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        state = self._active_write()
        if state is not None:
            yield state.connection
            return
        db = self._open_connection()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise ScionError("Repository is read-only", "read_only", 403)
        state = self._active_write()
        if state is not None:
            try:
                yield state.connection
            except BaseException:
                state.failed = True
                raise
            return
        db = self._open_connection()
        token = None
        try:
            db.execute("BEGIN IMMEDIATE")
            state = _WriteState(db, threading.get_ident())
            token = self._write_context.set(state)
            yield db
            if state.failed:
                raise ScionError("A nested mutation failed; the transaction was rolled back",
                                 "transaction_aborted", 409)
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            if token is not None:
                self._write_context.reset(token)
            db.close()

    @contextmanager
    def mutation(self):
        """Atomically publish all metadata mutations within the context."""
        with self._write_transaction():
            yield
