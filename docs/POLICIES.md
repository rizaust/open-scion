# Protected refs

[Project overview](../README.md) · [Architecture](ARCHITECTURE.md) · [HTTP API](API.md)

Protected-ref policies define who may advance shared refs and which history transitions are accepted. They complement authentication and capability scopes: a caller must satisfy both its token permissions and the first matching policy rule.

The service loads a validated policy snapshot at startup. Ref updates evaluate that snapshot inside the same SQLite transaction that performs compare-and-swap and appends the ref audit record. Policies use the authenticated principal ID supplied by the server, independently of changeset author text.

## Configure a policy

Save this configuration as `policy.json`:

```json
{
  "version": 1,
  "rules": [
    {
      "id": "main-integration",
      "pattern": "main",
      "writers": ["integration-bot"],
      "fast_forward_only": true,
      "allow_direct": false,
      "allow_merge": true
    },
    {
      "id": "release-linear",
      "pattern": "release/*",
      "writers": ["release-bot"],
      "fast_forward_only": true,
      "allow_direct": false,
      "allow_merge": false
    },
    {
      "id": "team-work",
      "pattern": "team/*",
      "writers": ["ada", "ci-worker"],
      "fast_forward_only": true,
      "allow_direct": true,
      "allow_merge": true
    }
  ]
}
```

The `main` rule lets `integration-bot` land work while preserving the existing tip in the resulting history. The service can combine divergent changes into a merge. The release rule gives `release-bot` a linear landing workflow. Team refs accept direct compare-and-swap updates from either listed principal.

Start the repository service with the policy file:

```sh
scion serve --data ./repository --ref-policy ./policy.json
```

Configure authentication so token principal IDs match the entries in `writers`. The built-in `local` identity is used for local access, and `legacy` identifies legacy shared-token access; those identities can also be listed explicitly when that deployment mode is intended.

Policy files are read into immutable rule objects. Editing the file leaves the running snapshot unchanged; restart the service to load a new version. Startup rejects unknown fields, duplicate JSON keys, duplicate rule IDs or patterns, malformed patterns, repeated writer IDs, and non-boolean flag values. Files use UTF-8 JSON and have a 1 MiB size limit.

## Fields and defaults

| Field | Default | Meaning |
| --- | --- | --- |
| `version` | Required | Configuration format version; currently `1`. |
| `rules` | Required | Ordered array of rules; an empty array leaves refs governed by the existing authentication and compare-and-swap behavior. |
| `id` | Required | Stable identifier included in policy decisions and audit metadata. |
| `pattern` | Required | Case-sensitive selector for normalized ref names. |
| `writers` | `[]` | Exact principal IDs allowed to update the matching ref. An empty list denies every writer. |
| `fast_forward_only` | `true` | Require the current tip to be an ancestor of the proposed target. Creation of a new ref is permitted. |
| `allow_direct` | `false` | Permit direct ref-update operations, including the HTTP ref-update endpoint, when set to `true`. |
| `allow_merge` | `true` | Permit newly introduced merge changesets in the ref's reachable history. |

Rule IDs and writer IDs are case-sensitive ASCII identifiers of 1 to 128 characters, beginning with a letter or digit. Writer IDs identify individual principals; `*` is not a writer wildcard. All checks are cumulative: a listed writer still needs the appropriate capability, ref scope, operation permission, ancestry condition, and expected ref value.

## Ref matching

Names are validated using the repository's ref-name rules before matching. Patterns support literal ref characters plus `*` and `?`:

- `main` selects exactly `main`.
- `release/*` selects refs beginning with `release/`, including nested names such as `release/2026/stable`.
- `team/?/main` selects a one-character component between `team/` and `/main`.
- `*` selects every valid ref name.

Matching is case-sensitive. Wildcards follow shell-style string matching: `*` matches zero or more characters and `?` matches one character; both can match `/`. Bracket character classes and backslashes are rejected by configuration validation.

**The first matching rule wins.** Place specific rules before broad patterns. Later matching rules are not combined with the first. When no rule matches, existing authentication, capability scopes, and compare-and-swap behavior remain in effect.

