"""Recovery journal validation, error context, and transaction cleanup."""

from __future__ import annotations

import json
import re
import shutil

from ..store import ScionError, validate_path
from .filesystem import sync_directory


_JOURNAL = "transaction.json"
_OID = re.compile(r"[0-9a-f]{64}\Z")
_TRANSACTION = re.compile(r"update-[0-9a-f]{32}\Z")


def _failure(workspace, message, transaction=None, conflicts=None):
    details = {"journal": str(workspace.control / _JOURNAL)}
    if transaction:
        details["backup"] = str(transaction / "backup")
    if conflicts:
        details["conflicts"] = sorted(set(conflicts))
    return ScionError(message, code="recovery_required", status=409, details=details)


def _read_journal(workspace):
    path = workspace.control / _JOURNAL
    if path.is_symlink():
        raise _failure(workspace, "Workspace recovery journal must not be a symlink")
    if not path.exists():
        return None
    transaction = None
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(journal, dict) or journal.get("version") != 1
                or journal.get("phase") not in ("prepared", "applying")):
            raise ValueError("Unsupported journal format")
        if not isinstance(journal.get("transaction"), str) or not _TRANSACTION.fullmatch(journal["transaction"]):
            raise ValueError("Invalid transaction name")
        transaction = workspace.control / journal["transaction"]
        if transaction.is_symlink() or not transaction.is_dir():
            raise ValueError("Missing or unsafe transaction directory")
        for name in ("before", "after"):
            entries = journal[name]
            if not isinstance(entries, dict):
                raise ValueError("Invalid file action map")
            for relative, entry in entries.items():
                validate_path(relative)
                if (not isinstance(entry, dict) or set(entry) != {"blob", "mode"}
                        or not isinstance(entry["blob"], str) or not _OID.fullmatch(entry["blob"])
                        or entry["mode"] not in ("100644", "100755")):
                    raise ValueError("Invalid file entry")
        directories = journal["directories_before"]
        if not isinstance(directories, dict):
            raise ValueError("Invalid directory map")
        for relative, permissions in directories.items():
            validate_path(relative)
            if not isinstance(permissions, int) or isinstance(permissions, bool) or not 0 <= permissions <= 0o7777:
                raise ValueError("Invalid directory permissions")
        for name in ("previous", "target"):
            if not isinstance(journal[name], dict):
                raise ValueError("Invalid metadata snapshot")
        if {k: v for k, v in journal["previous"].items() if k != "revision"} != {k: v for k, v in journal["target"].items() if k != "revision"}:
            raise ValueError("Transaction changes non-revision metadata")
        for name in ("previous", "target"):
            revision = journal[name].get("revision")
            if revision is not None and (not isinstance(revision, str) or not _OID.fullmatch(revision)):
                raise ValueError("Invalid transaction revision")
        return journal
    except (OSError, ValueError, TypeError, KeyError, ScionError) as exc:
        raise _failure(workspace, f"Cannot read workspace recovery journal: {exc}", transaction) from exc


def _finish(workspace, transaction):
    (workspace.control / _JOURNAL).unlink()
    sync_directory(workspace.control)
    workspace._checkpoint("journal_cleared")
    try:
        shutil.rmtree(transaction)
        sync_directory(workspace.control)
        return None
    except OSError:
        return str(transaction)
