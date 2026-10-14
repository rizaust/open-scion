"""Workspace initialization, recovery, edits, and integration commands."""

from __future__ import annotations

from ...store import ScionError
from ...workspace import Workspace

COMMANDS = frozenset({"init", "recover", "status", "commit", "update", "land", "merge", "rebase"})


def run(args):
    if args.command == "init":
        workspace = Workspace.init(args.directory, args.server, revision=args.rev, sparse=args.sparse)
        return {"root": str(workspace.root), "server": workspace.client.server,
                "revision": workspace.revision, "sparse": workspace.sparse}
    if args.command == "recover":
        return Workspace.find(auto_recover=False).recover()
    workspace = Workspace.find()
    if args.command == "status":
        return workspace.status()
    if args.command == "commit":
        return workspace.commit(args.message, args.author)
    if args.command == "update":
        return workspace.update(args.revision)
    if args.command == "land":
        return workspace.land(args.revision, ref=args.ref,
                              expected=None if args.expected == "none" else args.expected,
                              author=args.author, explicit_expected=args.expected is not None)
    if args.command == "merge":
        return workspace.integrate("merge", {"base": args.base, "ours": args.ours,
                                              "theirs": args.theirs, "message": args.message, "author": args.author})
    if args.command == "rebase":
        return workspace.integrate("rebase", {"revision": args.revision, "onto": args.onto,
                                               "message": args.message, "author": args.author})
    raise ScionError("Unknown workspace command")
