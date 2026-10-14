"""Offline, repeatable recovery of interrupted working-copy updates."""

from __future__ import annotations

import os
import stat
import uuid

from ..store import ScionError
from .filesystem import (_current_files, _matches, _mkdir_parents, _remove_empty_tree,
                         _roots, _transaction_file, _write_file, sync_directory)
from .journal import _failure, _finish, _read_journal


def _restore_backup(workspace, transaction, relative):
    backup = _transaction_file(transaction, "backup", relative)
    path = workspace._safe_path(relative)
    _mkdir_parents(workspace, path)
    info = backup.stat()
    temporary = transaction / ("restore-" + uuid.uuid4().hex)
    try:
        _write_file(temporary, backup.read_bytes(), stat.S_IMODE(info.st_mode), (info.st_atime_ns, info.st_mtime_ns))
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _owned_current_file(path, relative, before, after, transaction, native_modes):
    if relative in before and _matches(path, before[relative], native_modes):
        return True
    if relative in after and _matches(path, after[relative], native_modes):
        staged = _transaction_file(transaction, "target", relative)
        return staged.is_file() and os.path.samefile(path, staged)
    return False


def recover_transaction(workspace):
    """Recover under the workspace lock without contacting the server."""
    journal = _read_journal(workspace)
    if journal is None:
        return {"recovered": False, "action": "none", "revision": workspace.revision}
    transaction = workspace.control / journal["transaction"]
    if workspace.config == journal["target"]:
        retained = _finish(workspace, transaction)
        return {"recovered": True, "action": "completed", "revision": workspace.revision,
                "retained_backup": retained}
    if workspace.config != journal["previous"]:
        raise _failure(workspace, "Workspace metadata changed during an interrupted update", transaction)
    if journal["phase"] == "prepared":
        retained = _finish(workspace, transaction)
        return {"recovered": True, "action": "rolled_back", "revision": workspace.revision,
                "retained_backup": retained}
    before, after = journal["before"], journal["after"]
    paths = before.keys() | after.keys()
    native_modes = workspace._native_modes()
    try:
        for relative, entry in before.items():
            backup = _transaction_file(transaction, "backup", relative)
            if not _matches(backup, entry, native_modes):
                raise _failure(workspace, "Saved original file failed recovery verification", transaction, [relative])
        current = _current_files(workspace, paths)
        conflicts = []
        for relative, path in current.items():
            if not _owned_current_file(path, relative, before, after, transaction, native_modes):
                conflicts.append(relative)
        if conflicts:
            raise _failure(workspace, "Local files changed during an interrupted update; preserve or move them before recovery",
                           transaction, conflicts)
        for relative in sorted(current, key=lambda p: (-p.count("/"), p)):
            path = workspace._safe_path(relative)
            if not _owned_current_file(path, relative, before, after, transaction, native_modes):
                raise _failure(workspace, "Local file changed while recovery was running", transaction, [relative])
            path.unlink()
            sync_directory(path.parent)
            workspace._checkpoint("recovery_file_removed")
        for relative in _roots(paths):
            path = workspace._safe_path(relative)
            if path.is_dir():
                _remove_empty_tree(path)
        for relative in sorted(journal["directories_before"], key=lambda p: (p.count("/"), p)):
            path = workspace._safe_path(relative)
            _mkdir_parents(workspace, path)
            path.mkdir(exist_ok=True)
            path.chmod(journal["directories_before"][relative])
            sync_directory(path)
            sync_directory(path.parent)
        for relative in sorted(before):
            _restore_backup(workspace, transaction, relative)
            workspace._checkpoint("recovery_file_restored")
        for relative in after:
            parent = (workspace.root / relative).parent
            while parent != workspace.root and parent.relative_to(workspace.root).as_posix() not in journal["directories_before"]:
                try:
                    parent.rmdir()
                    sync_directory(parent.parent)
                except OSError:
                    break
                parent = parent.parent
        workspace._checkpoint("recovery_complete")
        retained = _finish(workspace, transaction)
        workspace.config = dict(journal["previous"])
        return {"recovered": True, "action": "rolled_back", "revision": workspace.revision,
                "retained_backup": retained}
    except Exception as exc:
        if isinstance(exc, ScionError) and exc.code == "recovery_required" and exc.details and "backup" in exc.details:
            raise
        raise _failure(workspace, f"Workspace recovery needs attention: {exc}", transaction) from exc
