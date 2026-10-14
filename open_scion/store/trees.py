"""Persistent Merkle trees with batched path-copy updates."""
from __future__ import annotations

import json

from .codec import _json
from .errors import ScionError
from .validation import _MODES, _validate_oid, validate_path, validate_prefix


class TreeStoreMixin:
    def _write_tree(self, entries: dict) -> str:
        return self._write_object("tree", _json(entries))

    def _read_tree(self, oid: str) -> dict:
        try:
            entries = json.loads(self._read_object(oid, "tree")[1])
            if not isinstance(entries, dict):
                raise ValueError("Tree must be an object")
            for name, entry in entries.items():
                validate_path(name)
                if "/" in name or not isinstance(entry, dict):
                    raise ValueError("Invalid tree entry")
                _validate_oid(entry.get("oid"))
                if entry.get("type") == "tree":
                    if set(entry) != {"type", "oid"}:
                        raise ValueError("Invalid directory entry")
                elif entry.get("type") == "blob":
                    if set(entry) != {"type", "oid", "mode"} or not isinstance(entry.get("mode"), str) or entry["mode"] not in _MODES:
                        raise ValueError("Invalid file entry")
                else:
                    raise ValueError("Invalid tree entry type")
            return entries
        except (ValueError, TypeError, ScionError) as exc:
            if isinstance(exc, ScionError) and exc.code in {"not_found", "corrupt"}:
                raise
            raise ScionError(f"Corrupt tree {oid}: {exc}", "corrupt", 500) from exc

    def _patch_tree(self, oid: str, changes: list[tuple[list[str], dict | None]]) -> str:
        entries = self._read_tree(oid)
        original = dict(entries)
        groups: dict[str, list] = {}
        for parts, value in changes:
            groups.setdefault(parts[0], []).append((parts[1:], value))
        for name, group in sorted(groups.items()):
            direct = [(parts, value) for parts, value in group if not parts]
            nested = [(parts, value) for parts, value in group if parts]
            old = entries.get(name)
            if direct:
                if nested:
                    raise ScionError(f"File/directory collision at {name}")
                value = direct[0][1]
                if old and old["type"] == "tree":
                    raise ScionError(f"Path names a directory: {name}")
                if value is None:
                    entries.pop(name, None)
                else:
                    entries[name] = {"type": "blob", "oid": value["blob"], "mode": value["mode"]}
            else:
                if old and old["type"] != "tree":
                    raise ScionError(f"Path descends through a file: {name}")
                if old is None and all(value is None for _, value in nested):
                    continue
                child = self._patch_tree(old["oid"] if old else self._empty_tree, nested)
                if child == self._empty_tree:
                    entries.pop(name, None)
                else:
                    entries[name] = {"type": "tree", "oid": child}
        return oid if entries == original else self._write_tree(entries)

    def tree(self, revision: str, prefix: str = "") -> dict[str, dict]:
        validate_prefix(prefix)
        root = self.get_changeset(revision)["tree"]
        output = {}
        stack = [(root, "")]
        while stack:
            oid, base = stack.pop()
            for name, entry in self._read_tree(oid).items():
                path = f"{base}/{name}" if base else name
                if entry["type"] == "tree":
                    if not prefix or path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/"):
                        stack.append((entry["oid"], path))
                elif not prefix or path == prefix or path.startswith(prefix + "/"):
                    output[path] = {"blob": entry["oid"], "mode": entry["mode"]}
        return dict(sorted(output.items()))
