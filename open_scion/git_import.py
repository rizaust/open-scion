"""One-way ingestion of a regular-file Git DAG into native changesets."""

from __future__ import annotations

from pathlib import Path
import subprocess

from .store import ScionError, validate_path, validate_ref


def import_git(path, client, ref: str = "main", revision: str = "HEAD") -> dict:
    """Import reachable commits without running hooks or altering the Git repository."""
    validate_ref(ref)
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ScionError("Git source must be an existing directory")

    def git(*args):
        try:
            result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                                    check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScionError(f"Git import failed: {exc}") from exc
        if result.returncode:
            raise ScionError("Git import failed: " + result.stderr.decode("utf-8", errors="replace").strip())
        return result.stdout

    if ref in client.request("GET", "/v1/refs"):
        raise ScionError("Import requires a new ref; choose --ref with an unused name", "stale_ref", 409)
    target = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    records = git("rev-list", "--topo-order", "--reverse", "--parents", target).decode().splitlines()
    if any(len(record.split()) > 3 for record in records):
        raise ScionError("Octopus merges are not supported; import supports up to two parents")

    def tree(oid):
        result = {}
        for item in git("ls-tree", "-rz", "--full-tree", oid).split(b"\0"):
            if not item:
                continue
            header, raw_path = item.split(b"\t", 1)
            mode, kind, blob = header.decode("ascii").split()
            try:
                name = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ScionError("Git paths must be valid UTF-8") from exc
            validate_path(name)
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ScionError(f"Unsupported Git entry {name!r}: {mode} {kind}; symlinks and submodules are excluded")
            result[name] = (mode, blob)
        return result

    commits, blobs = {}, {}
    for record in records:
        oid, *parents = record.split()
        before, after = tree(parents[0]) if parents else {}, tree(oid)
        changes = {}
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) == after.get(name):
                continue
            if name not in after:
                changes[name] = None
                continue
            mode, blob = after[name]
            if blob not in blobs:
                blobs[blob] = client.put_blob(git("cat-file", "blob", blob))
            changes[name] = {"mode": mode, "blob": blobs[blob]}
        author = git("show", "-s", "--format=%an <%ae>", oid).decode("utf-8", errors="replace").strip()
        message = git("show", "-s", "--format=%B", oid).decode("utf-8", errors="replace").rstrip()
        result = client.request("POST", "/v1/changesets", {
            "parents": [commits[parent] for parent in parents], "changes": changes,
            "message": message + "\n\nImported-from-git: " + oid, "author": author})
        commits[oid] = result["id"]
    from urllib.parse import quote
    landed = client.request("POST", "/v1/refs/" + quote(ref, safe=""), {
        "new": commits[target], "expected": None})
    return {"commits": len(commits), "blobs": len(blobs), "git_revision": target,
            "revision": commits[target], "ref": landed}
