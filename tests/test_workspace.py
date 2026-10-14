"""Working-copy data preservation and cache-integrity regression tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from open_scion.client import Client
from open_scion.server import make_server
from open_scion.store import ScionError, Store, blob_id
from open_scion.workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.store = Store(self.base / "server")
        self.server = make_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = Client(self.url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def record(self, changes, parent=None, ref=None):
        delta = {path: None if content is None else {"blob": self.store.put_blob(content), "mode": "100644"}
                 for path, content in changes.items()}
        result = self.store.create_changeset([parent] if parent else [], delta, "test", "tester")
        if ref:
            self.store.update_ref(ref, result["id"], self.store.list_refs().get(ref))
        return result["id"]

    def workspace(self, sparse=None):
        return Workspace.init(self.base / "work", self.url, sparse=sparse)

    def test_empty_commit_land_and_second_checkout(self):
        workspace = self.workspace()
        self.assertIsNone(workspace.revision)
        (workspace.root / "hello.txt").write_text("hello\n")
        change = workspace.commit("first", "alice")
        self.assertEqual([], change["parents"])
        self.assertEqual({}, self.store.list_refs())
        result = workspace.land()
        self.assertEqual(change["id"], result["ref"]["target"])
        other = Workspace.init(self.base / "other", self.url)
        self.assertEqual("hello\n", (other.root / "hello.txt").read_text())
        self.assertEqual([], other.status()["changes"])

    def test_sparse_commit_preserves_unseen_files(self):
        first = self.record({"src/main.py": b"one\n", "docs/readme.md": b"invisible\n"}, ref="main")
        workspace = self.workspace(sparse=["src"])
        self.assertFalse((workspace.root / "docs").exists())
        (workspace.root / "src/main.py").write_text("two\n")
        (workspace.root / "outside.txt").write_text("unselected\n")
        result = workspace.commit("edit", "alice")
        tree = self.store.tree(result["id"])
        self.assertEqual(self.store.tree(first)["docs/readme.md"], tree["docs/readme.md"])
        self.assertNotIn("outside.txt", tree)
        self.assertEqual({"src/main.py"}, set(result["changes"]))

    def test_update_rejects_modified_and_deleted_tracked_files(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        self.record({"a": b"two"}, first, ref="main")
        (workspace.root / "a").write_bytes(b"local")
        with self.assertRaises(ScionError):
            workspace.update()
        self.assertEqual(b"local", (workspace.root / "a").read_bytes())
        self.assertEqual(first, workspace.revision)
        (workspace.root / "a").unlink()
        with self.assertRaises(ScionError):
            workspace.update()
        self.assertFalse((workspace.root / "a").exists())

    def test_update_protects_untracked_and_ignored_files(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        (workspace.root / ".scionignore").write_text("*.secret\n")
        (workspace.root / "local.secret").write_bytes(b"secret")
        self.record({"local.secret": b"remote"}, first, ref="main")
        with self.assertRaises(ScionError):
            workspace.update()
        self.assertEqual(b"secret", (workspace.root / "local.secret").read_bytes())
        self.assertEqual(first, workspace.revision)

    def test_unrelated_untracked_file_survives_update(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        (workspace.root / "notes").write_text("keep")
        second = self.record({"a": b"two"}, first, ref="main")
        workspace.update()
        self.assertEqual(second, workspace.revision)
        self.assertEqual("keep", (workspace.root / "notes").read_text())
        self.assertEqual(b"two", (workspace.root / "a").read_bytes())

    def test_file_directory_transitions_and_untracked_descendants(self):
        first = self.record({"node": b"file"}, ref="main")
        workspace = self.workspace()
        second = self.record({"node": None, "node/child": b"child"}, first, ref="main")
        workspace.update()
        self.assertEqual(b"child", (workspace.root / "node/child").read_bytes())
        (workspace.root / "node/keep").write_bytes(b"untracked")
        third = self.record({"node/child": None, "node": b"file again"}, second, ref="main")
        with self.assertRaises(ScionError):
            workspace.update()
        self.assertEqual(b"untracked", (workspace.root / "node/keep").read_bytes())
        (workspace.root / "node/keep").unlink()
        workspace.update()
        self.assertEqual(third, workspace.revision)
        self.assertEqual(b"file again", (workspace.root / "node").read_bytes())

    def test_symlinks_are_rejected(self):
        workspace = self.workspace()
        secret = self.base / "secret"
        secret.write_text("private")
        (workspace.root / "link").symlink_to(secret)
        with self.assertRaises(ScionError):
            workspace.status()
        self.assertEqual({}, self.store.list_refs())

    def test_ignore_patterns_and_tracked_files(self):
        self.record({"tracked.log": b"old"}, ref="main")
        workspace = self.workspace()
        (workspace.root / ".scionignore").write_text("# comment\n*.log\nbuild/\n")
        (workspace.root / "ignored.log").write_text("ignored")
        (workspace.root / "tracked.log").write_text("new")
        (workspace.root / "build").mkdir()
        (workspace.root / "build/output").write_text("ignored")
        (workspace.root / ".git").mkdir()
        (workspace.root / ".git/config").write_text("ignored")
        changes = {c["path"] for c in workspace.status()["changes"]}
        self.assertEqual({".scionignore", "tracked.log"}, changes)

    def test_cache_corruption_is_refetched_and_verified(self):
        self.record({"a": b"contents"}, ref="main")
        workspace = self.workspace()
        oid = blob_id(b"contents")
        (workspace.cache / oid).write_bytes(b"corrupt")
        self.assertEqual(b"contents", workspace.blob(oid))
        self.assertEqual(b"contents", (workspace.cache / oid).read_bytes())
        (workspace.cache / oid).unlink()
        with patch.object(workspace.client, "get_blob", return_value=b"corrupt"):
            with self.assertRaises(ScionError):
                workspace.blob(oid)
        self.assertFalse((workspace.cache / oid).exists())

    def test_cache_is_bounded(self):
        workspace = self.workspace()
        first = self.store.put_blob(b"abcd")
        second = self.store.put_blob(b"efgh")
        with patch("open_scion.workspace.cache.CACHE_LIMIT", 6):
            workspace.blob(first)
            workspace.blob(second)
            self.assertLessEqual(sum(p.stat().st_size for p in workspace.cache.iterdir()), 6)
        self.assertEqual(b"abcd", workspace.blob(first))

    def test_failed_blob_download_does_not_touch_workspace(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        self.record({"a": b"two", "b": b"three"}, first, ref="main")
        with patch.object(workspace, "blob", side_effect=ScionError("network failure")):
            with self.assertRaises(ScionError):
                workspace.update()
        self.assertEqual(b"one", (workspace.root / "a").read_bytes())
        self.assertFalse((workspace.root / "b").exists())
        self.assertEqual(first, workspace.revision)

    def test_failed_metadata_write_rolls_back_files(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        self.record({"a": b"two", "b": b"three"}, first, ref="main")
        with patch.object(workspace, "_save", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                workspace.update()
        self.assertEqual(b"one", (workspace.root / "a").read_bytes())
        self.assertFalse((workspace.root / "b").exists())
        self.assertEqual(first, workspace.revision)
        self.assertEqual(first, json.loads((workspace.control / "workspace.json").read_text())["revision"])

    @unittest.skipIf(os.name == "nt", "Windows has no native POSIX executable bits")
    def test_executable_mode_is_committed(self):
        workspace = self.workspace()
        path = workspace.root / "run.sh"
        path.write_text("#!/bin/sh\necho hello\n")
        path.chmod(0o755)
        result = workspace.commit("script", "alice")
        self.assertEqual("100755", self.store.tree(result["id"])["run.sh"]["mode"])

    def test_executable_mode_is_preserved_without_native_mode_bits(self):
        blob = self.store.put_blob(b"script\n")
        record = self.store.create_changeset([], {"run": {"blob": blob, "mode": "100755"}}, "script", "tester")
        self.store.update_ref("main", record["id"], None)
        workspace = self.workspace()
        with patch("open_scion.workspace.scanning.NATIVE_EXECUTABLE_BITS", False):
            self.assertEqual([], workspace.status()["changes"])
            (workspace.root / "run").write_bytes(b"new script\n")
            (workspace.root / "new").write_bytes(b"new file\n")
            result = workspace.commit("edit", "tester")
            self.assertEqual("100755", result["changes"]["run"]["mode"])
            self.assertEqual("100644", result["changes"]["new"]["mode"])

    def test_portable_paths_cannot_escape_or_alias_metadata(self):
        workspace = self.workspace()
        for path in ("C:/outside", "a:stream", "CON", "aux.txt", ".SCION/cache/entry", "nested/.GIT/config", "a.", "a "):
            with self.subTest(path=path), self.assertRaises(ScionError):
                workspace._safe_path(path)

    def test_incomplete_rollback_retains_recovery_files(self):
        first = self.record({"a": b"original"}, ref="main")
        workspace = self.workspace()
        self.record({"a": b"replacement"}, first, ref="main")
        with patch.object(workspace, "_save", side_effect=OSError("save failure")), patch(
                "open_scion.workspace.recovery._restore_backup", side_effect=OSError("restore failure")):
            with self.assertRaises(ScionError) as raised:
                workspace.update()
        self.assertEqual(raised.exception.code, "recovery_required")
        backup = Path(raised.exception.details["backup"])
        self.assertEqual((backup / "a").read_bytes(), b"original")
        self.assertEqual(workspace.revision, first)
        recovered = Workspace(workspace.root, auto_recover=False).recover()
        self.assertEqual("rolled_back", recovered["action"])
        self.assertEqual(b"original", (workspace.root / "a").read_bytes())

    def test_root_parent_alias_is_normalized_but_symlink_target_rejected(self):
        alias = self.base / "alias"
        real = self.base / "real"
        real.mkdir()
        alias.symlink_to(real, target_is_directory=True)
        workspace = Workspace.init(alias / "work", self.url)
        self.assertEqual(real / "work", workspace.root)
        with self.assertRaises(ScionError):
            Workspace.init(alias, self.url)

    def test_init_by_unlanded_revision(self):
        revision = self.record({"a": b"unlanded"})
        workspace = Workspace.init(self.base / "work", self.url, revision=revision)
        self.assertEqual(revision, workspace.revision)
        self.assertEqual(b"unlanded", (workspace.root / "a").read_bytes())

    def test_case_collisions_are_rejected_before_writes(self):
        self.record({"A/one": b"one", "a/two": b"two"}, ref="main")
        with self.assertRaises(ScionError):
            self.workspace()
        self.assertFalse((self.base / "work/A").exists())
        self.assertFalse((self.base / "work/a").exists())

    def test_init_nonempty_and_unsafe_sparse_rejected(self):
        path = self.base / "work"
        path.mkdir()
        (path / "keep").write_text("keep")
        with self.assertRaises(ScionError):
            Workspace.init(path, self.url)
        with self.assertRaises(ScionError):
            Workspace.init(self.base / "elsewhere", self.url, sparse=["../outside"])
        self.assertEqual("keep", (path / "keep").read_text())

    def child(self, source, *args, **kwargs):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        return subprocess.Popen([sys.executable, "-c", source, *map(str, args)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, **kwargs)

    def crash_update(self, root, target, checkpoint, operation="update"):
        source = """
