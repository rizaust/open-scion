"""Source inspection and shared-ref query commands."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from ...store import ScionError
from ...workspace import Workspace

COMMANDS = frozenset({"refs", "ref-log", "show", "log", "diff", "blame", "search"})


def _revision(workspace, given):
    revision = given or workspace.revision
    if not revision:
        raise ScionError("Workspace has no revision; commit first")
    return revision


def run(args):
    workspace = Workspace.find()
    if args.command == "refs":
        return workspace.client.request("GET", "/v1/refs")
    if args.command == "ref-log":
        params = {"limit": args.limit}
        if args.name:
            params["name"] = args.name
        return workspace.client.request("GET", "/v1/ref-log?" + urlencode(params))
    if args.command == "show":
        return workspace.client.request("GET", "/v1/changesets/" + quote(_revision(workspace, args.revision), safe=""))
    if args.command == "log":
        params = {"rev": _revision(workspace, args.revision), "limit": args.limit}
        if args.path:
            params["path"] = args.path
        return workspace.client.request("GET", "/v1/log?" + urlencode(params))
    if args.command == "diff":
        if not args.base:
            changes = workspace.diff()
            return [c for c in changes if not args.prefix or c["path"] == args.prefix or c["path"].startswith(args.prefix + "/")]
        params = {"base": args.base, "target": _revision(workspace, args.target), "prefix": args.prefix}
        return workspace.client.request("GET", "/v1/diff?" + urlencode(params))
    if args.command == "blame":
        params = {"rev": _revision(workspace, args.rev), "path": args.path}
        return workspace.client.request("GET", "/v1/blame?" + urlencode(params))
    if args.command == "search":
        params = {"rev": _revision(workspace, args.rev), "q": args.query, "prefix": args.prefix, "limit": args.limit}
        return workspace.client.request("GET", "/v1/search?" + urlencode(params))
    raise ScionError("Unknown command")
