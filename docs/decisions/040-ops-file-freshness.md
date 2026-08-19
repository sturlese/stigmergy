# ADR 040 — the ops files a running process trusts, and the roads that keep them fresh

- **Status**: accepted
- **Date**: 2026-08-19
- **Closes**: issue #79 (ops files served by a running process are trusted more than the pages
  beside them — replay, monotonicity, strictness and three readers)
- **Related**: [ADR 012](./012-hybrid-index.md) (the derived index this cache lives inside),
  issue #74 (the entity registry's deploy-time staleness, fixed by the snapshot this decision
  generalizes), [ADR 016](./016-human-loop-and-entity-governance.md) (the governance that makes
  `ops/` files committed, reviewable statements).
  Narrative: [`docs/reference/hybrid-index.md`](../reference/hybrid-index.md#the-ops-files-ride-along),
  [`docs/reference/server.md`](../reference/server.md).

## Context

The deployed `app` and `slack` process groups hold no checkout of the knowledge repo. Every
`ops/` control file they read — the entity registry, the identity roster, the channel scope map —
was a copy baked into the docker image at deploy time. Issue #74 found the ranking half of what
that means (a minted entity had no name until the next rollout) and fixed it for the registry
with a webhook-refreshed snapshot in the derived index. Issue #79 found the sharper half: an
identity REVOKED after the rollout kept resolving, and a channel scoped after it stayed
unscoped, until the next deploy. Revocation latency was deploy cadence.

The same audit found what the fix must not import: the webhook fetched files at the delivery's
own pushed sha, so a captured-and-replayed delivery could install any HISTORICAL version of a
file — for the identity roster, a reinstated revocation with no commit and no deploy.

## Decision

**D1 — one relpath-keyed cache, one policy column, three files.** `ops_file_snapshot` carries
`ops/entity-registry.json`, `ops/identities.json` and `ops/slack-channels.json` as verbatim TEXT,
keyed by repo-relative path, so a fourth file cannot be given its own subtly different road. The
cache stores bytes and never parses: each file's own reader owns what they mean, and
`server/ops_files.py` states the preference order once — the snapshot wherever the database has
one, the process's own file where it does not.

**D2 — ops files are fetched at the BRANCH ref, never at the pushed sha.** This one line is the
replay and monotonicity defense for every file in the cache: a replayed or delayed delivery
re-fetches what the branch says NOW, so no historical roster is installable through the endpoint
and no out-of-order delivery can regress one. Pages keep the sha fetch — their consistency story
is the delivery's own path list — and get their own defense instead: `webhook_deliveries`
records each APPLIED `X-GitHub-Delivery` id inside the write transaction, so a replayed page
delivery is acknowledged and applied nowhere, while a FAILED delivery never records itself and
GitHub's manual redelivery keeps working. The sha-ancestry check the issue floated is declined
with a reason: the deployed writers hold no checkout to ask git about ancestry, and with D2 both
real writers are always at the tip.

**D3 — reconciliation is a per-file decision, because absence means different things.** The
nightly rebuild writes what the checkout carries. When the checkout LACKS a file: the registry's
snapshot is cleared (a repo before its first mint genuinely has none; readers fall back to their
file), and the two access files' snapshots are KEPT with an error logged — clearing them would
hand every deployed reader back to the roster baked at the last deploy, a revocation silently
undone by a cron. A deployment that genuinely wants "nobody" or "no scoping" pushes an explicit
`{}`: a committed, reviewable statement, the same line the view sweep's registry refusal draws.
An OVERSIZED file leaves the previous snapshot standing on both roads — two writers of one row
must not disagree about the same fault.

**D4 — the readers stay fail-closed, and empty is malformed.** `store.read_ops_file` answers
`None` for "no snapshot" (fall back to the file) and `""` for a real empty snapshot — and every
access reader treats `""` as malformed JSON: it resolves NOBODY, never everybody, and never falls
through to the baked file. The startup seam only CREATES the table (two process groups run it
concurrently on a rolling deploy); the rebuild road's `init_schema` — single-process, the store's
documented upgrade path — is where issue #74's single-purpose `entity_registry_snapshot` is
retired.

**D5 — the two registry parsers stay two, with authority split.** `kernel.registry` (strict — a
nameless entity is refused) is authoritative for what a VALID registry IS: every writer runs it,
so nothing this system produces is ever the degraded case. `server/entity_aliases` (tolerant) is
authoritative for SERVING: on the hot path, one broken hand-edited record costs that record's
name, not every identity's `describe_entity`. The lint refusing a registry the server serves
happily is therefore the lint working — it reports the substrate while the service degrades.

**D6 — what is deliberately NOT here.** `ops/stewards.json` stays out of the cache: its deployed
reader answers an AUTHORIZATION question per decision and prefers a live `origin/main` read
wherever a checkout exists; its deployed staleness (a steward revocation takes a redeploy on the
checkout-less `app` group) is recorded on issue #88's comment as its own pending decision, not
smuggled in here. And `_expansion_terms` — the one place registry CONTENT set a per-request cost
— is bounded by count and term length, because a registry is a file somebody edits.

## Consequences

An identity or scoping edit lands within seconds of its push, on process groups that hold no
checkout; the console's Index panel answers "which copy, how fresh, from which sha" for each
file. Whoever can push a signed webhook delivery can only ever refresh the cache to what the
branch already says. The trade: identity resolution now reads Postgres on the request path where
it read a local file — the same per-request cost the registry already paid, on the same one
small machine — and a local `stigmergy-server --repo` pointed at a database that carries
snapshots answers from them, not from a working-tree edit (the local-development inversion
`server.md` documents for the registry, now true of all three files).
