# Identities and capabilities

[Operations](OPERATIONS.md) · [Protected refs](POLICIES.md) · [HTTP API](API.md)

The service supports three authentication modes:

| Mode | Authenticated identity | Configuration |
| --- | --- | --- |
| Local loopback | `local` | Neither token nor auth file |
| Shared token | `legacy` | `SCION_TOKEN` |
| Scoped identities | Configured record ID | `--auth-file FILE` |

Local and shared-token identities receive all capabilities and ref scopes. A
scoped configuration contains SHA-256 hashes of high-entropy bearer credentials,
stable identities, explicit capabilities, ref patterns, and revocation state.
The shared-token and auth-file modes are mutually exclusive.

## Generate and configure a credential

```sh
scion auth token --id build-agent \
  --capability read --capability object-write \
  --capability changeset-write --capability ref-update \
  --ref 'agents/*' --json
```

This command prints a freshly generated token and its configuration record. Keep
the token in the caller's secret environment. Add only the `record` object to an
authentication file with this structure:

```json
{
  "version": 1,
  "tokens": []
}
```

The `tokens` array can contain up to 4,096 records. Every record has `id`, `sha256`,
`capabilities`, `refs`, and `revoked` fields. Identities and credential hashes are
unique. Configuration files are limited to 1 MiB.

```sh
scion auth inspect /etc/open-scion/auth.json
unset SCION_TOKEN
scion serve --data /srv/open-scion --auth-file /etc/open-scion/auth.json
```

Set `SCION_TOKEN` to the generated credential in the client environment, then
inspect the identity with `scion identity --server http://127.0.0.1:7433`.

## Authorization matrix

| Endpoint | Required capabilities |
| --- | --- |
| `GET /health`, `GET /ready` | Public probes |
| `GET /v1/identity` | Authenticated identity |
| `GET /v1/metrics` | `admin` |
| Other repository GET endpoints, including ref audit and policy inspection | `read` |
| `POST /v1/blobs` | `object-write` |
| `POST /v1/changesets` | `changeset-write` |
| `POST /v1/merge`, `POST /v1/rebase` | `read`, `changeset-write` |
| `POST /v1/refs/{name}` | `ref-update` and a matching ref scope |
| `POST /v1/land` | `read`, `changeset-write`, `ref-update`, and a matching ref scope |

Capabilities are independent: `admin` grants metrics access, not implicit write
access. `read` grants repository-wide immutable queries. Ref patterns constrain
mutations, not visibility of file paths or blobs. Patterns are case-sensitive;
`*` and `?` have shell-style matching and may match across `/`.

## Rotation and revocation

Replace a record's hash to rotate a credential while retaining its identity. Set
`revoked` to `true` to revoke it. Publish the complete JSON file atomically; the
service reloads a changed file at the next request authentication. In-flight
requests retain the identity snapshot they were authorized with. Invalid or
unavailable configuration returns an authentication-configuration error instead
of falling back to an old credential set.

Ref audit records include the authenticated `actor`, operation, and policy
decision. The changeset `author` remains caller-provided attribution and grants no
permissions. Access logs include authenticated actor IDs without credentials or
credential hashes.
