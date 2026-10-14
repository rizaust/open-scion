"""Canonical mutation arguments and explicit endpoint schemas."""

from __future__ import annotations

from ..store import ScionError, validate_ref


def mutation_schema(handler, path: str, body: dict) -> tuple[int, str, str | None]:
    """Validate arguments and normalize defaults before request fingerprinting."""
    if path == "/v1/changesets":
        handler._fields(body, ("parents", "changes", "message", "author"))
        handler._text(body, "message", empty=True)
        handler._text(body, "author")
        parents = body["parents"]
        if not isinstance(parents, list) or len(parents) > 2 or any(
                not isinstance(parent, str) or not parent for parent in parents):
            raise ScionError("parents must contain at most two revision strings")
        if not isinstance(body["changes"], dict):
            raise ScionError("changes must be a path-to-entry object")
        for name, entry in body["changes"].items():
            handler._text({"path": name}, "path")
            if entry is None:
                continue
            if not isinstance(entry, dict):
                raise ScionError("Each change must be an entry object or null")
            handler._fields(entry, ("blob", "mode"))
            handler._text(entry, "blob")
            if entry["mode"] not in ("100644", "100755"):
                raise ScionError("File mode must be 100644 or 100755")
        return 201, "changeset-write", None
    if path.startswith("/v1/refs/") and path[9:]:
        name = validate_ref(path[9:])
        handler._fields(body, ("new", "expected"))
        handler._text(body, "new")
        handler._text(body, "expected", nullable=True)
        return 200, "ref-update", name
    if path == "/v1/merge":
        handler._fields(body, ("base", "ours", "theirs", "message", "author"))
        handler._text(body, "base", nullable=True)
        for name in ("ours", "theirs", "author"):
            handler._text(body, name)
        handler._text(body, "message", empty=True)
        return 201, "changeset-write", None
    if path == "/v1/rebase":
        handler._fields(body, ("revision", "onto"), ("message", "author"))
        for name in ("revision", "onto"):
            handler._text(body, name)
        handler._text(body, "message", nullable=True, empty=True)
        handler._text(body, "author", nullable=True)
        body.setdefault("message", None)
        body.setdefault("author", None)
        return 201, "changeset-write", None
    if path == "/v1/land":
        handler._fields(body, ("revision", "ref", "expected"), ("author",))
        for name in ("revision", "ref", "author"):
            handler._text(body, name)
        handler._text(body, "expected", nullable=True)
        validate_ref(body["ref"])
        body.setdefault("author", "scion")
        return 200, "ref-update", body["ref"]
    raise ScionError("Endpoint not found", "not_found", 404)
