"""Exercise the deployed container and print only non-secret verification data."""

import json
import os
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def request(path, body=None):
    headers = {"Authorization": "Bearer " + os.environ["SCION_TOKEN"]}
    if isinstance(body, bytes):
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urlopen(Request("http://127.0.0.1:7433" + path, data=body, headers=headers), timeout=3) as response:
        payload = response.read()
        return json.loads(payload) if "json" in response.headers["Content-Type"] else payload


for attempt in range(30):
    try:
        request("/ready")
        break
    except (URLError, TimeoutError):
        time.sleep(1)
else:
    raise SystemExit("Container did not become ready")

if sys.argv[1:] == ["verify"]:
    refs = request("/v1/refs")
    tree = request("/v1/tree?rev=main")
    assert request("/v1/blobs/" + tree["hello.txt"]["blob"]) == b"durable container\n"
    assert request("/v1/ref-log?name=main")[0]["new"] == refs["main"]
    print("Persisted repository and audit trail verified after container restart")
else:
    blob = request("/v1/blobs", b"durable container\n")["id"]
    record = request("/v1/changesets", {"parents": [], "changes": {"hello.txt": {"blob": blob, "mode": "100644"}},
                                              "message": "container smoke", "author": "CI"})
    request("/v1/refs/main", {"new": record["id"], "expected": None})
    assert request("/v1/metrics")["requests"] >= 3
    print("Container API, persistence, and metrics verified")
