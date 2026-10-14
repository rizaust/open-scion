# Architecture

[English README](../README.md) · [中文说明](../README.zh-CN.md) · [HTTP API](API.md)

Open Scion is a server-first version-control system, independently built from the architectural ideas in Anthropic's published [Scion article](https://www.anthropic.com/engineering/redesigning-source-control). It uses its own storage format and HTTP protocol. A Python service manages each repository through SQLite metadata and immutable local object files.

![Open Scion architecture](assets/architecture.svg)

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `store/repository.py` | Public `Store` facade, repository lifecycle, SQLite connections and additive schema migration |
| `store/objects.py`, `store/trees.py` | Atomic compressed object installation, verified reads, persistent Merkle trees |
| `store/changesets.py`, `store/refs.py` | Immutable changesets, revision resolution, exact ref CAS and transactional audit history |
| `store/integrity.py` | Repository statistics and snapshot-consistent integrity verification |
| `store/backup.py` | Online snapshot creation and verified repository restoration |
| `store/snapshot_files.py`, `store/snapshot_manifest.py` | Durable copying, atomic no-replace publication, manifest and path validation |
| `store/codec.py`, `store/validation.py`, `store/errors.py` | Typed identities, canonical serialization, portable names and domain errors |
| `graph/queries.py`, `graph/cache.py` | Generation-pruned ancestry queries and bounded immutable metadata caching |
| `operations/service.py`, `operations/integration.py`, `operations/inspection.py`, `operations/text.py` | Operation facade, diff, three-way merge, rebase, landing, blame and search |
| `server/application.py`, `server/transport.py` | Configuration, listener lifecycle, worker admission, upload budgets and shutdown draining |
| `server/handler.py`, `server/routing.py`, `server/read_routes.py`, `server/write_routes.py` | Handler composition and explicit endpoint dispatch |
| `server/validation.py`, `server/responses.py`, `server/config.py` | Authentication, request validation, response envelopes and transport defaults |
| `server/readiness.py`, `server/telemetry.py` | Read-only readiness probes, bounded metrics and redacted access events |
| `client/transport.py`, `client/api.py` | HTTP requests, environment token and blob verification |
| `workspace/model.py`, `workspace/locking.py` | Public `Workspace` facade, metadata lifecycle, reentrant and cross-process locks |
| `workspace/scanning.py`, `workspace/inspection.py`, `workspace/cache.py` | Sparse selection, ignore rules, status, diff and verified bounded cache |
| `workspace/mutations.py`, `workspace/transactions.py` | Commits, integration, landing and durable working-copy updates |
| `workspace/journal.py`, `workspace/recovery.py`, `workspace/filesystem.py` | Journal validation, repeatable offline recovery and durable file primitives |
| `cli/parser.py`, `cli/main.py`, `cli/output.py`, `cli/commands/` | Argument parsing, command routing, service/workspace/query handlers and output |
| `importers/git.py` | Topological import of reachable Git commits and file content |

The `store`, `server`, `workspace`, `client`, `operations`, `graph`, and `cli` package entrypoints preserve their public imports. Their facades compose focused modules; storage primitives do not depend on HTTP or workspace code. The storage facade loads backup orchestration lazily, and the HTTP handler's transport type reference is guarded by `TYPE_CHECKING`.

Native commands need Python 3.11 or newer and its standard library. Git is required only by the importer. Packaging uses setuptools as a build-time dependency.

## Object identity and layout

All object IDs are lowercase, 64-character SHA-256 digests. The hash input includes a type and byte length:

```text
<kind> <decimal-payload-length>\0<payload>
```

For a blob, the payload is the original bytes. For a tree or changeset, it is canonical JSON with sorted keys and compact separators. A blob ID is therefore different from the SHA-256 hash of its raw contents, and Scion IDs are not Git IDs.

A server directory contains:

```text
repository/
├── scion.db                 # Changesets, refs, ref audit, and schema metadata
├── scion.db-wal             # SQLite's write-ahead log, while in use
├── scion.db-shm             # SQLite's shared-memory bookkeeping, while in use
└── objects/
    └── ab/
        └── cdef...          # zlib-compressed typed blob or tree envelope
```

Blobs and trees are immutable content-addressed files. Installation writes and flushes a temporary file, persists object-prefix directory entries on systems with directory `fsync`, and links the file to its final identity without replacing an existing object. Existing objects are verified before reuse. Reads check decompression, the typed envelope, payload length, expected type, and digest.

