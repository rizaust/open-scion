"""Run a disposable two-agent workflow against a real HTTP service."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from open_scion.server import make_server
from open_scion.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix="open-scion-demo-") as temporary:
        root = Path(temporary).resolve()
        store = Store(root / "server")
        server = make_server(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        env.pop("SCION_TOKEN", None)

        def scion(*args, cwd=root):
            print("$ scion " + " ".join(args), flush=True)
            result = subprocess.run([sys.executable, "-m", "open_scion", *args],
                                    cwd=cwd, env=env, check=True, text=True,
                                    capture_output=True, timeout=30)
            print(result.stdout.replace(str(root), "<demo>"), end="", flush=True)

        try:
            scion("init", "workspace", "--server", url)
            workspace = root / "workspace"
            (workspace / "src").mkdir()
            (workspace / "docs").mkdir()
            (workspace / "src" / "app.py").write_text('print("hello")\n')
            (workspace / "docs" / "guide.md").write_text("# Hello\n")
            scion("commit", "-m", "Initial project", "--author", "maintainer", cwd=workspace)
            scion("land", "--author", "maintainer", cwd=workspace)
            scion("init", "agent-a", "--server", url, "--sparse", "src")
            scion("init", "agent-b", "--server", url, "--sparse", "docs")
            a, b = root / "agent-a", root / "agent-b"
            (a / "src" / "app.py").write_text('print("hello, agents")\n')
            (b / "docs" / "guide.md").write_text("# Hello, agents\n")
            scion("commit", "-m", "Improve greeting", "--author", "agent-a", cwd=a)
            scion("commit", "-m", "Update guide", "--author", "agent-b", cwd=b)
            scion("land", "--author", "agent-a", cwd=a)
            scion("land", "--author", "agent-b", cwd=b)
            scion("update", cwd=workspace)
            scion("log", "--limit", "4", cwd=workspace)
            scion("search", "agents", cwd=workspace)
            scion("status", cwd=workspace)
            check = store.fsck()
            assert check["ok"], check["errors"]
            print("Repository integrity: OK")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
