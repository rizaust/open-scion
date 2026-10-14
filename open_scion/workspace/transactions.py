"""Stage, journal, and apply recoverable workspace update transactions."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import uuid

from ..store import ScionError, blob_id
from .filesystem import (_matches, _mkdir_parents, _remove_empty_tree, _sync_tree,
                         _transaction_file, _write_file, durable_json, sync_directory)
from .journal import _JOURNAL, _failure, _finish
from .recovery import recover_transaction


def apply_transaction(workspace, target, before, after, payloads):
    """Stage durable originals before making any user-visible file changes."""
    transaction = workspace.control / ("update-" + uuid.uuid4().hex)
    transaction.mkdir()
    previous = dict(workspace.config)
    target_config = {**previous, "revision": target}
    directories = {}
    published = False
    paths = before.keys() | after.keys()
    try:
        for relative in paths:
            path = workspace._safe_path(relative)
            for parent in path.parents:
                if parent == workspace.root:
                    break
                if parent.is_dir():
                    directories[parent.relative_to(workspace.root).as_posix()] = stat.S_IMODE(parent.stat().st_mode)
            if path.is_dir():
                for root, names, _ in os.walk(path, followlinks=False):
                    directory = Path(root)
                    if directory.is_symlink() or any((directory / name).is_symlink() for name in names):
                        raise ScionError("Symlink blocks workspace update")
                    directories[directory.relative_to(workspace.root).as_posix()] = stat.S_IMODE(directory.stat().st_mode)
        for relative, entry in before.items():
            source = workspace._safe_path(relative)
            info = source.stat()
            data = source.read_bytes()
            if blob_id(data) != entry["blob"] or not _matches(source, entry, workspace._native_modes()):
                raise ScionError("Tracked file changed while preparing update", code="workspace_conflict", status=409)
            backup = _transaction_file(transaction, "backup", relative)
            _write_file(backup, data, stat.S_IMODE(info.st_mode), (info.st_atime_ns, info.st_mtime_ns))
        for relative, entry in after.items():
            path = _transaction_file(transaction, "target", relative)
            _write_file(path, payloads[relative], 0o755 if entry["mode"] == "100755" else 0o644)
        _sync_tree(transaction)
        sync_directory(workspace.control)
        workspace._checkpoint("backups_staged")
        journal = {"version": 1, "phase": "prepared", "transaction": transaction.name, "previous": previous,
                   "target": target_config, "before": before, "after": after,
                   "directories_before": directories}
        published = True
        durable_json(workspace.control / _JOURNAL, journal)
        workspace._checkpoint("journal_published")
        journal["phase"] = "applying"
        durable_json(workspace.control / _JOURNAL, journal)
        workspace._checkpoint("apply_started")
        for relative, entry in sorted(before.items(), key=lambda item: (-item[0].count("/"), item[0])):
            source = workspace._safe_path(relative)
            if not _matches(source, entry, workspace._native_modes()):
                raise ScionError("Tracked file changed while applying update", code="workspace_conflict", status=409)
            source.unlink()
            sync_directory(source.parent)
            workspace._checkpoint("file_removed")
        for relative in sorted(after):
            path = workspace._safe_path(relative)
            if path.is_dir():
                _remove_empty_tree(path)
            _mkdir_parents(workspace, path)
            staged = _transaction_file(transaction, "target", relative)
            os.link(staged, path)
            sync_directory(path.parent)
            workspace._checkpoint("file_written")
        for relative in before:
            parent = (workspace.root / relative).parent
            while parent != workspace.root:
                try:
                    parent.rmdir()
                    sync_directory(parent.parent)
                except OSError:
                    break
                parent = parent.parent
        workspace._checkpoint("files_applied")
        workspace.config = target_config
        workspace._save()
        workspace._checkpoint("metadata_committed")
        _finish(workspace, transaction)
    except Exception as update_error:
        if published:
            try:
                workspace._reload()
                workspace.last_recovery = recover_transaction(workspace)
            except Exception as recovery_error:
                if isinstance(recovery_error, ScionError):
                    raise recovery_error from update_error
                raise _failure(workspace, f"Update failed and recovery needs attention: {recovery_error}", transaction) from recovery_error
        else:
            shutil.rmtree(transaction)
        raise
