"""Credential generation and authenticated identity/policy inspection."""

from __future__ import annotations

import hashlib
from pathlib import Path
import secrets
from urllib.parse import urlencode

from ...client import Client
from ...security.config import load_auth_config, validate_token_record
from ...store import ScionError
from ...workspace import Workspace

COMMANDS = frozenset({"auth", "identity", "policy"})


def run(args):
    if args.command == "auth":
        if args.auth_command == "token":
            token = secrets.token_urlsafe(32)
            record = {"id": args.id, "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                      "capabilities": args.capability, "refs": args.ref, "revoked": False}
            validate_token_record(record)
            return {"token": token, "record": record}
        if args.auth_command == "inspect":
            _, records = load_auth_config(Path(args.file).expanduser())
            return {"identities": [{"id": record.id, "capabilities": sorted(record.capabilities),
                                    "refs": list(record.refs), "revoked": record.revoked} for record in records]}
    else:
        client = Client(args.server) if args.server else Workspace.find().client
        if args.command == "identity":
            return client.request("GET", "/v1/identity")
        if args.command == "policy":
            return client.request("GET", "/v1/ref-policy?" + urlencode({"name": args.name}))
    raise ScionError("Unknown security command")
