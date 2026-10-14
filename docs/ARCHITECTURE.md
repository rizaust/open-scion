# Architecture

[English README](../README.md) · [中文说明](../README.zh-CN.md) · [HTTP API](API.md)

Open Scion implements a small, server-first version-control system. It takes architectural direction from Anthropic's published [Scion article](https://www.anthropic.com/engineering/redesigning-source-control), but its storage format, protocol, code, and operational scope are independent. The implementation described here is a single-node Python service backed by SQLite and local object files.

![Open Scion architecture](assets/architecture.svg)

## Responsibilities

| Component | Owns | Does not own |
| --- | --- | --- |
| `store.py` | Object identities, immutable blobs and trees, changesets, refs, storage validation | Working-copy files or network transport |
| `operations.py` | Graph traversal, diff, three-way merge, one-changeset rebase, landing, blame, search | Per-user permissions or a review workflow |
| `server.py` | Explicit HTTP routes, input validation, authentication, transport limits | Remote command execution or filesystem export |
| `client.py` | HTTP requests, environment token, blob verification | Repository history or Git protocols |
| `workspace.py` | Sparse materialization, local changes, bounded cache, update protection | An authoritative local repository |
| `cli.py` | Commands, human-readable output, JSON output, exit status | A separate object format |
| `git_import.py` | Topological import of reachable Git commits and file content | Bidirectional Git synchronization |

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
├── scion.db                 # Changesets, refs, and schema metadata
├── scion.db-wal             # SQLite's write-ahead log, while in use
├── scion.db-shm             # SQLite's shared-memory bookkeeping, while in use
└── objects/
    └── ab/
        └── cdef...          # zlib-compressed typed blob or tree envelope
```

Blobs and trees are immutable content-addressed files. Installation writes a temporary file, flushes it, and links it to its final identity without replacing an existing object. Existing objects are verified before reuse. Reads check decompression, the typed envelope, payload length, expected type, and digest.

Changesets are canonical records stored in SQLite; their IDs use the same typed hashing scheme. SQL triggers reject updates and deletions of existing changeset rows. Reads validate the record, ID, and indexed tree/generation fields. These controls detect accidental corruption through the supported interfaces; they are not a security boundary against an administrator editing the database or program.

`scion fsck --data DIRECTORY` checks stored objects, changeset references, tree consistency, and refs. It reports errors and does not repair or delete content. Object statistics count compressed blob/tree files separately from changesets in SQLite.

## Changesets and persistent trees

A changeset records:

- Zero, one, or two parent changeset IDs.
- File changes relative to the first parent: an entry containing a blob ID and mode, or `null` for deletion.
- A root tree ID representing the resulting complete snapshot.
- Message, author, server-assigned UTC timestamp, and graph generation.

Root generation is zero; a later generation is one greater than the maximum parent generation. Regular files use mode `100644`, executable files use `100755`. Symlinks, submodules, and other special files are outside the format supported by the workspace and importer. On Windows, commits preserve the stored executable mode for tracked files and assign `100644` to new files, rather than inferring Unix execute bits from Windows filesystem metadata.

The tree is a persistent directory trie. Each directory object maps immediate names to child trees or file entries. Applying a delta groups changes by path and rebuilds the affected directory nodes. Unchanged subtrees retain their IDs; deleted empty directories disappear.

This avoids rebuilding and storing an entire repository tree for a small edit. It is not a constant-time commit claim: affected directories must still be read and serialized, and a large directory can have many entries. Changeset construction also validates referenced blobs, and metadata writes run inside a SQLite write transaction.

A sparse client's delta inherits all unseen paths from the parent tree. Absence from the local workspace is not interpreted as deletion outside its selected prefixes.

## Concurrency and refs

Each storage operation opens its own SQLite connection. The database uses WAL mode, a busy timeout, foreign-key checks, and full synchronous writes. Write operations use `BEGIN IMMEDIATE`; SQLite serializes writers. This implementation is **not lock-free**, and WAL mode does not turn it into a distributed database.

Creating a changeset never moves a ref. Anyone with repository access can address the new changeset by ID immediately after it is stored.

Updating a ref is an exact compare-and-swap:

```text
update_ref(name, new, expected)

current(name) == expected  →  store new target
current(name) != expected  →  fail with a stale-ref error
expected is null          →  the ref must not exist
```

The check and update occur in one transaction. The generic ref API does not require fast-forward ancestry. `land` provides the higher-level integration policy:

1. Verify the caller's expected target.
2. Fast-forward if the current tip is an ancestor of the proposed changeset.
3. Keep the current tip if it already contains the proposed changeset.
4. Otherwise attempt a three-way merge and create a merge changeset.
5. Compare-and-swap the ref again using the original expected value.

A concurrent winner between steps 1 and 5 causes the final update to fail. An immutable object created during the attempt can remain unreferenced; history is not overwritten. There is no garbage collector yet.

By default, the CLI reads the current ref before landing and uses that snapshot as the expectation. `--expected ID` lets callers supply an earlier snapshot; `--expected none` requires ref creation. This is concurrency protection, not code review or test approval.

## Merge, rebase, and history

Merge-base computation traverses parent graphs and chooses a common ancestor by highest generation, breaking ties by ID. It does not construct recursive virtual merge bases for complex criss-cross histories.

Three-way merging first resolves unchanged or identical entries at file level. Independent mode/content changes can combine. When both sides changed the same text content, the implementation computes line edits against the base and combines disjoint edits; overlapping changes produce explicit conflict paths. Add/add differences, modify/delete conflicts, and file/directory collisions also fail. No ref moves on a merge conflict, and no conflict markers are written into a workspace.

Binary files are stored without conversion. An identical change or one-sided binary change can merge through file-level rules; conflicting edits to both sides cannot be text-merged. Server text inspection requires valid UTF-8 without NUL bytes, at most 1 MiB, and at most 20,000 lines. Larger text is treated like non-text for those operations. Server diff output caps patches at 2,000 lines and reports truncation.

`rebase` replays **one changeset's first-parent delta** onto a chosen revision. It creates a new changeset with one parent; it does not walk and replay an entire branch. CLI merge and rebase update the workspace to the new changeset but do not advance shared refs.

Log and blame follow first-parent history. Blame compares line sequences as it walks that history; newly introduced lines at a merge are attributed within the first-parent lineage. Search is a case-sensitive literal substring scan of the selected snapshot. None of these queries uses a persistent derived-data index or rename tracking. Their current cost can grow with the history or selected files.

## Working copies and cache

A workspace contains:

```text
workspace/
├── selected-project-files
└── .scion/
    ├── workspace.json       # Server URL, revision, default ref, sparse prefixes
    └── cache/
        └── <blob-id>        # Verified, uncompressed file contents
