"""Authorized mutation dispatch and durable request replay."""

from __future__ import annotations

import hashlib
import re

from ..store import ScionError, _json
from .write_schema import mutation_schema


_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class WriteRoutesMixin:
    """Commit a mutation and its idempotency result before sending a response."""

    def _idempotency_key(self) -> str | None:
        keys = self.headers.get_all("Idempotency-Key", [])
        if not keys:
            return None
        if len(keys) != 1 or not _KEY.fullmatch(keys[0]):
            raise ScionError("Idempotency-Key must be 1..128 ASCII letters, digits, dots, colons, underscores, or hyphens")
        return keys[0]

    def _post(self, path: str, query: dict[str, str]) -> None:
        self._fields(query)
        store = self.server.store
        key = self._idempotency_key()
        if path == "/v1/blobs":
            self._require_capability("object-write")
            if key is not None:
                raise ScionError("Blob uploads use their content identity; omit Idempotency-Key")
            self._send(201, {"id": store.put_blob(self._body("application/octet-stream"))})
            return
        if path not in ("/v1/changesets", "/v1/merge", "/v1/rebase", "/v1/land") and not (
                path.startswith("/v1/refs/") and path[9:]):
            raise ScionError("Endpoint not found", "not_found", 404)
        body = self._json_body()
        status, capability, ref = mutation_schema(self, path, body)
        self._require_capability(capability, ref=ref)
        if path in ("/v1/merge", "/v1/rebase", "/v1/land"):
            self._require_capability("read")
            self._require_capability("changeset-write")

        def execute():
            ops = self.server.operations
            if path == "/v1/changesets":
                result = store.create_changeset(**body)
            elif path.startswith("/v1/refs/"):
                result = store.update_ref(ref, **body)
            elif path == "/v1/merge":
                result = ops.merge(**body)
            elif path == "/v1/rebase":
                result = ops.rebase(**body)
            else:
                result = ops.land(**body)
            return {"status": status, "body": result}

        operation = "ref-update" if path.startswith("/v1/refs/") else path.removeprefix("/v1/")
        headers = None
        with store.actor_scope(self.principal.id, operation=operation):
            if key is not None:
                fingerprint = hashlib.sha256(_json({"path": path, "body": body})).hexdigest()
                outcome = store.execute_idempotent(self.principal.id, path, key, fingerprint, execute)
                result = outcome["result"]
                headers = {"Idempotency-Replayed": "true" if outcome["replayed"] else "false"}
            else:
                with store.mutation():
                    result = execute()
        self._send(result["status"], result["body"], headers=headers)