To apply a repository-wide default after more specific rules, append a final rule:

```json
{
  "id": "explicit-writers-only",
  "pattern": "*",
  "writers": []
}
```

This makes the earlier rules the explicit list of permitted writers for protected refs.

## Direct updates, landing, and merge history

The storage layer labels each ref mutation using trusted server-side operation context:

| Operation | When it is used | Policy behavior |
| --- | --- | --- |
| `ref-update` | A direct `update_ref` call or the HTTP ref-update endpoint | Requires `allow_direct: true`. |
| `land` | Initial landing, fast-forward landing, or a revision already contained by the current tip | Checks the writer, ancestry rule, and resulting history. |
| `land-merge` | Landing divergent histories by creating a merge | Also requires `allow_merge: true`. |

Clients do not select these labels in request JSON. `Operations.land` chooses the label from the branch of the integration algorithm that actually ran. Unknown labels are rejected for matching protected refs.

`allow_merge: false` applies to the history transition, not only the endpoint name. It rejects a direct update to a new two-parent changeset and a fast-forward to a single-parent descendant that would introduce an earlier merge. Existing merge history already reachable from the current ref can remain in place while later linear changes are landed. Creating a new ref applies this check to the selected target's ancestry.

Fast-forward policy is checked with the commit graph: `current` must be an ancestor of `target`, with an unchanged target also satisfying the condition. A merge produced by landing normally satisfies fast-forward protection because it contains the previous ref tip. Setting `fast_forward_only: false` permits other ancestry transitions when the operation is otherwise authorized; it does not change the landing algorithm itself.

## Actor and author

A changeset's `author` describes authorship. A ref update's `actor` identifies the authenticated principal that requested the mutation. Changing `author`, adding a principal name to a commit message, or sending an extra JSON field does not grant writer permission.

For example, a commit authored by `Ada` can be landed by the authenticated `integration-bot`. Policy checks the latter against `writers`, and the ref audit records the trusted actor, actual operation label, and successful policy decision.

Direct Python integrations use the same storage hook:

```python
from open_scion.security import RefPolicy
from open_scion.store import Store

store = Store("repository")
store.ref_policy = RefPolicy.from_file("policy.json")

with store.actor_scope("ci-worker", operation="ref-update"):
    store.update_ref("team/build", new=target_id, expected=previous_id)
```

`actor_scope` is an in-process integration API for trusted service code. The HTTP layer derives that context from authentication rather than a user-supplied author field. Policy installation applies to storage-level ref updates, so alternative HTTP handlers still pass through the same authorization decision.

## Inspect decisions

The read-only policy view reports the effective rule for a ref:

```sh
curl -H "Authorization: Bearer $SCION_TOKEN" \
  'http://127.0.0.1:7433/v1/ref-policy?name=main'
```

```json
{
  "ref": "main",
  "matched": true,
  "rule": {
    "id": "main-integration",
    "pattern": "main",
    "writers": ["integration-bot"],
    "fast_forward_only": true,
    "allow_direct": false,
    "allow_merge": true
  }
}
```

An unmatched ref has `matched: false` and `rule: null`. The view describes the configured rule; an attempted mutation additionally evaluates the authenticated actor and current commit graph. In Python, use `policy.describe("main")` for the same detached description.

A permitted mutation produces policy audit metadata such as:

```json
{"rule": "main-integration", "decision": "allow"}
```

An unmatched mutation uses `{"rule": null, "decision": "allow"}`. A policy rejection returns HTTP `403` with a structured error:

```json
{
  "error": {
    "code": "policy_denied",
    "message": "Ref policy 'main-integration' denied update to 'main': writer_not_allowed",
    "details": {
      "rule": "main-integration",
      "ref": "main",
      "reason": "writer_not_allowed"
    }
  }
}
```

Reasons are `writer_not_allowed`, `direct_update_disabled`, `merge_disabled`, `non_fast_forward`, and `unsupported_operation`. A stale expected target remains a compare-and-swap conflict (`409`). Rejected mutations leave the ref and its committed transition audit unchanged.