Changesets are canonical records stored in SQLite; their IDs use the same typed hashing scheme. SQL triggers reject updates and deletions of existing changeset rows. Reads validate the record, ID, and indexed tree/generation fields. These checks detect accidental corruption through the supported interfaces. Repository administrators manage access to the database, object directory, and service process.

`scion fsck --data DIRECTORY` opens the repository read-only and checks object hashes, tree edges, changeset references, generations, refs, audit continuity, and SQLite integrity. Verification pins the metadata read snapshot before enumerating objects, then follows referenced tree edges directly. Concurrent object publication cannot create a false missing-child report merely because an inventory scan already visited that child's directory. The check reports errors while retaining stored content. Object statistics count compressed blob/tree files separately from changesets in SQLite.

## Changesets and persistent trees

A changeset records:

- Zero, one, or two parent changeset IDs.
- File changes relative to the first parent: an entry containing a blob ID and mode, or `null` for deletion.
- A root tree ID representing the resulting complete snapshot.
- Message, author, server-assigned UTC timestamp, and graph generation.

Root generation is zero; a later generation is one greater than the maximum parent generation. Regular files use mode `100644`, executable files use `100755`. The workspace and importer validate entries against these two regular-file modes. On Windows, commits preserve the stored executable mode for tracked files and assign `100644` to new files, rather than inferring Unix execute bits from Windows filesystem metadata.

The tree is a persistent directory trie. Each directory object maps immediate names to child trees or file entries. Applying a delta groups changes by path and rebuilds the affected directory nodes. Unchanged subtrees retain their IDs; deleted empty directories disappear.

A small edit reuses the existing tree outside its affected paths. Commit work includes reading and serializing affected directory entries, validating referenced blobs, and storing metadata inside a SQLite write transaction. The number of entries in the affected directories therefore contributes to commit cost.

A sparse client's delta inherits all unseen paths from the parent tree. Absence from the local workspace is not interpreted as deletion outside its selected prefixes.

## Concurrency and refs

Each storage operation opens its own SQLite connection. The database uses WAL mode, a busy timeout, foreign-key checks, and full synchronous writes. Write operations use `BEGIN IMMEDIATE`; SQLite serializes writers within the repository service.

Creating a changeset never moves a ref. Anyone with repository access can address the new changeset by ID immediately after it is stored.

Updating a ref is an exact compare-and-swap:

```text
update_ref(name, new, expected)

current(name) == expected  →  store new target
current(name) != expected  →  fail with a stale-ref error
expected is null          →  the ref must not exist
```

The comparison, ref update, and insertion into `ref_audit` occur in one transaction. Each successful update records a monotonically increasing sequence, ref name, old target, new target, and server-assigned UTC timestamp. A stale comparison records no event; a failed audit insertion rolls back the ref update. SQL triggers make audit rows immutable. `Store.ref_log()` returns newest-first history, optionally filtered by ref name; `/v1/ref-log?name=REF` exposes history for the selected ref. On an existing repository, the additive schema migration starts recording subsequent transitions while retaining its current refs.

The generic ref API accepts any valid target revision after a successful comparison. `land` adds ancestry-aware integration:

1. Verify the caller's expected target.
2. Fast-forward if the current tip is an ancestor of the proposed changeset.
3. Keep the current tip if it already contains the proposed changeset.
4. Otherwise attempt a three-way merge and create a merge changeset.
5. Compare-and-swap the ref again using the original expected value.

A concurrent winner between steps 1 and 5 causes the final update to fail. Immutable objects created during the attempt remain stored, including any that the ref update leaves unreferenced. Existing history is preserved.

By default, the CLI reads the current ref before landing and uses that snapshot as the expectation. `--expected ID` lets callers supply an earlier snapshot; `--expected none` requires ref creation. Callers can run review and test checks before invoking this ref-update operation.

## Merge, rebase, and history

`graph/queries.py` owns ancestry traversal. Merge-base computation uses a generation-ordered frontier and propagates reachability from both tips. Once a common ancestor is found, candidates below its generation can be pruned; the highest-generation common ancestor wins, with lexicographically greatest ID as the deterministic tie-break. Ancestor checks stop following nodes once their generation cannot reach the candidate.