import os
import sys
from open_scion.workspace import Workspace
class CrashWorkspace(Workspace):
    def _checkpoint(self, name):
        if name == sys.argv[3]:
            os._exit(73)
workspace = CrashWorkspace(sys.argv[1], auto_recover=False)
if sys.argv[4] == 'recover':
    workspace.recover()
else:
    workspace.update(sys.argv[2])
"""
        child = self.child(source, root, target, checkpoint, operation)
        out, err = child.communicate(timeout=20)
        self.assertEqual(73, child.returncode, (checkpoint, out, err))

    def assert_files(self, root, expected):
        actual = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
                  if p.is_file() and ".scion" not in p.relative_to(root).parts}
        self.assertEqual(expected, actual)

    def test_process_death_recovers_each_update_stage(self):
        original = {"a": b"one", "gone": b"old", "node/child": b"child", "single": b"file"}
        first = self.record(original, ref="main")
        second = self.record({"a": b"two", "gone": None, "new/sub": b"new", "node/child": None,
                              "node": b"flattened", "single": None, "single/child": b"expanded"}, first)
        updated = {"a": b"two", "new/sub": b"new", "node": b"flattened", "single/child": b"expanded"}
        checkpoints = ("backups_staged", "journal_published", "apply_started", "file_removed", "file_written",
                       "files_applied", "metadata_committed", "journal_cleared")
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                workspace = Workspace.init(self.base / checkpoint, self.url, revision=first)
                (workspace.root / "notes").write_bytes(b"keep")
                (workspace.root / "node/empty").mkdir()
                self.crash_update(workspace.root, second, checkpoint)
                recovered = Workspace(workspace.root, auto_recover=False)
                outcome = recovered.recover()
                committed = checkpoint in ("metadata_committed", "journal_cleared")
                self.assertEqual(second if committed else first, recovered.revision)
                self.assert_files(workspace.root, {**(updated if committed else original), "notes": b"keep"})
                self.assertFalse((workspace.control / "transaction.json").exists())
                if not committed:
                    self.assertTrue((workspace.root / "node/empty").is_dir())
                if checkpoint not in ("backups_staged", "journal_cleared"):
                    self.assertTrue(outcome["recovered"])
                self.assertFalse(recovered.recover()["recovered"])

    def test_recovery_survives_process_death_and_runs_without_server(self):
        first = self.record({"a": b"one", "b": b"two"}, ref="main")
        second = self.record({"a": b"changed", "b": None, "c": b"new"}, first)
        for checkpoint in ("recovery_file_removed", "recovery_file_restored", "recovery_complete"):
            with self.subTest(checkpoint=checkpoint):
                workspace = Workspace.init(self.base / checkpoint, self.url)
                self.crash_update(workspace.root, second, "files_applied")
                self.crash_update(workspace.root, second, checkpoint, operation="recover")
                recovered = Workspace(workspace.root, auto_recover=False)
                with patch.object(recovered.client, "request", side_effect=AssertionError("Recovery must be offline")):
                    result = recovered.recover()
                self.assertEqual("rolled_back", result["action"])
                self.assertEqual(first, recovered.revision)
                self.assert_files(recovered.root, {"a": b"one", "b": b"two"})

    def test_open_automatically_recovers_interrupted_update(self):
        first = self.record({"a": b"one"}, ref="main")
        second = self.record({"a": b"two"}, first)
        workspace = self.workspace()
        self.crash_update(workspace.root, second, "files_applied")
        opened = Workspace(workspace.root)
        self.assertEqual("rolled_back", opened.last_recovery["action"])
        self.assertEqual(first, opened.revision)
        self.assertEqual(b"one", (opened.root / "a").read_bytes())

    def test_recovery_preserves_external_changes_and_original_backups(self):
        first = self.record({"a": b"one"}, ref="main")
        second = self.record({"a": b"two"}, first)
        workspace = self.workspace()
        self.crash_update(workspace.root, second, "files_applied")
        (workspace.root / "a").write_bytes(b"external edit")
        with self.assertRaises(ScionError) as raised:
            Workspace(workspace.root)
        self.assertEqual("recovery_required", raised.exception.code)
        self.assertEqual(b"external edit", (workspace.root / "a").read_bytes())
        self.assertEqual(b"one", (Path(raised.exception.details["backup"]) / "a").read_bytes())
        (workspace.root / "a").rename(workspace.root / "saved-external-edit")
        recovered = Workspace(workspace.root)
        self.assertEqual(first, recovered.revision)
        self.assert_files(recovered.root, {"a": b"one", "saved-external-edit": b"external edit"})

    def test_recovery_does_not_remove_external_file_matching_target(self):
        first = self.record({"a": b"one"}, ref="main")
        second = self.record({"a": b"two", "added": b"target content"}, first)
        workspace = self.workspace()
        self.crash_update(workspace.root, second, "apply_started")
        (workspace.root / "added").write_bytes(b"target content")
        with self.assertRaises(ScionError) as raised:
            Workspace(workspace.root)
        self.assertEqual("recovery_required", raised.exception.code)
        self.assertEqual(b"target content", (workspace.root / "added").read_bytes())
        self.assertEqual(b"one", (workspace.root / "a").read_bytes())
        (workspace.root / "added").rename(workspace.root / "external")
        self.assertEqual(first, Workspace(workspace.root).revision)
        self.assertEqual(b"target content", (workspace.root / "external").read_bytes())

    def test_prepared_recovery_preserves_edits_before_mutation_starts(self):
        first = self.record({"a": b"one"}, ref="main")
        second = self.record({"a": b"two"}, first)
        workspace = self.workspace()
        self.crash_update(workspace.root, second, "journal_published")
        (workspace.root / "a").write_bytes(b"external edit")
        reopened = Workspace(workspace.root)
        self.assertEqual(first, reopened.revision)
        self.assertEqual(b"external edit", (workspace.root / "a").read_bytes())
        self.assertFalse((workspace.control / "transaction.json").exists())

    def test_corrupt_backup_is_retained_without_touching_working_files(self):
        first = self.record({"a": b"one"}, ref="main")
        second = self.record({"a": b"two"}, first)
        workspace = self.workspace()
        self.crash_update(workspace.root, second, "files_applied")
        journal = json.loads((workspace.control / "transaction.json").read_text())
        backup = workspace.control / journal["transaction"] / "backup/a"
        backup.write_bytes(b"damaged")
        with self.assertRaises(ScionError) as raised:
            Workspace(workspace.root)
        self.assertEqual("recovery_required", raised.exception.code)
        self.assertEqual(b"two", (workspace.root / "a").read_bytes())
        self.assertEqual(b"damaged", backup.read_bytes())
        self.assertTrue((workspace.control / "transaction.json").exists())

    def test_lock_timeout_and_release_after_process_death(self):
        workspace = self.workspace()
        marker = self.base / "locked"
        source = """
