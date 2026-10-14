# Durable mutation replay

[HTTP API](API.md) · [Identities](AUTHENTICATION.md) · [Operations](OPERATIONS.md)

Structured mutation endpoints accept an optional `Idempotency-Key` header:
changeset creation, merge, rebase, landing, and direct ref updates. Keys contain
1–128 ASCII letters, digits, dots, colons, underscores, or hyphens.

A replay is identified by the authenticated actor, canonical endpoint path, and
key. The server fingerprints validated JSON arguments after normalizing optional
defaults. Reusing the same key and payload returns the original status and body;
reusing it with a different payload returns `409 idempotency_conflict`.

Successful replay responses carry `Idempotency-Replayed: true`; an initial keyed
success carries `false`. Authentication and capability checks run on every request.
Blob uploads use their content identity and accept no idempotency header.

## Atomicity

A reentrant SQLite unit of work includes all changeset records, ref transitions,
ref-audit events, and the saved response. The response is sent after commit.
Concurrent requests with the same key serialize on the repository write
transaction and observe the same committed result. Server restarts preserve
successful replay records.

If the operation fails, its metadata transaction rolls back and no successful
result is retained. Immutable object files created during an aborted operation
can remain unreferenced for later retention maintenance.

## Retention and capacity

| Setting | Default | Server flag |
| --- | --- | --- |
| Successful result retention | 7 days | `--idempotency-retention` |
| Maximum retained records | 100,000 | `--idempotency-max-records` |
| Maximum canonical JSON response per record | 1 MiB | `--idempotency-max-result-bytes` |

Each write prunes at most 256 expired records. Unexpired results are not evicted to
make room for new keys: capacity exhaustion returns `503 idempotency_capacity`
before invoking the mutation. An oversized result returns `413` and rolls back the
mutation. After expiry, a reused key starts a new operation. Repository snapshots
include retained replay records.

## Client behavior

The native client generates one key per structured request and keeps it across
that request's bounded retries. It retries network errors and selected transient
HTTP statuses for reads, content-addressed uploads, and keyed writes after the
endpoint advertises `Scion-Capabilities: idempotency-v1`.

Terminal mutation errors include the generated key and attempt count, so a caller
can explicitly replay the same logical request. A new CLI invocation is a new
request; callers coordinating retries across process lifetimes can supply the
original key through the Python client:

```python
from open_scion.client import Client

client = Client("http://127.0.0.1:7433")
result = client.request(
    "POST", "/v1/refs/releases/demo",
    {"new": changeset_id, "expected": None},
    idempotency_key="release-demo-001",
)
```

The replay namespace is separate from the operation label used by ref policy and
audit. A keyed direct ref update still evaluates policy as `ref-update`; landing
uses its actual `land` or `land-merge` action.
