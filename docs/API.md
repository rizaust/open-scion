# HTTP API

Open Scion exposes a single-repository HTTP API. The default address is
`http://127.0.0.1:7433`. All routes are explicit; there is no generic RPC,
remote shell, filesystem export, or HTTP administration endpoint.

## Transport and authentication

- JSON requests use `Content-Type: application/json`; blob uploads use
  `Content-Type: application/octet-stream`. JSON responses are UTF-8.
- POST requests require `Content-Length`. Chunked request bodies are rejected.
- Each request body is limited to **16 MiB**, including a raw blob upload.
  URLs are limited to 8,192 characters and 16 query parameters. Duplicate query
  parameters, JSON keys, and unknown argument names are rejected.
- The socket I/O inactivity timeout defaults to 15 seconds. Responses close the
  connection. The server admits up to 32 concurrent connections and 64 MiB of
  aggregate request-body bytes by default; both limits are configurable. Admission
  exhaustion returns `503` with `Retry-After: 1`. The byte limit accounts for raw
  bodies; configure process memory separately for parsing and repository work.
- `SCION_TOKEN` configures the CLI's bearer token. With a configured server token,
  every `/v1` endpoint requires `Authorization: Bearer TOKEN`. `/health` and `/ready` are public.
  A token is mandatory when binding beyond loopback. The token grants full access
  to the repository; it is not a per-user identity or an ACL.
- The server provides HTTP, not TLS. Use an HTTPS reverse proxy or an SSH tunnel
  for remote transport. Token strings must be printable ASCII without whitespace.
- There is no CORS support. Cross-origin requests are rejected; unauthenticated
  loopback requests must use a loopback `Host` with the actual listening port.
- Responses include a server-generated `X-Request-ID`, `X-Content-Type-Options: nosniff`, and normally
  `Cache-Control: no-store`. Successful immutable blob and full-ID changeset
  reads use `private, max-age=31536000, immutable` and an ETag. Blob reads support
  `If-None-Match` with that ETag or `*`, returning `304` without a body.

## Data types

`id` is a 64-character lowercase hexadecimal SHA-256 object identifier. Blob IDs
hash a typed envelope, not just the raw file bytes. `revision` accepts a full
changeset ID or a ref such as `main`. URL-encode revisions and ref names when used
in path segments, for example `imported%2Fmain`.

Paths are relative POSIX paths. Empty components, `.`, `..`, leading slashes,
backslashes, NUL, and `.git`/`.scion` components are rejected. A `prefix` selects
either an exact file or a directory and its descendants; it is not a glob or a
plain string prefix. An empty prefix selects the complete tree.

A **file entry** is:

```json
{"blob": "<blob-id>", "mode": "100644"}
```

Mode is `100644` for a regular file or `100755` for an executable file. Symlinks and
submodules are unsupported. A **changeset** has this shape:

```json
{
  "id": "<changeset-id>",
  "parents": ["<first-parent-id>"],
  "changes": {
    "src/hello.py": {"blob": "<blob-id>", "mode": "100644"},
    "old.txt": null
  },
  "message": "Update greeting",
  "author": "Ada",
  "created_at": "2026-10-14T06:00:00.000000Z",
  "tree": "<root-tree-id>",
  "generation": 1
}
```

Changes are a delta against the first parent. `null` deletes a path. Zero parents
creates a root, one creates an ordinary changeset, and two creates a merge.
The server assigns the timestamp and ID. Root generation is zero. A changeset
never moves a ref by itself.

## Read endpoints

All successful reads return `200`, except a conditional blob read may return
`304`. Parameters in brackets are optional. Listed response arrays and maps are
returned directly, without a `data` wrapper.

| Endpoint | Query parameters | Response |
| --- | --- | --- |
| `GET /health` | None | `{"status":"ok"}` |
| `GET /ready` | None | Repository readiness; `503` when unavailable or draining |
| `GET /v1/metrics` | None | Request, status, active worker, body admission, uptime, and configured limits |
| `GET /v1/ref-log` | `[name]`, `[limit=50]` | Newest-first array of `{sequence, name, old, new, created_at}` |
| `GET /v1/refs` | None | `{"main":"<changeset-id>"}` |
| `GET /v1/stats` | None | Integer counts: `objects`, `blobs`, `trees`, `changesets`, `refs`, `compressed_bytes` |
| `GET /v1/changesets/{revision}` | None | A changeset |
| `GET /v1/blobs/{id}` | None | Raw bytes, `application/octet-stream` |
| `GET /v1/tree` | `rev`, `[prefix=""]` | Map of path to file entry |
| `GET /v1/log` | `rev`, `[limit=50]`, `[path]` | Array of changesets, newest first, following first parents |
| `GET /v1/diff` | `base`, `target`, `[prefix=""]` | Array of diff records |
| `GET /v1/merge-base` | `left`, `right` | `{"base":"<changeset-id>"}` or `{"base":null}` |
| `GET /v1/blame` | `rev`, `path` | Array of `{line, text, changeset, author}` |
| `GET /v1/search` | `rev`, `q`, `[prefix=""]`, `[limit=100]` | Array of `{path, line, text}` |

HTTP `limit` values must be integers from 1 through 1,000. Log `path` filters
changesets touching that file or directory. Search uses a case-sensitive literal
substring, with results ordered by path then one-based line number. Blame follows
first-parent history; lines brought in by a merge are attributed against that
history. Text inspection uses UTF-8 without NUL bytes and has per-file size/line
limits; binary or oversized files are skipped by search and rejected by blame.

