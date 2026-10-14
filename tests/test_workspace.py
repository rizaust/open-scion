"""Working-copy data preservation and cache-integrity regression tests."""

from __future__ import annotations

import json
import os
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
        with patch("open_scion.workspace.CACHE_LIMIT", 6):
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
        with patch("open_scion.workspace.NATIVE_EXECUTABLE_BITS", False):
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
        replace = os.replace
        def fail_restore(source, destination):
            if Path(source).parent.name.startswith("update-"):
                raise OSError("restore failure")
            return replace(source, destination)
        with patch.object(workspace, "_save", side_effect=OSError("save failure")), patch("open_scion.workspace.os.replace", side_effect=fail_restore):
            with self.assertRaises(ScionError) as raised:
                workspace.update()
        self.assertEqual(raised.exception.code, "recovery_required")
        backup = Path(raised.exception.details["backup"])
        self.assertEqual((backup / "a").read_bytes(), b"original")
        self.assertEqual(workspace.revision, first)

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


if __name__ == "__main__":
    unittest.main()
