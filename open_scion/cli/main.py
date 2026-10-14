"""CLI entry point and process exit semantics."""

from __future__ import annotations

from ..store import ScionError
from .commands import run
from .output import _print_error, _print_result
from .parser import _parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        _print_result(args.command, result, args.json)
        if args.command == "fsck" and not result.get("ok"):
            return 1
        return 0
    except ScionError as exc:
        _print_error(str(exc), exc.code, exc.details, args.json)
        return 1
    except (OSError, ValueError) as exc:
        _print_error(str(exc), "local_error", None, args.json)
        return 1
    except KeyboardInterrupt:
        return 130
