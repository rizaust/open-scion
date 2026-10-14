"""Storage invariants and concurrent mutation tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from contextlib import closing
import random
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from open_scion.store import Store, ScionError, blob_id, validate_path, validate_ref


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)

    def entry(self, data=b"hello\n", mode="100644"):
        return {"blob": self.store.put_blob(data), "mode": mode}

    def commit(self, changes, parent=None):
        return self.store.create_changeset([parent] if parent else [], changes, "A commit", "Tester")

    def test_blobs_binary_deduplicated_and_persistent(self):
        data = bytes(range(256)) * 200
        oid = self.store.put_blob(data)
        self.assertEqual(oid, hashlib.sha256(b"blob 51200\0" + data).hexdigest())
        self.assertEqual(oid, blob_id(data))
        self.assertEqual(self.store.put_blob(data), oid)
        self.assertEqual(Store(self.temporary.name).get_blob(oid), data)
        self.assertEqual(self.store.stats()["blobs"], 1)
        self.assertLess(self.store.stats()["compressed_bytes"], len(data))

    def test_history_refs_modes_and_tree_prefix(self):
        a = self.commit({"src/a.py": self.entry(), "script": self.entry(b"#!/bin/sh\n", "100755")})
        b = self.commit({"src/a.py": self.entry(b"new\n"), "src/b.py": self.entry(b"second")}, a["id"])
        c = self.commit({"script": None}, b["id"])
        self.store.update_ref("main", c["id"], None)
        self.assertEqual(self.store.resolve("main"), c["id"])
        self.assertEqual(self.store.get_changeset("main"), c)
        self.assertEqual(self.store.tree(a["id"])["script"]["mode"], "100755")
        self.assertNotIn("script", self.store.tree(c["id"]))
        self.assertEqual(set(self.store.tree("main", "src")), {"src/a.py", "src/b.py"})
        self.assertEqual(set(self.store.tree("main", "src/a.py")), {"src/a.py"})
        self.assertEqual(self.store.tree("main", "sr"), {})
        self.assertEqual([x["id"] for x in self.store.log("main")], [c["id"], b["id"], a["id"]])
        self.assertEqual([x["id"] for x in self.store.log("main", path="src")], [b["id"], a["id"]])
        self.assertEqual(len(self.store.log("main", limit=1)), 1)
        self.assertEqual(c["generation"], 2)
        self.assertTrue(self.store.fsck()["ok"])

    def test_path_copy_reuses_unmodified_subtrees(self):
        root = self.commit({"left/a": self.entry(b"a"), "right/b": self.entry(b"b")})
        old_tree = self.store._read_tree(root["tree"])
        before = self.store.stats()["trees"]
        child = self.commit({"left/a": self.entry(b"changed")}, root["id"])
        new_tree = self.store._read_tree(child["tree"])
        self.assertEqual(old_tree["right"], new_tree["right"])
        self.assertNotEqual(old_tree["left"], new_tree["left"])
        self.assertEqual(self.store.stats()["trees"] - before, 2)
        unchanged = self.commit({"left/a": self.store.tree(child["id"])["left/a"]}, child["id"])
        self.assertEqual(child["tree"], unchanged["tree"])

    def test_batch_update_writes_each_directory_once(self):
        changes = {f"source/package/file{i}.py": self.entry(str(i).encode()) for i in range(100)}
        before = self.store.stats()["trees"]
        result = self.commit(changes)
        self.assertEqual(self.store.stats()["trees"] - before, 3)
        self.assertEqual(len(self.store.tree(result["id"])), 100)

    def test_file_directory_transitions_are_explicit(self):
        a = self.commit({"thing": self.entry()})
        with self.assertRaises(ScionError):
            self.commit({"thing/child": self.entry()}, a["id"])
        b = self.commit({"thing": None, "thing/child": self.entry(b"child")}, a["id"])
        self.assertEqual(set(self.store.tree(b["id"])), {"thing/child"})
        with self.assertRaises(ScionError):
            self.commit({"thing": self.entry()}, b["id"])
        c = self.commit({"thing/child": None, "thing": self.entry()}, b["id"])
        self.assertEqual(set(self.store.tree(c["id"])), {"thing"})
        with self.assertRaises(ScionError):
            self.commit({"a": self.entry(), "a/b": self.entry()}, c["id"])
        self.assertEqual(self.store.stats()["changesets"], 3)

    def test_rejects_unsafe_paths_and_modes(self):
        for path in ["", "/a", "a//b", "a/", "./a", "a/../b", "a\\b", "a\0b", ".git/x", "x/.scion/y", ".SCION/x", ".Git/x"]:
            with self.subTest(path=path), self.assertRaises(ScionError):
                validate_path(path)
        for name in ["", "../main", ".hidden", "feature//x", "a.lock", "a..b", "a" * 64]:
            with self.subTest(name=name), self.assertRaises(ScionError):
                validate_ref(name)
        with self.assertRaises(ScionError):
            self.commit({"symlink": self.entry(mode="120000")})
        with self.assertRaises(ScionError):
            self.commit({"missing": {"blob": "a" * 64, "mode": "100644"}})
        with self.assertRaises(ScionError):
            self.store.get_blob("../../secret")
        self.assertEqual(self.store.stats()["changesets"], 0)

    def test_ref_compare_and_swap_and_concurrent_winner(self):
        a = self.commit({"a": self.entry()})
        b = self.commit({"a": self.entry(b"b")}, a["id"])
        c = self.commit({"a": self.entry(b"c")}, a["id"])
        self.store.update_ref("main", a["id"], None)
        with self.assertRaises(ScionError) as rejected:
            self.store.update_ref("main", b["id"], None)
        self.assertEqual(rejected.exception.status, 409)
        barrier = threading.Barrier(2)
        def attempt(target):
            barrier.wait()
            try:
                self.store.update_ref("main", target, a["id"])
                return "ok"
            except ScionError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [b["id"], c["id"]]))
        self.assertCountEqual(results, ["ok", 409])
        self.assertIn(self.store.resolve("main"), [b["id"], c["id"]])
        self.assertTrue(self.store.fsck()["ok"])

    def test_parallel_blob_install_and_changesets(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.store.put_blob(b"same" * 10000), range(16)))
        self.assertEqual(len(set(ids)), 1)
        root = self.commit({})
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda i: self.commit({f"file{i}": self.entry(str(i).encode())}, root["id"]), range(8)))
        self.assertEqual(len({r["id"] for r in records}), 8)
        self.assertTrue(self.store.fsck()["ok"])

    def test_random_deltas_match_flat_reference(self):
        generator = random.Random(17)
        expected = {}
        parent = None
        paths = [f"dir{i}/sub{j}/file{k}" for i in range(3) for j in range(2) for k in range(3)]
        for _ in range(30):
            changes = {}
            for path in generator.sample(paths, 6):
                if generator.random() < 0.3:
                    changes[path] = None
                    expected.pop(path, None)
                else:
                    changes[path] = self.entry(str(generator.randrange(20)).encode())
                    expected[path] = changes[path]
            record = self.commit(changes, parent)
            parent = record["id"]
            self.assertEqual(self.store.tree(parent), expected)
        self.assertTrue(self.store.fsck()["ok"])

    def test_sqlite_changesets_are_immutable(self):
        record = self.commit({})
        with closing(sqlite3.connect(self.store.db_path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE changesets SET generation=4 WHERE id=?", (record["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM changesets WHERE id=?", (record["id"],))

    def test_corrupt_and_missing_object_detection(self):
        entry = self.entry()
        self.commit({"a": entry})
        path = self.store._object_path(entry["blob"])
        path.write_bytes(b"not zlib")
        with self.assertRaises(ScionError) as raised:
            self.store.get_blob(entry["blob"])
        self.assertEqual(raised.exception.code, "corrupt")
        check = self.store.fsck()
        self.assertFalse(check["ok"])
        self.assertTrue(any(entry["blob"] in error for error in check["errors"]))
        path.unlink()
        self.assertFalse(self.store.fsck()["ok"])

    def test_ref_audit_is_transactional_and_immutable(self):
        a = self.commit({})
        b = self.commit({"a": self.entry()}, a["id"])
        self.store.update_ref("main", a["id"], None)
        self.store.update_ref("main", b["id"], a["id"])
        with self.assertRaises(ScionError):
            self.store.update_ref("main", a["id"], a["id"])
        history = self.store.ref_log("main")
        self.assertEqual([(row["old"], row["new"]) for row in history], [(a["id"], b["id"]), (None, a["id"])])
        self.assertGreater(history[0]["sequence"], history[1]["sequence"])
        self.assertEqual(self.store.ref_log("absent"), [])
        self.assertEqual(len(self.store.ref_log(limit=1)), 1)
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE ref_audit SET name='other'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM ref_audit")
            db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON ref_audit BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_ref("main", a["id"], b["id"])
        self.assertEqual(self.store.resolve("main"), b["id"])
        self.assertEqual(len(self.store.ref_log()), 2)

    def test_audit_migration_preserves_existing_refs(self):
        a = self.commit({})
        self.store.update_ref("main", a["id"], None)
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("DROP TABLE ref_audit")
        reopened = Store(self.temporary.name)
        self.assertEqual(reopened.resolve("main"), a["id"])
        self.assertEqual(reopened.ref_log(), [])
        b = self.commit({}, a["id"])
        reopened.update_ref("main", b["id"], a["id"])
        self.assertEqual(reopened.ref_log()[0]["old"], a["id"])
        self.assertTrue(reopened.fsck()["ok"])

    def test_fsck_pins_metadata_before_concurrent_objects(self):
        a = self.commit({"a": self.entry()})
        self.store.update_ref("main", a["id"], None)
        original = self.store._object_files
        def inventory_after_new_commit():
            b = self.commit({"nested/b": self.entry(b"new")}, a["id"])
            self.store.update_ref("main", b["id"], a["id"])
            return original()
        with patch.object(self.store, "_object_files", inventory_after_new_commit):
            check = self.store.fsck()
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["changesets"], 1)
        self.assertEqual(check["ref_events"], 1)
        self.assertEqual(self.store.stats()["changesets"], 2)

    def test_fsck_follows_edges_missing_from_racing_inventory(self):
        a = self.commit({"nested/a": self.entry()})
        with patch.object(self.store, "_object_files", return_value=iter([self.store._object_path(a["tree"])])):
            check = self.store.fsck()
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["objects"], 4)

    def test_read_only_verification_never_recreates_missing_objects(self):
        self.store._object_path(self.store._empty_tree).unlink()
        check = Store(self.temporary.name, read_only=True).fsck()
        self.assertFalse(check["ok"])
        self.assertFalse(self.store._object_path(self.store._empty_tree).exists())
        with self.assertRaises(ScionError):
            Store(self.temporary.name, read_only=True).put_blob(b"forbidden")

    def test_parent_validation(self):
        a = self.commit({})
        b = self.commit({}, a["id"])
        merged = self.store.create_changeset([a["id"], b["id"]], {}, "merge", "Tester")
        self.assertEqual(merged["parents"], [a["id"], b["id"]])
        self.assertEqual(merged["generation"], 2)
        with self.assertRaises(ScionError):
            self.store.create_changeset([a["id"], a["id"]], {}, "bad", "Tester")
        with self.assertRaises(ScionError):
            self.store.create_changeset([a["id"]] * 3, {}, "bad", "Tester")
        with self.assertRaises(ScionError):
            self.store.create_changeset(["missing"], {}, "bad", "Tester")


if __name__ == "__main__":
    unittest.main()
