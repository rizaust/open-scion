# Operating Open Scion

[English README](../README.md) · [中文说明](../README.zh-CN.md) · [HTTP API](API.md)

## Run a repository service

Install the package and supply the same `SCION_TOKEN` to the server and its clients.
Generate a token locally and store it in your service manager's secret facility:

```sh
export SCION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
scion serve --data /srv/open-scion \
  --host 127.0.0.1 --port 7433 \
  --max-workers 32 --request-timeout 15 \
  --max-inflight-bytes 67108864 --access-log
```

The environment value is never saved in workspace metadata. Keep it available in
the client environment. For remote clients, terminate HTTPS in a reverse proxy or
forward the loopback port through SSH. Preserve the `Authorization` header when
proxying requests. A bearer token grants access to this repository service.

The service admits at most `--max-workers` connections and reserves request body
bytes against `--max-inflight-bytes` before reading them. Saturated requests receive
HTTP 503 and `Retry-After: 1`; callers can retry after that delay. The socket timeout
limits I/O inactivity. SIGTERM stops admission, gives active requests one configured
timeout interval to drain, and then closes remaining worker sockets.

## Container deployment

The supplied image runs as UID/GID 10001 and stores repository data at
`/var/lib/open-scion`. The Compose configuration includes a persistent volume,
read-only root filesystem, resource limits, dropped capabilities, and a readiness
healthcheck.

```sh
export SCION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build -d
docker compose ps
docker compose logs --tail 50 scion
```

Compose publishes `127.0.0.1:7433`. Its named volume survives container replacement.
Keep the token in your deployment environment when restarting or upgrading. If you
use a bind mount instead of the named volume, make it writable by UID/GID 10001.

```sh
docker compose stop
docker compose up --build -d
```

CI builds the image, exercises authenticated writes and metrics, creates and
restores a live snapshot, then restarts the container and verifies its persisted
content and audit trail.

## Health, readiness, and metrics

| Endpoint | Meaning |
| --- | --- |
| `GET /health` | HTTP process liveness |
| `GET /ready` | Repository metadata is readable and required schema is present |
| `GET /v1/metrics` | Authenticated worker, body-admission, request, status, and uptime counters |

```sh
curl http://127.0.0.1:7433/ready
curl -H "Authorization: Bearer $SCION_TOKEN" http://127.0.0.1:7433/v1/metrics
```

Metrics count request attempts, including overloaded or abandoned connections.
Status counters record terminal outcomes; `active` includes the metrics request
itself. Labels are bounded to HTTP status codes. Use sustained 503 responses,
growing active requests, readiness failures, and error counts as operating signals.

`--access-log` emits JSON lines with a generated request ID, method, path, status,
duration in milliseconds, and peer IP. Request query strings, headers, and bodies
are excluded. Responses carry the same ID in `X-Request-ID`, so clients can attach
it to bug reports without including credentials or source content.

## Online backups and verified restore

Run backup on the server host while the repository service remains online:

```sh
mkdir -p /srv/scion-snapshots
scion backup --data /srv/open-scion \
  --output /srv/scion-snapshots/snapshot-001 --json
```

Every output directory is new. The snapshot combines a SQLite backup with the
immutable objects referenced by that captured database and a digest manifest. The
backup is verified before publication. Concurrent commits after the captured
database snapshot remain in the live repository and belong to a later backup.

Store snapshots on an independent volume or backup service according to your
retention policy. Digest checks detect changed or missing files; control access to
the snapshot and its manifest together.

Restore into a new directory and inspect it before pointing a service at it:

```sh
scion restore /srv/scion-snapshots/snapshot-001 \
  --data /srv/open-scion-restored --json
scion fsck --data /srv/open-scion-restored --json
```

Restore checks the manifest, object content, and repository consistency in a
staging directory, then publishes the destination. It refuses existing destination
paths. Practice a restore as part of the backup schedule; a copied live SQLite file
alone is not a repository snapshot.

## Ref audit trail

Each successful compare-and-swap appends an audit record in the same database
transaction as the ref update. Failed comparisons append no success record.

```sh
scion ref-log --name main --limit 50 --json
```

The record contains an ordered sequence number, ref name, previous target, new
target, and server timestamp. Audit history is included in repository snapshots.

## Working-copy recovery

Workspace operations acquire an OS advisory lock and reload workspace metadata
after acquisition. Independent agents should normally use independent workspaces;
when processes share one workspace, the lock serializes their mutations. A process
exit releases the OS lock automatically.

An update stages its original files and target contents, flushes a transaction
journal, and commits by atomically replacing workspace metadata. Opening a workspace
recovers an interrupted transaction before proceeding. Recovery restores the
previous state before that commit point or completes cleanup after it.

```sh
scion recover --json
scion status
```

Recovery also works without reaching the server. If files were externally edited
after interruption, recovery retains the saved copies and reports their location
for reconciliation. Keep the `.scion` directory with its workspace when moving or
backing up working files.

## Upgrades

Create a snapshot before upgrading. Storage initialization applies additive schema
updates when the new service opens a repository; immutable object identities and
changeset IDs remain stable. After upgrading, check `/ready`, inspect the audit
trail, and perform a workspace checkout and a restore drill against the snapshot.
