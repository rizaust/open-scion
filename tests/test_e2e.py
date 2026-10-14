"""Exercise the installed CLI protocol through an actual HTTP server."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from open_scion.server import make_server
from open_scion.store import Store


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.server = make_server(self.store, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), SCION_TOKEN="test-secret")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def cli(self, *args, cwd=None, ok=True):
        result = subprocess.run([sys.executable, "-m", "open_scion", "--json", *args],
                                cwd=cwd or self.root, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def test_two_agents_commit_merge_land_and_sparse_update(self):
        self.cli("init", "seed", "--server", self.url)
        seed = self.root / "seed"
        (seed / "src").mkdir()
        (seed / "docs").mkdir()
        (seed / "src" / "app.txt").write_text("hello\n")
        (seed / "docs" / "guide.txt").write_text("guide\n")
        first = self.cli("commit", "-m", "initial", "--author", "seed", cwd=seed)
        self.cli("land", cwd=seed)
        self.cli("init", "agent-a", "--server", self.url, "--sparse", "src")
        self.cli("init", "agent-b", "--server", self.url, "--sparse", "docs")
        a, b = self.root / "agent-a", self.root / "agent-b"
        self.assertFalse((a / "docs").exists())
        self.assertFalse((b / "src").exists())
        (a / "src" / "app.txt").write_text("agent a\n")
        (b / "docs" / "guide.txt").write_text("agent b\n")
        ca = self.cli("commit", "-m", "application", "--author", "agent-a", cwd=a)
        cb = self.cli("commit", "-m", "documentation", "--author", "agent-b", cwd=b)
        self.assertEqual(self.store.resolve("main"), first["id"])
        self.cli("land", cwd=a)
        landed = self.cli("land", cwd=b)
        self.assertEqual(landed["changeset"]["parents"], [ca["id"], cb["id"]])
        self.cli("update", cwd=seed)
        self.assertEqual((seed / "src" / "app.txt").read_text(), "agent a\n")
        self.assertEqual((seed / "docs" / "guide.txt").read_text(), "agent b\n")
        self.assertEqual(self.cli("status", cwd=seed)["changes"], [])
        self.assertEqual(len(self.cli("search", "agent", cwd=a)), 1)
        self.cli("update", cwd=a)
        self.assertEqual(len(self.cli("search", "agent", cwd=a)), 2)
        self.assertNotIn("test-secret", (a / ".scion" / "workspace.json").read_text())
        self.assertTrue(self.cli("fsck", "--data", str(self.root / "data"))["ok"])

    def test_conflicting_agent_retains_work_and_shared_tip(self):
        self.cli("init", "one", "--server", self.url)
        one, two = self.root / "one", self.root / "two"
        (one / "file").write_text("base\n")
        self.cli("commit", "-m", "base", cwd=one)
        self.cli("land", cwd=one)
        self.cli("init", "two", "--server", self.url)
        (one / "file").write_text("left\n")
        (two / "file").write_text("right\n")
        left = self.cli("commit", "-m", "left", cwd=one)
        right = self.cli("commit", "-m", "right", cwd=two)
        self.cli("land", cwd=one)
        error = self.cli("land", cwd=two, ok=False)
        self.assertEqual(error["error"]["code"], "conflict")
        self.assertEqual(self.store.resolve("main"), left["id"])
        self.assertEqual(self.cli("status", cwd=two)["revision"], right["id"])
        self.assertEqual((two / "file").read_text(), "right\n")


if __name__ == "__main__":
    unittest.main()