A diff record is:

```json
{
  "path": "src/hello.py",
  "status": "modified",
  "old": {"blob": "<old-blob-id>", "mode": "100644"},
  "new": {"blob": "<new-blob-id>", "mode": "100755"},
  "patch": "--- a/src/hello.py\n+++ b/src/hello.py\n...",
  "binary": false,
  "truncated": false
}
```

Status is `added`, `modified`, or `deleted`; the absent side is `null`. Binary or
oversized text has an empty patch and `binary: true`. Patches are limited to 2,000
diff lines; `truncated` reports that limit. Mode-only changes can have an empty
text patch. The stats object count covers compressed blob and tree files;
changesets are counted separately in SQLite. `compressed_bytes` excludes SQLite.

## Write endpoints

All named body fields are required unless marked optional. Unrecognized fields
are rejected. Mutation endpoints do not accept query parameters.

| Endpoint | Body | Success |
| --- | --- | --- |
| `POST /v1/blobs` | Raw bytes; an empty blob is valid | `201`, `{"id":"<blob-id>"}` |
| `POST /v1/changesets` | `{parents, changes, message, author}` | `201`, a changeset |
| `POST /v1/refs/{name}` | `{new, expected}` | `200`, `{name, target}` |
| `POST /v1/merge` | `{base, ours, theirs, message, author}` | `201`, a changeset |
| `POST /v1/rebase` | `{revision, onto, [message], [author]}` | `201`, a changeset |
| `POST /v1/land` | `{revision, ref, expected, [author]}` | `200`, `{changeset, ref: {name, target}}` |

- **Create changeset:** `parents` is an array of up to two distinct revisions.
  `changes` maps paths to file entries or `null`. Referenced blobs must already
  exist. `message` is text and may be empty; `author` must be nonempty text.
- **Update ref:** `new` is the target revision. `expected` must be the exact current
  changeset ID, or `null` to require that the ref does not yet exist. This is an
  atomic compare-and-swap; it does not impose a fast-forward requirement.
- **Merge:** `ours` and `theirs` are revisions. Set `base` to `null` to compute a
  common ancestor, or provide an ancestor of both revisions. Unrelated histories
  use an empty base. The operation combines file changes and disjoint text-line
  edits; unresolved overlaps return a conflict. No ref moves.
- **Rebase:** Replays one changeset's first-parent delta onto `onto`, creating a
  new changeset with a single parent. It does not replay an entire branch.
  Optional `message` and `author` default to the original, including when `null`.
- **Land:** Advances `ref` if `expected` matches. Fast-forwards when possible,
  leaves an already contained revision at the existing tip, and otherwise creates
  a merge. Initial landing uses `expected: null`. Optional `author` defaults to
  `scion`. Concurrent updates and conflicts leave the ref unchanged; an immutable
  object created before a failed final compare-and-swap may remain unreferenced.

## Example: upload, commit, and publish a ref

This example uses `curl` and Python's standard library. Set `SCION_TOKEN` to match
the server when authentication is enabled.

```sh
SCION_URL=http://127.0.0.1:7433

BLOB_ID=$(printf 'hello\n' | curl --fail --silent --show-error \
  -H "Authorization: Bearer ${SCION_TOKEN:-}" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @- "$SCION_URL/v1/blobs" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

CHANGESET_ID=$(curl --fail --silent --show-error \
  -H "Authorization: Bearer ${SCION_TOKEN:-}" \
  -H 'Content-Type: application/json' \
  --data "{\"parents\":[],\"changes\":{\"hello.txt\":{\"blob\":\"$BLOB_ID\",\"mode\":\"100644\"}},\"message\":\"First change\",\"author\":\"Ada\"}" \
  "$SCION_URL/v1/changesets" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl --fail --silent --show-error \
  -H "Authorization: Bearer ${SCION_TOKEN:-}" \
  -H 'Content-Type: application/json' \
  --data "{\"new\":\"$CHANGESET_ID\",\"expected\":null}" \
  "$SCION_URL/v1/refs/main"

curl --fail --silent --show-error \
  -H "Authorization: Bearer ${SCION_TOKEN:-}" \
  "$SCION_URL/v1/tree?rev=main"
```

## Errors

Errors have a stable JSON envelope:

```json
{
  "error": {
    "code": "conflict",
    "message": "Merge conflicts require an explicit resolution changeset",
    "details": {"conflicts": ["src/hello.py"]}
  }
}
```

`details` is `null` when no structured details are available. Stale ref errors
report expected and actual IDs. Clients should inspect the HTTP status and error
code, rather than parse the message.

| HTTP status | Meaning |
| --- | --- |
| `400` | Invalid argument, malformed JSON/URL, unsupported transfer encoding |
| `401` | Missing or invalid bearer token; includes `WWW-Authenticate: Bearer` |
| `403` | Rejected origin or Host |
| `404` | Missing route, revision, path, or object |
| `405` | Unsupported method for a route; includes `Allow` |
| `408` | Request body read timed out |
| `409` | Ref compare-and-swap failure or unresolved merge conflict |
| `411` | Missing `Content-Length` |
| `413` | Request body exceeds the limit |
| `414` | Request URL exceeds the limit |
| `415` | Incorrect or missing request `Content-Type` |
| `500` | Internal error or detected storage corruption |

The API does not provide ref deletion, object deletion, garbage collection,
pagination cursors, resumable uploads, per-user permissions, or rate limiting.
Integrity checks are available locally with `scion fsck --data DIRECTORY`.
