"""Command-line interface for the Scion server and native workspaces."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from urllib.parse import quote, urlencode

from .client import Client
from .store import ScionError, Store
from .workspace import Workspace


def _parser():
    parser = argparse.ArgumentParser(prog="scion", description="Server-native version control with thin, sparse workspaces.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, help):
        item = sub.add_parser(name, help=help)
        item.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON")
        return item

    serve = command("serve", "Run the repository server")
    serve.add_argument("--data", required=True, help="Server repository directory")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7433)

    init = command("init", "Create an empty or sparse native workspace")
    init.add_argument("directory")
    init.add_argument("--server", required=True)
    init.add_argument("--rev", default="main")
    init.add_argument("--sparse", nargs="+", action="extend", default=[], metavar="PREFIX")

    command("status", "Show local changes")
    commit = command("commit", "Create an immutable changeset without moving a shared ref")
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("--author", default=getpass.getuser())

    update = command("update", "Update a clean working copy without overwriting untracked files")
    update.add_argument("revision", nargs="?")

    log = command("log", "Read first-parent history from the server")
    log.add_argument("revision", nargs="?")
    log.add_argument("--limit", type=int, default=20)
    log.add_argument("--path")

    diff = command("diff", "Compare local changes, or two server revisions")
    diff.add_argument("base", nargs="?")
    diff.add_argument("target", nargs="?")
    diff.add_argument("--prefix", default="")

    land = command("land", "Atomically advance a shared ref, merging if needed")
    land.add_argument("revision", nargs="?")
    land.add_argument("--ref", default="main")
    land.add_argument("--expected", help="Expected ref ID; use 'none' to require a new ref")
    land.add_argument("--author", default=getpass.getuser())

    merge = command("merge", "Merge two histories on the server and update the clean workspace")
    merge.add_argument("theirs")
    merge.add_argument("--ours")
    merge.add_argument("--base")
    merge.add_argument("-m", "--message", required=True)
    merge.add_argument("--author", default=getpass.getuser())

    rebase = command("rebase", "Replay a changeset delta onto another revision")
    rebase.add_argument("onto")
    rebase.add_argument("--revision")
    rebase.add_argument("-m", "--message")
    rebase.add_argument("--author")

    blame = command("blame", "Show server-computed line provenance")
    blame.add_argument("path")
    blame.add_argument("--rev")

    search = command("search", "Search the repository on the server, including unseen paths")
    search.add_argument("query")
    search.add_argument("--rev")
    search.add_argument("--prefix", default="")
    search.add_argument("--limit", type=int, default=100)

    command("refs", "List shared refs")
    show = command("show", "Show an immutable changeset")
    show.add_argument("revision", nargs="?")
    check = command("fsck", "Verify repository objects and metadata")
    check.add_argument("--data", required=True)

    git = command("import-git", "Import reachable Git history into Scion")
    git.add_argument("path")
    git.add_argument("--server", required=True)
    git.add_argument("--ref", default="main")
    git.add_argument("--revision", default="HEAD")
    return parser


def _revision(workspace, given):
    revision = given or workspace.revision
    if not revision:
        raise ScionError("Workspace has no revision; commit first")
    return revision


def _run(args):
    if args.command == "serve":
        from .server import serve
        serve(args.data, host=args.host, port=args.port, token=os.environ.get("SCION_TOKEN"))
        return None
    if args.command == "fsck":
        return Store(args.data).fsck()
    if args.command == "import-git":
        from .git_import import import_git
        return import_git(args.path, Client(args.server), ref=args.ref, revision=args.revision)
    if args.command == "init":
        workspace = Workspace.init(args.directory, args.server, revision=args.rev, sparse=args.sparse)
        return {"root": str(workspace.root), "server": workspace.client.server,
                "revision": workspace.revision, "sparse": workspace.sparse}
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
        return workspace.integrate("merge", {"base": args.base, "ours": _revision(workspace, args.ours),
                                              "theirs": args.theirs, "message": args.message, "author": args.author})
    if args.command == "rebase":
        return workspace.integrate("rebase", {"revision": _revision(workspace, args.revision), "onto": args.onto,
                                               "message": args.message, "author": args.author})
    if args.command == "refs":
        return workspace.client.request("GET", "/v1/refs")
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


def _print_result(command, result, as_json=False):
    if result is None:
        return
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command == "status":
        print("Workspace " + result["root"])
        print("Revision  " + (result["revision"] or "(empty)"))
        if result["sparse"]:
            print("Sparse    " + ", ".join(result["sparse"]))
        for change in result["changes"]:
            print({"added": "A", "modified": "M", "deleted": "D"}[change["status"]] + "  " + change["path"])
        if not result["changes"]:
            print("Working copy clean")
    elif command == "log":
        for record in result:
            print(f"{record['id'][:12]}  {record['author']}  {record['message'].splitlines()[0] if record['message'] else ''}")
    elif command == "refs":
        for ref, target in sorted(result.items()):
            print(f"{ref}\t{target}")
    elif command == "blame":
        for line in result:
            print(f"{line['changeset'][:12]} {line['author']} {line['line']:>5} | {line['text']}")
    elif command == "search":
        for line in result:
            print(f"{line['path']}:{line['line']}:{line['text']}")
    elif command == "diff":
        for change in result:
            print(f"{change['status']} {change['path']}")
            if change.get("binary"):
                print("Binary files differ")
            elif change.get("patch"):
                print(change["patch"], end="" if change["patch"].endswith("\n") else "\n")
    elif command in ("commit", "merge", "rebase"):
        print(f"Created {result['id']}\n{result['message']}\nRun scion land to advance a shared ref.")
    elif command == "land":
        print(f"Landed {result['changeset']['id']} on {result['ref']['name']}")
    elif command == "init":
        print(f"Initialized {result['root']}\nServer: {result['server']}\nRevision: {result['revision'] or '(empty)'}")
    elif command == "update":
        print(f"Updated to {result['revision']} ({result['updated']} files written, {result['deleted']} deleted)")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
        _print_result(args.command, result, args.json)
        if args.command == "fsck" and not result.get("ok"):
            return 1
        return 0
    except ScionError as exc:
        if args.json:
            print(json.dumps({"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"scion: {exc}", file=sys.stderr)
            if exc.details:
                print(json.dumps(exc.details, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": {"code": "local_error", "message": str(exc), "details": None}}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"scion: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
