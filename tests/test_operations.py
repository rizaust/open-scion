import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from open_scion.operations import Operations, merge_text
from open_scion.store import ScionError, Store


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "server")
        self.ops = Operations(self.store)
        self.base = self.commit([], {"a.txt": b"one\ntwo\nthree\n", "b.txt": b"untouched\n"})
        self.store.update_ref("main", self.base["id"], None)

    def commit(self, parents, files, author="alice"):
        return self.store.create_changeset(parents, {
            path: {"blob": self.store.put_blob(data), "mode": "100644"} if data is not None else None
            for path, data in files.items()}, "test", author)

    def content(self, revision, path):
        return self.store.get_blob(self.store.tree(revision)[path]["blob"])

    def test_disjoint_line_merge_and_blame(self):
        left = self.commit([self.base["id"]], {"a.txt": b"ONE\ntwo\nthree\n"}, "bob")
        right = self.commit([self.base["id"]], {"a.txt": b"one\ntwo\nTHREE\n"}, "carol")
        result = self.ops.merge(None, left["id"], right["id"], "merge", "merger")
        self.assertEqual(self.content(result["id"], "a.txt"), b"ONE\ntwo\nTHREE\n")
        self.assertEqual(self.ops.merge_base(left["id"], right["id"]), self.base["id"])
        self.assertEqual([r["author"] for r in self.ops.blame(result["id"], "a.txt")], ["bob", "alice", "merger"])
        self.assertEqual(self.ops.search(result["id"], "THREE")[0]["line"], 3)

    def test_blame_pins_revision_when_ref_moves_during_read(self):
        deleted = self.commit([self.base["id"]], {"a.txt": None})
        get_changeset = self.store.get_changeset

        def read_then_move_ref(revision):
            record = get_changeset(revision)
            if revision == "main":
                self.store.update_ref("main", deleted["id"], self.base["id"])
            return record

        with patch.object(self.store, "get_changeset", side_effect=read_then_move_ref):
            result = self.ops.blame("main", "a.txt")

        self.assertEqual([line["text"] for line in result], ["one", "two", "three"])
        self.assertEqual({line["changeset"] for line in result}, {self.base["id"]})
        self.assertEqual(self.store.resolve("main"), deleted["id"])

    def test_conflict_does_not_move_ref(self):
        left = self.commit([self.base["id"]], {"a.txt": b"left\n"})
        right = self.commit([self.base["id"]], {"a.txt": b"right\n"})
        self.store.update_ref("main", left["id"], self.base["id"])
        with self.assertRaises(ScionError) as raised:
            self.ops.land(right["id"], "main", left["id"])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.details["conflicts"], ["a.txt"])
        self.assertEqual(self.store.resolve("main"), left["id"])

    def test_delete_modify_and_binary_conflicts(self):
        left = self.commit([self.base["id"]], {"a.txt": None})
        right = self.commit([self.base["id"]], {"a.txt": b"changed\n"})
        with self.assertRaises(ScionError):
            self.ops.merge(None, left["id"], right["id"], "merge", "test")
        left = self.commit([self.base["id"]], {"a.txt": b"a\0"})
        right = self.commit([self.base["id"]], {"a.txt": b"b\0"})
        with self.assertRaises(ScionError):
            self.ops.merge(None, left["id"], right["id"], "merge", "test")
        with self.assertRaises(ScionError):
            self.ops.blame(left["id"], "a.txt")

    def test_rebase_applies_one_delta_and_keeps_source(self):
        left = self.commit([self.base["id"]], {"a.txt": b"changed\n"})
        right = self.commit([self.base["id"]], {"b.txt": b"new\n"})
        result = self.ops.rebase(left["id"], right["id"])
        self.assertEqual(result["parents"], [right["id"]])
        self.assertEqual(self.content(result["id"], "b.txt"), b"new\n")
        self.assertEqual(self.content(left["id"], "b.txt"), b"untouched\n")

    def test_concurrent_land_has_exactly_one_winner(self):
        revisions = [self.commit([self.base["id"]], {f"{i}.txt": str(i).encode()})["id"] for i in range(8)]
        def land(revision):
            try:
                return self.ops.land(revision, "main", self.base["id"])["changeset"]["id"]
            except ScionError as error:
                self.assertEqual(error.status, 409)
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(land, revisions))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertTrue(self.store.fsck()["ok"])

    def test_diff_modes_deletions_and_search_prefix(self):
        target = self.commit([self.base["id"]], {"a.txt": None, "src/new.txt": b"hello world\n"})
        diffs = self.ops.diff(self.base["id"], target["id"])
        self.assertEqual({r["status"] for r in diffs}, {"added", "deleted"})
        self.assertEqual(len(self.ops.search(target["id"], "hello", "src")), 1)
        self.assertEqual(self.ops.search(target["id"], "hello", "b.txt"), [])

    def test_identical_insertions_and_adjacent_edits(self):
        self.assertEqual(merge_text(b"a\nb\n", b"x\na\nb\n", b"x\na\nb\n"), b"x\na\nb\n")
        self.assertEqual(merge_text(b"a\nb\n", b"A\nb\n", b"a\nB\n"), b"A\nB\n")
        self.assertIsNone(merge_text(b"a\n", b"x\na\n", b"y\na\n"))

    def test_file_directory_collision_conflicts(self):
        left = self.commit([self.base["id"]], {"new": b"file"})
        right = self.commit([self.base["id"]], {"new/child": b"child"})
        with self.assertRaises(ScionError) as raised:
            self.ops.merge(None, left["id"], right["id"], "merge", "test")
        self.assertEqual(raised.exception.status, 409)

    def test_land_initial_and_already_landed(self):
        self.assertEqual(self.ops.land(self.base["id"], "release", None)["ref"]["target"], self.base["id"])
        child = self.commit([self.base["id"]], {"new": b"new"})
        self.store.update_ref("main", child["id"], self.base["id"])
        self.assertEqual(self.ops.land(self.base["id"], "main", child["id"])["changeset"]["id"], child["id"])


if __name__ == "__main__":
    unittest.main()
