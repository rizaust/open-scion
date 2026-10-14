"""Human-readable and structured CLI result and error output."""

from __future__ import annotations

import json
import sys


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


def _print_error(message, code, details, as_json=False):
    if as_json:
        print(json.dumps({"error": {"code": code, "message": message, "details": details}},
                         ensure_ascii=False), file=sys.stderr)
    else:
        print(f"scion: {message}", file=sys.stderr)
        if details:
            print(json.dumps(details, ensure_ascii=False), file=sys.stderr)
