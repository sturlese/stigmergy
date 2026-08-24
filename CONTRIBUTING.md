# Contributing

## Local validation

Requirements are Python 3.12+, `uv`, and Docker.

```bash
make venv
make db-up
make lint
make test
```

The suite is keyless and forces fake model backends. It claims the dedicated `stigmergy_test`
database for the whole run, so run only one suite per database lane at a time. `make db-down`
removes the local test volumes.

Use `make test-system` for the focused Postgres, Git, capture, writer, Slack, bridge, admin, and
reset acceptance paths. CI also builds the deployment image and runs a pinned `gitleaks` scan.

## Project rules

- Read `CLAUDE.md` before changing the system.
- Keep Git and Markdown authoritative for current knowledge.
- Keep source pages immutable outside explicit deletion.
- Route every knowledge mutation through the serialized writer and its gates.
- Apply visibility constraints to writes as well as reads.
- Update code, tests, active documentation, deployment configuration, and the knowledge-repository
  contract together.
- Keep comments and docstrings limited to local non-obvious invariants or mechanics.
- Never use real credentials or paid models in tests.

The target architecture is documented in `docs/ARCHITECTURE.md`; deployment and validation are in
`docs/OPERATIONS.md`; destructive non-production reset is in `docs/RESET.md`.

## Pull requests

Branch from `main`, keep commits focused, and run the full local gates before requesting review.
Report security issues privately as described in `SECURITY.md`.

Contributions are licensed under Apache-2.0.
