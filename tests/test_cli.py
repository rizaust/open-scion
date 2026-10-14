"""CLI integration tests for JSON output and the native workflow."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from open_scion.cli import main
from open_scion.server import make_server
from open_scion.store import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.old_cwd = Path.cwd()
        self.store = Store(self.base / "server")
        self.server = make_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_full_native_workflow(self):
        work = self.base / "work"
        code, out, err = self.run_cli("--json", "init", str(work), "--server", self.url)
        self.assertEqual((0, ""), (code, err))
        self.assertIsNone(json.loads(out)["revision"])
        os.chdir(work)
        (work / "hello.txt").write_text("Hello Scion\n")
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual("added", json.loads(out)["changes"][0]["status"])
        code, out, err = self.run_cli("commit", "-m", "first", "--author", "Alice", "--json")
        self.assertEqual(0, code, err)
        committed = json.loads(out)["id"]
        code, out, err = self.run_cli("land", "--expected", "none", "--json")
        self.assertEqual(0, code, err)
        self.assertEqual(committed, json.loads(out)["ref"]["target"])
        for argv in (("log", "--json"), ("show", "--json"), ("refs", "--json"),
                     ("search", "Scion", "--json"), ("blame", "hello.txt", "--json"),
                     ("diff", "--json"), ("update", "--json")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(0, code, (argv, err))
            json.loads(out)
        code, out, err = self.run_cli("fsck", "--data", str(self.base / "server"), "--json")
        self.assertTrue(json.loads(out)["ok"])

    def test_merge_and_rebase_commands_update_workspace(self):
        blob = self.store.put_blob(b"base")
        base = self.store.create_changeset([], {"base": {"blob": blob, "mode": "100644"}}, "base", "Alice")
        self.store.update_ref("main", base["id"], None)
        left = self.store.create_changeset([base["id"]], {"left": {"blob": blob, "mode": "100644"}}, "left", "Alice")
        right = self.store.create_changeset([base["id"]], {"right": {"blob": blob, "mode": "100644"}}, "right", "Bob")
        work = self.base / "work"
        self.run_cli("init", str(work), "--server", self.url, "--rev", left["id"])
        os.chdir(work)
        code, out, err = self.run_cli("merge", right["id"], "-m", "merge", "--author", "Alice", "--json")
        self.assertEqual(0, code, err)
        self.assertEqual([left["id"], right["id"]], json.loads(out)["parents"])
        self.assertTrue((work / "left").exists())
        self.assertTrue((work / "right").exists())
        code, out, err = self.run_cli("rebase", right["id"], "--revision", left["id"], "--json")
        self.assertEqual(0, code, err)
        self.assertEqual([right["id"]], json.loads(out)["parents"])
        self.assertEqual(base["id"], self.store.list_refs()["main"])

    def test_error_json_is_stderr_and_nonzero(self):
        os.chdir(self.base)
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertEqual("invalid", json.loads(err)["error"]["code"])

    def test_human_readable_status_and_diff(self):
        work = self.base / "work"
        self.run_cli("init", str(work), "--server", self.url)
        os.chdir(work)
        (work / "hello").write_text("Hello\n")
        code, out, err = self.run_cli("status")
        self.assertEqual(0, code, err)
        self.assertIn("A  hello", out)
        code, out, err = self.run_cli("diff")
        self.assertEqual(0, code, err)
        self.assertIn("+Hello", out)


if __name__ == "__main__":
    unittest.main()
