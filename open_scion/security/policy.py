"""Immutable protected-ref rules evaluated inside ref-update transactions."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from ..graph import CommitGraph
from ..store import ScionError, validate_ref
from .config import ref_matches, validate_ref_pattern

_MAX_CONFIG_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
_OPERATIONS = frozenset({"ref-update", "land", "land-merge"})


def _invalid(message):
    return ScionError(message, code="invalid_policy", status=400)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid(f"Duplicate policy configuration key: {key}")
        result[key] = value
    return result


def _identifier(value, field):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _invalid(f"{field} must be an identifier of 1 to 128 ASCII characters")
    return value


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    pattern: str
    writers: frozenset[str]
    fast_forward_only: bool
    allow_direct: bool
    allow_merge: bool

    def describe(self):
        return {"id": self.id, "pattern": self.pattern, "writers": sorted(self.writers),
                "fast_forward_only": self.fast_forward_only, "allow_direct": self.allow_direct,
                "allow_merge": self.allow_merge}


@dataclass(frozen=True, slots=True)
class RefPolicy:
    """An ordered policy snapshot whose decisions never depend on author text."""

    rules: tuple[_Rule, ...]

    def __post_init__(self):
        if not isinstance(self.rules, tuple) or not all(isinstance(rule, _Rule) for rule in self.rules):
            raise _invalid("RefPolicy requires immutable validated rules; use from_file or from_dict")

    @classmethod
    def from_file(cls, path):
        try:
            with Path(path).open("rb") as stream:
                payload = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(payload) > _MAX_CONFIG_BYTES:
                raise _invalid("Policy configuration must fit within 1 MiB")
            data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, ValueError) as exc:
            raise _invalid(f"Cannot load ref policy: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {"version", "rules"}:
            raise _invalid("Policy configuration requires exactly version and rules")
        if type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["rules"], list):
            raise _invalid("Policy version must be 1 and rules must be an array")
        rules, identifiers, patterns = [], set(), set()
        allowed = {"id", "pattern", "writers", "fast_forward_only", "allow_direct", "allow_merge"}
        for index, value in enumerate(data["rules"]):
            if not isinstance(value, dict) or set(value) - allowed or not {"id", "pattern"} <= set(value):
                raise _invalid(f"Rule {index} requires id and pattern and accepts only documented fields")
            identifier = _identifier(value["id"], "Rule id")
            try:
                pattern = validate_ref_pattern(value["pattern"])
            except ScionError as exc:
                raise _invalid(f"Invalid pattern for rule {identifier}: {exc}") from exc
            if identifier in identifiers or pattern in patterns:
                raise _invalid("Rule IDs and patterns must be unique")
            writers = value.get("writers", [])
            if not isinstance(writers, list):
                raise _invalid(f"Writers for rule {identifier} must be an array of principal IDs")
            writers = [_identifier(writer, "Writer principal ID") for writer in writers]
            if len(set(writers)) != len(writers):
                raise _invalid(f"Writers for rule {identifier} must be unique")
            flags = {name: value.get(name, default) for name, default in
                     (("fast_forward_only", True), ("allow_direct", False), ("allow_merge", True))}
            if any(type(flag) is not bool for flag in flags.values()):
                raise _invalid(f"Policy flags for rule {identifier} must be JSON booleans")
            rules.append(_Rule(identifier, pattern, frozenset(writers), **flags))
            identifiers.add(identifier)
            patterns.add(pattern)
        return cls(tuple(rules))

    def _match(self, name):
        validate_ref(name)
        return next((rule for rule in self.rules if ref_matches(rule.pattern, name)), None)

    def describe(self, ref):
        """Return a detached description of the first effective rule."""
        rule = self._match(ref)
        return {"ref": ref, "matched": rule is not None, "rule": rule.describe() if rule else None}

    @staticmethod
    def _deny(rule, name, reason):
        raise ScionError(f"Ref policy {rule.id!r} denied update to {name!r}: {reason}",
                         code="policy_denied", status=403,
                         details={"rule": rule.id, "ref": name, "reason": reason})

    @staticmethod
    def _introduced_merge(store, graph, current, target):
        """Find a merge added to reachability, including behind linear descendants."""
        if current is not None and graph.is_ancestor(target, current):
            return None
        cursor, visited = target, set()
        while cursor is not None and cursor != current:
            if cursor in visited:
                raise ScionError("Cycle in commit graph", code="corrupt", status=500)
            visited.add(cursor)
            record = store.get_changeset(cursor)
            parents = record["parents"]
            if len(parents) > 1:
                if current is not None and graph.is_ancestor(cursor, current):
                    return None
                return cursor
            cursor = parents[0] if parents else None
        return None

    def check(self, store, actor, name, current, target, operation):
        """Authorize a resolved ref transition using the store's active transaction."""
        rule = self._match(name)
        if rule is None:
            return {"rule": None, "decision": "allow"}
        if not isinstance(actor, str) or actor not in rule.writers:
            self._deny(rule, name, "writer_not_allowed")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            self._deny(rule, name, "unsupported_operation")
        if operation == "ref-update" and not rule.allow_direct:
            self._deny(rule, name, "direct_update_disabled")
        if operation == "land-merge" and not rule.allow_merge:
            self._deny(rule, name, "merge_disabled")
        target = store.resolve(target)
        current = store.resolve(current) if current is not None else None
        graph = CommitGraph(store)
        if rule.fast_forward_only and current is not None and not graph.is_ancestor(current, target):
            self._deny(rule, name, "non_fast_forward")
        if not rule.allow_merge and self._introduced_merge(store, graph, current, target) is not None:
            self._deny(rule, name, "merge_disabled")
        return {"rule": rule.id, "decision": "allow"}
