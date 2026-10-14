"""Online snapshot, immutable history, and restore integrity tests."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from open_scion.store import backup
from open_scion.store.backup import restore_backup
from open_scion.store import ScionError, Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "source")

    def commit(self, content=b"hello", parent=None):
        blob = self.store.put_blob(content)
        return self.store.create_changeset([parent] if parent else [],
                                           {"src/file.bin": {"blob": blob, "mode": "100755"}}, "test", "Tester")

    def read_manifest(self, directory):
        return json.loads((directory / "manifest.json").read_text())

    def rewrite_digest(self, directory, name):
        manifest = self.read_manifest(directory)
        data = (directory / name).read_bytes()
        manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        (directory / "manifest.json").write_text(json.dumps(manifest))

    def test_round_trip_preserves_history_refs_and_audit(self):
        a = self.commit(bytes(range(256)))
        self.store.update_ref("main", a["id"], None)
        b = self.commit(b"second", a["id"])
        self.store.update_ref("main", b["id"], a["id"])
        draft = self.commit(b"unlanded", b["id"])
        unused = self.store.put_blob(b"uploaded but never committed")
        result = self.store.backup(self.root / "snapshot")
        self.assertEqual(result["changesets"], 3)
        self.assertEqual(result["ref_events"], 2)
        manifest = self.read_manifest(self.root / "snapshot")
        self.assertEqual(manifest["format_version"], 1)
        self.assertNotIn(f"objects/{unused[:2]}/{unused[2:]}", manifest["files"])
        self.assertFalse((self.root / "snapshot" / "scion.db-wal").exists())
        restored = restore_backup(self.root / "snapshot", self.root / "restored")
        self.assertTrue(restored["check"]["ok"])
        store = Store(self.root / "restored")
        self.assertEqual(store.list_refs(), self.store.list_refs())
        self.assertEqual(store.ref_log(), self.store.ref_log())
        self.assertEqual(store.get_changeset(draft["id"]), draft)
        self.assertEqual(store.tree(a["id"]), self.store.tree(a["id"]))
        self.assertEqual(store.get_blob(store.tree(a["id"])["src/file.bin"]["blob"]), bytes(range(256)))

    def test_backup_stays_on_pinned_revision_while_writer_advances(self):
        a = self.commit()
        self.store.update_ref("main", a["id"], None)
        copying = threading.Event()
        finished = threading.Event()
        failures = []
        def writer():
            try:
                if not copying.wait(5):
                    raise AssertionError("Backup never started copying objects")
                parent = a["id"]
                for index in range(12):
                    revision = self.commit(str(index).encode(), parent)
                    self.store.update_ref("main", revision["id"], parent)
                    parent = revision["id"]
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=writer)
        thread.start()
        original = backup._copy_file
        def concurrent_copy(*args, **kwargs):
            copying.set()
            self.assertTrue(finished.wait(5), "Writer blocked behind online backup")
            return original(*args, **kwargs)
        try:
            with patch.object(backup, "_copy_file", concurrent_copy):
                result = self.store.backup(self.root / "snapshot")
        finally:
            copying.set()
            thread.join(5)
        self.assertFalse(failures, failures)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["changesets"], 1)
        self.assertEqual(result["ref_events"], 1)
        restore_backup(self.root / "snapshot", self.root / "restored")
        restored = Store(self.root / "restored")
        self.assertEqual(restored.resolve("main"), a["id"])
        self.assertEqual(len(restored.ref_log()), 1)
        self.assertEqual(self.store.stats()["changesets"], 13)
        self.assertTrue(self.store.fsck()["ok"])

    def test_empty_repository_round_trip(self):
        self.store.backup(self.root / "snapshot")
        restore_backup(self.root / "snapshot", self.root / "restored")
        restored = Store(self.root / "restored")
        self.assertEqual(restored.list_refs(), {})
        self.assertTrue(restored.fsck()["ok"])

    def test_existing_destination_is_never_overwritten(self):
        self.store.backup(self.root / "snapshot")
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "keep").write_text("preserve")
        for action in [lambda: self.store.backup(existing),
                       lambda: restore_backup(self.root / "snapshot", existing)]:
            with self.assertRaises(ScionError):
                action()
        self.assertEqual((existing / "keep").read_text(), "preserve")

    def test_atomic_publish_rejects_destination_created_during_restore(self):
        self.store.backup(self.root / "snapshot")
        target = self.root / "restored"
        original = backup._publish_directory
        def race(staging, destination):
            destination.mkdir()
            return original(staging, destination)
        with patch.object(backup, "_publish_directory", race), self.assertRaises((ScionError, FileExistsError)):
            restore_backup(self.root / "snapshot", target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(list(self.root.glob(".restored.restore-*")))

    def test_checksum_tampering_rejected_without_partial_destination(self):
        self.commit()
        self.store.backup(self.root / "snapshot")
        snapshot = self.root / "snapshot"
        name = next(name for name in self.read_manifest(snapshot)["files"] if name.startswith("objects/"))
        (snapshot / name).write_bytes(b"tampered")
        with self.assertRaises(ScionError) as raised:
            restore_backup(snapshot, self.root / "restored")
        self.assertEqual(raised.exception.code, "corrupt_backup")
        self.assertFalse((self.root / "restored").exists())
        self.assertFalse(list(self.root.glob(".restored.restore-*")))

    def test_content_hash_detects_tamper_even_with_rewritten_manifest(self):
        self.commit()
        self.store.backup(self.root / "snapshot")
        snapshot = self.root / "snapshot"
        name = next(name for name in self.read_manifest(snapshot)["files"] if name.startswith("objects/"))
        (snapshot / name).write_bytes(b"tampered")
        self.rewrite_digest(snapshot, name)
        with self.assertRaises(ScionError) as raised:
            restore_backup(snapshot, self.root / "restored")
        self.assertIn("content integrity", str(raised.exception))

    def test_ref_audit_detects_tampered_database(self):
        a = self.commit()
        self.store.update_ref("main", a["id"], None)
        b = self.commit(b"next", a["id"])
        self.store.update_ref("main", b["id"], a["id"])
        self.store.backup(self.root / "snapshot")
        snapshot = self.root / "snapshot"
        with closing(sqlite3.connect(snapshot / "scion.db")) as db, db:
            db.execute("UPDATE refs SET target=? WHERE name='main'", (a["id"],))
        self.rewrite_digest(snapshot, "scion.db")
        with self.assertRaises(ScionError) as raised:
            restore_backup(snapshot, self.root / "restored")
        self.assertTrue(any("audit disagrees" in error for error in raised.exception.details["errors"]))

    def test_manifest_traversal_and_unlisted_files_rejected(self):
        self.store.backup(self.root / "snapshot")
        snapshot = self.root / "snapshot"
        manifest = self.read_manifest(snapshot)
        manifest["files"]["../outside"] = {"sha256": "0" * 64, "size": 0}
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ScionError):
            restore_backup(snapshot, self.root / "restored")
        self.assertFalse((self.root / "outside").exists())
        del manifest["files"]["../outside"]
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
        (snapshot / "scion.db-wal").write_bytes(b"unexpected")
        with self.assertRaises(ScionError):
            restore_backup(snapshot, self.root / "restored")

    def test_symlinked_snapshot_object_rejected(self):
        self.commit()
        self.store.backup(self.root / "snapshot")
        snapshot = self.root / "snapshot"
        name = next(name for name in self.read_manifest(snapshot)["files"] if name.startswith("objects/"))
        target = snapshot / name
        outside = self.root / "outside"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(ScionError):
            restore_backup(snapshot, self.root / "restored")


if __name__ == "__main__":
    unittest.main()
