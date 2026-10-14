"""Exercise the Git adapter against real Git objects and the live HTTP API."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from open_scion.client import Client
from open_scion.git_import import import_git
from open_scion.server import make_server
from open_scion.store import ScionError, Store


@unittest.skipUnless(shutil.which("git"), "Git is required for adapter integration tests")
class GitImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.environment = {name: value for name, value in os.environ.items()
                            if not name.startswith("GIT_")}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0"})
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test Author")
        self.git("config", "user.email", "test@example.invalid")
        self.store = Store(self.root / "server")
        self.server = make_server(self.store, token="git-import-test-token")
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.client = Client(f"http://127.0.0.1:{self.server.server_address[1]}",
                             token="git-import-test-token")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.assertFalse(self.worker.is_alive())

    def git(self, *arguments, input=None):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
             "-C", str(self.source), *arguments], input=input, capture_output=True,
            check=False, env=self.environment, timeout=20)
        if result.returncode:
            self.fail(f"Git failed: {arguments!r}: {result.stderr.decode(errors='replace')}")
        return result.stdout

    def write(self, name, content, executable=False):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if executable:
            path.chmod(0o755)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def native_graph(self, revision):
        graph = {}
        pending = [revision]
        while pending:
            oid = pending.pop()
            if oid in graph:
                continue
            record = self.client.request("GET", "/v1/changesets/" + oid)
            graph[oid] = record
            pending.extend(record["parents"])
        return graph

    def assert_imported_tree_matches_git(self, git_oid, native_oid):
        expected = {}
        for item in self.git("ls-tree", "-rz", "--full-tree", git_oid).split(b"\0"):
            if not item:
                continue
            header, raw_path = item.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            self.assertEqual(kind, "blob")
            expected[raw_path.decode()] = (mode, self.git("cat-file", "blob", oid))
        actual = self.client.request("GET", "/v1/tree?rev=" + native_oid)
        self.assertEqual(set(actual), set(expected))
        for path, (mode, content) in expected.items():
            self.assertEqual(actual[path]["mode"], mode)
            self.assertEqual(self.client.get_blob(actual[path]["blob"]), content)

    def test_import_preserves_full_dag_modes_deletions_and_binary_content(self):
        self.write("README.txt", b"root\n")
        self.write("remove-me.txt", b"remove later\n")
        self.write("assets/binary.dat", b"\x00\xff\x80\r\n")
        self.write("bin/run.sh", b"#!/bin/sh\nprintf 'hello\\n'\n", executable=True)
        self.write("docs/你好.txt", "Unicode paths survive.\n".encode())
        base = self.commit("Root commit")
        self.git("checkout", "-q", "-b", "feature")
        self.write("feature.txt", b"feature branch\n")
        feature = self.commit("Feature work")
        self.git("checkout", "-q", "main")
        (self.source / "remove-me.txt").unlink()
        self.write("README.txt", b"main branch\n")
        main = self.commit("Main work and deletion")
        self.git("merge", "--no-ff", "-q", "-m", "Merge feature", "feature")
        head = self.git("rev-parse", "HEAD").decode().strip()
        source_status = self.git("status", "--porcelain=v1")
        result = import_git(self.source, self.client, ref="imported/main")
        self.assertEqual(result["commits"], 4)
        self.assertEqual(result["git_revision"], head)
        self.assertEqual(result["ref"], {"name": "imported/main", "target": result["revision"]})
        graph = self.native_graph(result["revision"])
        self.assertEqual(len(graph), 4)
        by_git = {record["message"].rsplit("Imported-from-git: ", 1)[1]: record
                  for record in graph.values()}
        self.assertEqual(set(by_git), {base, main, feature, head})
        self.assertEqual(by_git[head]["parents"], [by_git[main]["id"], by_git[feature]["id"]])
        self.assertEqual(by_git[main]["parents"], [by_git[base]["id"]])
        self.assertEqual(by_git[feature]["parents"], [by_git[base]["id"]])
        self.assertEqual(by_git[base]["parents"], [])
        self.assertIsNone(by_git[main]["changes"]["remove-me.txt"])
        for git_oid, record in by_git.items():
            self.assertEqual(record["author"], "Test Author <test@example.invalid>")
            self.assert_imported_tree_matches_git(git_oid, record["id"])
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), head)
        self.assertEqual(self.git("status", "--porcelain=v1"), source_status)
        self.assertTrue(self.store.fsck()["ok"])

    def test_existing_ref_is_rejected_without_adding_changesets(self):
        self.write("file.txt", b"first\n")
        self.commit("First")
        imported = import_git(self.source, self.client)
        before = self.client.request("GET", "/v1/stats")
        self.write("file.txt", b"second\n")
        self.commit("Second")
        with self.assertRaises(ScionError) as raised:
            import_git(self.source, self.client)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.client.request("GET", "/v1/refs"), {"main": imported["revision"]})
        self.assertEqual(self.client.request("GET", "/v1/stats"), before)

    def test_explicit_revision_imports_only_its_reachable_history(self):
        self.write("file.txt", b"first\n")
        first = self.commit("First")
        self.write("file.txt", b"second\n")
        self.commit("Second")
        result = import_git(self.source, self.client, ref="historical", revision=first)
        self.assertEqual(result["commits"], 1)
        self.assertEqual(result["git_revision"], first)
        self.assert_imported_tree_matches_git(first, result["revision"])

    def test_tracked_symlink_is_rejected_without_publishing_a_ref(self):
        self.write("target.txt", b"target\n")
        (self.source / "link.txt").symlink_to("target.txt")
        self.commit("Contains a symlink")
        with self.assertRaises(ScionError) as raised:
            import_git(self.source, self.client)
        self.assertIn("symlinks", str(raised.exception))
        self.assertEqual(self.client.request("GET", "/v1/refs"), {})

    def test_gitlink_is_rejected_without_publishing_a_ref(self):
        self.write("root.txt", b"root\n")
        root = self.commit("Root")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{root},vendor/submodule")
        self.git("commit", "-q", "-m", "Contains a gitlink")
        with self.assertRaises(ScionError) as raised:
            import_git(self.source, self.client)
        self.assertIn("submodules", str(raised.exception))
        self.assertEqual(self.client.request("GET", "/v1/refs"), {})

    def test_octopus_merge_is_rejected_before_uploading_changesets(self):
        self.write("root.txt", b"root\n")
        root = self.commit("Root")
        tips = []
        for branch in ("one", "two", "three"):
            self.git("checkout", "-q", "-b", branch, root)
            self.write(branch + ".txt", branch.encode())
            tips.append(self.commit(branch))
        tree = self.git("rev-parse", tips[0] + "^{tree}").decode().strip()
        octopus = self.git("commit-tree", tree, "-p", tips[0], "-p", tips[1], "-p", tips[2],
                           input=b"Octopus\n").decode().strip()
        with self.assertRaises(ScionError) as raised:
            import_git(self.source, self.client, revision=octopus)
        self.assertIn("Octopus", str(raised.exception))
        self.assertEqual(self.client.request("GET", "/v1/stats")["changesets"], 0)


if __name__ == "__main__":
    unittest.main()
