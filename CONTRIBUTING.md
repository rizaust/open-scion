# Contributing

Open Scion welcomes small, focused changes with reproducible examples. For a new
storage format or protocol change, open an issue describing the use case and
compatibility implications before implementation.

## Development

```sh
git clone https://github.com/rizaust/open-scion.git
cd open-scion
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/demo.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
The runtime and test suite use only Python's standard library. Git is needed for
the import integration tests; those tests skip when Git is unavailable.

## Invariants worth protecting

- A changeset never changes after publication.
- A shared ref moves only when its expected previous value still matches.
- Sparse commits preserve paths the workspace has never materialized.
- Updates refuse to overwrite dirty, untracked, ignored, or symlinked paths.
- Blob bytes are verified against their typed content address at every read.
- Errors cross the HTTP boundary as structured responses without server internals.

Tests should demonstrate behavior across a real persistence, filesystem, or HTTP
boundary when the change affects one. Add regressions for actual bugs, including
the state that must remain intact after a failure. Do not add performance claims
without a reproducible benchmark and an explicit workload.

Keep the English and Chinese READMEs aligned when changing product behavior or
installation steps.

## Scope

The current backend is deliberately single-node. Distributed object backends,
shared caches, graph indexes, and richer review integrations are useful extension
areas. Changes must preserve the distinction between immutable changeset creation
and mutable ref publication.

## Static website

```sh
python -m http.server 8080 --directory site
```

The `site/` directory deploys to GitHub Pages through its dedicated workflow.
Keep all navigation and asset paths compatible with the `/open-scion/` base path.

Contributions are licensed under [Apache-2.0](LICENSE).