```

The workspace stores no token and no local changeset graph. `SCION_TOKEN` is read from the process environment. The client does not follow HTTP redirects, preventing credentials from being forwarded through a redirect to another endpoint.

Sparse prefixes select an exact file or a directory subtree. The client requests only selected tree entries and required blobs. This is eager sparse checkout: all selected files are materialized. There is no filesystem virtualization or on-demand FUSE layer.

The blob cache is bounded to 64 MiB using modification timestamps as an LRU approximation. Cache hits are verified before use; corrupt entries are removed and fetched again. Oversized individual blobs are returned without being cached. Eviction never removes authoritative server data.

Local status reads selected files and compares typed blob IDs plus executable modes with the selected parent tree. It does not use a filesystem watcher, stat-only shortcut, or staging index. Local `diff` compares working-copy contents; `diff BASE TARGET` runs on the server. A commit snapshots all selected, non-ignored changes and uploads their blobs, with deletions represented explicitly.

### Update protection

Before updating, the workspace checks tracked content and mode. Modified or deleted tracked files cause the update to stop. Untracked files are allowed when they do not collide with paths that the target needs. Ignored files receive the same overwrite protection.

Paths are validated and symlink traversal inside the managed workspace is rejected. Workspace paths use portable names: Windows reserved device names, colons, trailing dots/spaces, and reserved `.git`/`.scion` components are rejected. User-selected parent aliases, such as macOS `/tmp`, are normalized to their real path; a symlink used as the workspace target itself is refused. The workspace also rejects remote trees whose names collide after Unicode normalization and case folding, to prevent ambiguous materialization on common filesystems.

Required target blobs are fetched and verified before file changes begin. An update moves affected tracked files to a temporary backup area, writes the target files, and saves metadata last. Ordinary exceptions trigger rollback, and backups are retained if restoration itself fails. This is not a durable filesystem transaction: process crashes, power loss, external filesystem mutation, or concurrent writers can interrupt recovery. Use one writer per workspace and keep independent work in separate workspaces.

Landing may succeed on the server before the subsequent local update fails, for example because a newly merged path collides with an untracked file. The CLI reports that the ref already advanced and includes the landed result; it does not pretend the remote mutation was rolled back.

## Git import

The importer reads reachable commits in topological order, imports regular/executable file contents, reconstructs parent relationships, and appends an `Imported-from-git` provenance trailer to each message. It maintains the parent ID mapping internally; its result returns commit/blob counts, the original Git tip, the imported Scion tip, and the resulting ref. Original Git author strings are retained, while Scion timestamps are assigned by the server. Git commands use argument arrays without a shell. Symlinks and gitlinks are rejected. Merge commits with more parents than the Scion format supports cannot be represented.

Import requires an unused destination ref, writes objects and changesets incrementally, then creates that ref with compare-and-swap protection. A failed import can leave unreferenced imported objects. It is a one-way ingestion path, not a Git remote helper or a bidirectional compatibility layer.

## HTTP and trust model

The server is a threaded standard-library HTTP service with explicit routes. It accepts no generic method invocation and cannot execute a command requested through the API. JSON bodies, raw blob bodies, route parameters, field names, and path/ref formats are validated. Request bodies are limited to 16 MiB; the API reference documents the remaining transport bounds and error responses.

Without a token, binding is restricted to loopback and browser-origin/Host checks protect the local service. When a token is configured, `/v1` requests require bearer authentication; `/health` remains public. A nonloopback listener requires a token. Tokens grant access to the entire repository; author strings are client-provided metadata, not authenticated identities.

The service provides HTTP, not TLS. Remote use requires a separately configured HTTPS proxy or SSH tunnel. There are no per-user roles, directory ACLs, quotas, request-rate controls, distributed replication, automatic failover, or encrypted-at-rest storage in this implementation.

## What this architecture establishes

The implementation demonstrates immutable server-side history, delta-based changesets, persistent trees, independently mutable refs, server-side repository queries, sparse workspaces, and content-verified bounded client caching. Tests exercise these semantics, including competing writers and working-copy preservation.

It does not establish fleet-scale throughput, high availability, production durability under faults, or equivalence to Anthropic's internal deployment. Reaching those goals would require explicit storage, indexing, authorization, recovery, and operational work beyond this release.
