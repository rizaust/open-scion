"""Explicit command dispatch across repository, workspace, and query groups."""

from ...store import ScionError
from . import queries, service, workspace


def run(args):
    if args.command in service.COMMANDS:
        return service.run(args)
    if args.command in workspace.COMMANDS:
        return workspace.run(args)
    if args.command in queries.COMMANDS:
        return queries.run(args)
    raise ScionError("Unknown command")
