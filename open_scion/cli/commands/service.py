"""Repository service, local maintenance, and Git ingestion commands."""

from __future__ import annotations

import os

from ...client import Client
from ...store import ScionError, Store

COMMANDS = frozenset({"serve", "fsck", "backup", "restore", "import-git"})


def run(args):
    if args.command == "serve":
        import logging
        from ...server import serve
        if args.access_log:
            logger = logging.getLogger("open_scion.access")
            logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.propagate = False
        serve(args.data, host=args.host, port=args.port, token=os.environ.get("SCION_TOKEN"),
              max_workers=args.max_workers, request_timeout=args.request_timeout,
              max_inflight_bytes=args.max_inflight_bytes)
        return None
    if args.command == "fsck":
        return Store(args.data, read_only=True).fsck()
    if args.command == "backup":
        if not os.path.isfile(os.path.join(args.data, "scion.db")):
            raise ScionError("Source repository does not exist")
        return Store(args.data).backup(args.output)
    if args.command == "restore":
        from ...store.backup import restore_backup
        return restore_backup(args.snapshot, args.data)
    if args.command == "import-git":
        from ...importers import import_git
        return import_git(args.path, Client(args.server), ref=args.ref, revision=args.revision)
    raise ScionError("Unknown service command")