import sys
from pathlib import Path
from open_scion.workspace import Workspace
workspace = Workspace(sys.argv[1])
with workspace._operation():
    Path(sys.argv[2]).write_text('locked')
    sys.stdin.readline()
"""
        child = self.child(source, workspace.root, marker, stdin=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            with self.assertRaises(ScionError) as raised:
                Workspace(workspace.root, lock_timeout=0.05)
            self.assertEqual("workspace_busy", raised.exception.code)
            child.kill()
            child.communicate(timeout=5)
            reopened = Workspace(workspace.root, lock_timeout=1)
            self.assertEqual([], reopened.status()["changes"])
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_concurrent_process_commits_reload_locked_metadata(self):
        first = self.record({"a": b"one"}, ref="main")
        workspace = self.workspace()
        (workspace.root / "a").write_bytes(b"two")
        source = """
import json
import sys
import time
from pathlib import Path
from open_scion.store import ScionError
from open_scion.workspace import Workspace
workspace = Workspace(sys.argv[1])
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
try:
    result = workspace.commit('concurrent', 'tester')
    print(json.dumps({'id': result['id'], 'parents': result['parents']}))
except ScionError as exc:
    print(json.dumps({'error': exc.code}))
"""
        ready = [self.base / "ready1", self.base / "ready2"]
        go = self.base / "go"
        children = [self.child(source, workspace.root, marker, go) for marker in ready]
        try:
            deadline = time.monotonic() + 10
            while not all(p.exists() for p in ready) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(all(p.exists() for p in ready))
            go.write_text("go")
            results = []
            for child in children:
                out, err = child.communicate(timeout=10)
                self.assertEqual(0, child.returncode, err)
                results.append(json.loads(out))
            successful = [r for r in results if "id" in r]
            self.assertEqual(1, len(successful))
            self.assertEqual([first], successful[0]["parents"])
            self.assertEqual(["nothing_to_commit"], [r["error"] for r in results if "error" in r])
            self.assertEqual(successful[0]["id"], Workspace(workspace.root).revision)
            self.assertEqual(2, self.store.stats()["changesets"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_lock_error_releases_thread_and_file_handles(self):
        workspace = self.workspace()
        with patch.object(workspace._lock, "_lock", side_effect=OSError(1, "not permitted")):
            with self.assertRaises(ScionError) as raised:
                workspace.status()
        self.assertEqual("workspace_lock", raised.exception.code)
        self.assertEqual([], workspace.status()["changes"])

    def test_existing_instance_refreshes_revision_after_another_writer(self):
        self.record({"a": b"one"}, ref="main")
        first = self.workspace()
        second = Workspace(first.root)
        (first.root / "a").write_bytes(b"two")
        committed = first.commit("first writer", "one")
        (first.root / "b").write_bytes(b"three")
        subsequent = second.commit("second writer", "two")
        self.assertEqual([committed["id"]], subsequent["parents"])
        self.assertEqual(subsequent["id"], first.status()["revision"])


if __name__ == "__main__":
    unittest.main()
