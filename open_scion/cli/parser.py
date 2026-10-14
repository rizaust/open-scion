"""Argument definitions for native workspace and repository commands."""

from __future__ import annotations

import argparse
import getpass

from .. import __version__


def _parser():
    parser = argparse.ArgumentParser(prog="scion", description="Server-native version control with thin, sparse workspaces.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
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
    serve.add_argument("--max-workers", type=int, default=32, help="Maximum simultaneous HTTP requests")
    serve.add_argument("--request-timeout", type=float, default=15, help="Socket timeout in seconds")
    serve.add_argument("--max-inflight-bytes", type=int, default=64 * 1024 * 1024,
                       help="Aggregate admitted HTTP request body bytes")
    serve.add_argument("--access-log", action="store_true", help="Write structured JSON access logs to stderr")
    serve.add_argument("--auth-file", help="Hashed credential configuration with scoped identities")
    serve.add_argument("--ref-policy", help="Protected-ref policy configuration")
    serve.add_argument("--idempotency-retention", type=int, default=604800, help="Seconds to retain successful mutation results")
    serve.add_argument("--idempotency-max-records", type=int, default=100000, help="Maximum retained mutation results")
    serve.add_argument("--idempotency-max-result-bytes", type=int, default=1048576, help="Maximum stored JSON bytes per result")

    auth = command("auth", "Generate credentials or inspect hashed token configuration")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    token = auth_commands.add_parser("token", help="Generate a bearer token and its hashed configuration record")
    token.add_argument("--id", required=True, help="Stable authenticated identity")
    token.add_argument("--capability", action="append", default=[], choices=("read", "object-write", "changeset-write", "ref-update", "admin"))
    token.add_argument("--ref", action="append", default=[], help="Authorized ref glob; repeat for multiple scopes")
    token.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    inspect = auth_commands.add_parser("inspect", help="Describe configured identities without showing token hashes")
    inspect.add_argument("file")
    inspect.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    identity = command("identity", "Inspect the current authenticated identity")
    identity.add_argument("--server", help="Server URL; defaults to the current workspace")
    policy = command("policy", "Inspect the effective rule for a shared ref")
    policy.add_argument("name", nargs="?", default="main")
    policy.add_argument("--server", help="Server URL; defaults to the current workspace")

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
    ref_log = command("ref-log", "Read the transactional ref update audit trail")
    ref_log.add_argument("--name")
    ref_log.add_argument("--limit", type=int, default=50)
    command("recover", "Recover an interrupted working-copy update")
    show = command("show", "Show an immutable changeset")
    show.add_argument("revision", nargs="?")
    check = command("fsck", "Verify repository objects and metadata")
    check.add_argument("--data", required=True)

    backup = command("backup", "Create a verified repository snapshot while the server runs")
    backup.add_argument("--data", required=True)
    backup.add_argument("--output", required=True, help="New snapshot directory")
    restore = command("restore", "Verify and restore a snapshot into a new repository directory")
    restore.add_argument("snapshot")
    restore.add_argument("--data", required=True, help="New destination directory")

    git = command("import-git", "Import reachable Git history into Scion")
    git.add_argument("path")
    git.add_argument("--server", required=True)
    git.add_argument("--ref", default="main")
    git.add_argument("--revision", default="HEAD")
    return parser