A thread-safe LRU cache retains up to 4,096 immutable `(generation, parents)` records per graph instance. Query frontiers remain local to each request, and refs are resolved afresh before traversal. Recent branch queries can reuse cached metadata and avoid walking older history that cannot improve the answer.

Three-way merging first resolves unchanged or identical entries at file level. Independent mode/content changes can combine. When both sides changed the same text content, the implementation computes line edits against the base and combines disjoint edits; overlapping changes produce explicit conflict paths. Add/add differences, modify/delete conflicts, and file/directory collisions also fail. A conflict response includes the paths to resolve and leaves shared refs and workspace content unchanged.

Binary files are stored without conversion. An identical change or one-sided binary change can merge through file-level rules; conflicting edits to both sides cannot be text-merged. Server text inspection requires valid UTF-8 without NUL bytes, at most 1 MiB, and at most 20,000 lines. Larger text is treated like non-text for those operations. Server diff output caps patches at 2,000 lines and reports truncation.

`rebase` replays **one changeset's first-parent delta** onto a chosen revision. The result is a new changeset whose single parent is the chosen revision. CLI merge and rebase update the workspace to the new changeset but do not advance shared refs.

Log and blame follow first-parent history. Blame compares line sequences as it walks that history; newly introduced lines at a merge are attributed within the first-parent lineage. Search is a case-sensitive literal substring scan of the selected snapshot. These queries compute their results from stored changesets and file contents. Their cost grows with the history traversed or the selected files scanned.

## Working copies and cache

A workspace contains:

```text
workspace/
├── selected-project-files
└── .scion/
    ├── workspace.json       # Server URL, revision, default ref, sparse prefixes
    ├── writer.lock          # Advisory cross-process lock
    ├── transaction.json     # Durable journal while an update is in progress
    ├── update-<id>/         # Staged originals and target files during that update
    │   ├── backup/
    │   └── target/
    └── cache/
        └── <blob-id>        # Verified, uncompressed file contents
```

Workspace metadata records the server, revision, default ref, and selected prefixes. `SCION_TOKEN` is read from the process environment and kept out of that metadata. The client rejects HTTP redirects so credentials stay bound to the configured endpoint.

Sparse prefixes select an exact file or a directory subtree. The client requests only selected tree entries and required blobs. This is eager sparse checkout: all selected files are materialized as ordinary filesystem entries.

The blob cache is bounded to 64 MiB using modification timestamps as an LRU approximation. Cache hits are verified before use; corrupt entries are removed and fetched again. Oversized individual blobs are returned without being cached. Eviction never removes authoritative server data.

Local status reads selected files and compares typed blob IDs plus executable modes with the selected parent tree. The content scan supplies the changes selected for commit. Local `diff` compares working-copy contents; `diff BASE TARGET` runs on the server. A commit snapshots all selected, non-ignored changes and uploads their blobs, with deletions represented explicitly.

### Update protection

Before updating, the workspace checks tracked content and mode. Modified or deleted tracked files cause the update to stop. Untracked files are allowed when they do not collide with paths that the target needs. Ignored files receive the same overwrite protection.

Paths are validated and symlink traversal inside the managed workspace is rejected. Workspace paths use portable names: Windows reserved device names, colons, trailing dots/spaces, and reserved `.git`/`.scion` components are rejected. User-selected parent aliases, such as macOS `/tmp`, are normalized to their real path; a symlink used as the workspace target itself is refused. The workspace also rejects remote trees whose names collide after Unicode normalization and case folding, to prevent ambiguous materialization on common filesystems.

Workspace operations acquire a reentrant thread lock and an advisory operating-system file lock. The process lock serializes writers sharing the workspace, and the operating system releases it when a process exits. Metadata is reloaded while holding the lock so a waiting command sees the previous writer's committed revision.

Required target blobs are fetched and verified before file changes begin. An update copies affected originals and target files into a transaction directory, flushes those files, and publishes a durable journal. The journal records previous and target metadata, before/after file identities, original directory modes, and the `prepared` or `applying` phase. Target files are installed from the staged data; `workspace.json` is atomically saved last. The journal is cleared only after metadata has committed.

Opening a workspace checks for an interrupted transaction while holding its lock. Recovery uses local staged data and does not contact the server. If metadata already names the target, recovery completes journal cleanup; otherwise it verifies the originals and rolls back an applying transaction. Recovery can be repeated after interruption. Changes made outside the transaction are preserved: ownership and content checks report `recovery_required` with the journal, backup, and conflicting paths for inspection. `scion recover` exposes this recovery operation explicitly.

