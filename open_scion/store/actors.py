"""Context-local authenticated actors and protected-ref policy delegation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json

from .errors import ScionError


def validate_scope(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ScionError(f"{label} must be nonempty text without control characters")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ScionError(f"{label} exceeds {maximum} bytes")
    except UnicodeError as exc:
        raise ScionError(f"{label} must contain valid Unicode") from exc
    return value


def policy_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ScionError("Ref policy returned invalid audit information", "policy_error", 500) from exc


class ActorStoreMixin:
    def _initialize_actors(self) -> None:
        self._actor_context = ContextVar(f"scion-actor-{id(self)}", default=("local", "ref-update"))
        self.ref_policy = None

    @contextmanager
    def actor_scope(self, actor: str, operation: str = "ref-update"):
        actor = validate_scope(actor, "Actor", 256)
        operation = validate_scope(operation, "Operation", 8192)
        token = self._actor_context.set((actor, operation))
        try:
            yield
        finally:
            self._actor_context.reset(token)

    @contextmanager
    def operation_scope(self, operation: str):
        actor, _ = self._actor_context.get()
        with self.actor_scope(actor, operation):
            yield

    def _authorize_ref(self, name: str, current: str | None, target: str):
        if self.ref_policy is None:
            return None
        actor, operation = self._actor_context.get()
        return self.ref_policy.check(self, actor, name, current, target, operation)