Landing may succeed on the server before the subsequent local update fails, for example because a newly merged path collides with an untracked file. The CLI reports the advanced ref and the landed result separately from the local update error, so the caller can resolve the local collision and retry the update.

## Online backups and verified restoration

`Store.backup(destination)` takes a consistent SQLite snapshot through the SQLite backup API while a read transaction pins the source metadata. Writers can continue using WAL during object copying. The snapshot includes every committed changeset, including unlanded changesets, together with the root trees and blobs referenced by those records. Unattached blob uploads are outside that metadata snapshot.

Each backup directory contains a self-contained `scion.db`, the selected compressed objects, and `manifest.json`. The versioned manifest records SHA-256 digests and byte counts for every copied file, along with object, changeset, ref, and audit-event counts. The staged snapshot passes read-only integrity verification before publication. Files and directory entries are flushed where supported, and the completed backup is installed with an atomic no-replace rename.

`store/backup.py` restores into a new destination. It validates the manifest format, file inventory, relative paths, regular-file types, and checksums before running read-only `fsck` in staging. Content hashes and ref-audit continuity are checked independently of the outer file checksums. Only a fully verified staging directory is atomically published; an existing destination is retained even if it appears during restoration.

Use filesystem permissions and trusted transfer channels to control who can replace a backup or its manifest. The manifest provides content-integrity checks; backup source authenticity is established by the surrounding storage and access controls. The [operations guide](OPERATIONS.md) documents backup and restore commands.

## Git import

The importer reads reachable commits in topological order, imports regular/executable file contents, reconstructs parent relationships, and appends an `Imported-from-git` provenance trailer to each message. It maintains the parent ID mapping internally; its result returns commit/blob counts, the original Git tip, the imported Scion tip, and the resulting ref. Original Git author strings are retained, while Scion timestamps are assigned by the server. Git commands use argument arrays without a shell. Import validates regular-file modes and accepts commits with up to two parents.

Import requires an unused destination ref, writes objects and changesets incrementally, then creates that ref with compare-and-swap protection. Already imported objects remain stored if a later step fails. The workflow provides one-way ingestion from a Git repository into native Scion history.

## HTTP and trust model

The server is a threaded standard-library HTTP service with bounded worker admission. Each route maps to an explicit repository operation. JSON bodies, raw blob bodies, route parameters, field names, and path/ref formats are validated.

Defaults allow 32 active workers, 16 MiB per request body, and 64 MiB of aggregate reserved in-flight body bytes. Admission and body reservation are separate: a request reserves its declared body size before reading it or sending `100 Continue`, and releases that budget when the connection finishes. Saturation returns `503` with `Retry-After`. The 15-second socket timeout bounds periods of I/O inactivity; repository computation and total request duration are separate from this socket limit.

Shutdown stops admission, waits for active connections up to the drain deadline, then closes remaining sockets. `/v1/metrics` exposes fixed-cardinality counters, active workers, reserved upload bytes, configured limits, uptime, and drain state. Access events carry a server-generated request ID, method, query-free path, status, elapsed time, and remote address; request bodies, bearer tokens, and query strings are excluded.

Without a token, binding is restricted to loopback and browser-origin/Host checks protect the local service. When a token is configured, `/v1` requests require bearer authentication. `/health` is a public liveness endpoint; `/ready` is a public, read-only schema probe that also reflects drain state. Readiness opens SQLite in read-only mode and checks the expected metadata, changeset, ref, and audit columns without scanning objects. A nonloopback listener requires a token. Tokens grant access to the entire repository; author strings are client-provided metadata, not authenticated identities.

For remote use, place the HTTP service behind an HTTPS reverse proxy or connect through an SSH tunnel. Configure access at repository scope, protect the server data directory using operating-system permissions, and provide backup and recovery procedures for that directory.

## Validation

The regression suite exercises immutable server-side history, delta-based changesets, persistent trees, independently mutable refs, server-side repository queries, sparse workspaces, and content-verified bounded client caching. It also covers competing writers, explicit merge conflicts, failed downloads, cache corruption, and working-copy preservation.

Run the suite with `python -m unittest discover -s tests -v`. The [runnable demo](../examples/demo.py) exercises the same components through a real HTTP service and two independent agent workspaces.
