# ADR 018 — pilot readiness: freshness, the read site, honest refusals, the instrument

Status: accepted. Narrative: [`docs/reference/operator-runbook.md`](../reference/operator-runbook.md),
[`docs/reference/hybrid-index.md`](../reference/hybrid-index.md), [`docs/reference/server.md`](../reference/server.md),
[`docs/reference/capture.md`](../reference/capture.md).

## Context

The Slack transport made the brain reachable by people who will never open a terminal. This record
closes the gap between what the system promises such a reader and what it actually delivers —
freshness, a citation a non-technical reader can open, a retention promise that runs on its own,
refusal prose that describes the run rather than the corpus, and the instrument a pilot is judged
with.

## Decisions

**D1 — incremental index upsert reuses the full rebuild's own parser and store primitives, never
a second implementation.** `index.corpus.page_row` (made public) is the SAME function
`load_pages`'s directory walk calls per file; `index.store.upsert_pages`/`delete_pages` sit beside
`insert_pages`, same row shape, same `_TSV_SQL`. Two code paths writing `pages_index` is a real
drift risk; reusing both closes it structurally rather than by
discipline alone.

**D2 — the webhook is the one declared, narrow exception to "the server never imports the
librarian".** `server/webhook.py` reaches `librarian.githubapp`'s App-credential primitives (the
SAME credential the librarian worker already reads from this process's environment — an accepted
residual of running both in one image) rather than reimplementing JWT/installation-token minting a
second time.
Symbol-scoped and pinned by `tests/test_architecture.py`, mirroring the ACL-adapter and
entities-boundary exceptions this repo already carries.

**D3 — the withheld excerpt closes when the LIBRARIAN HAS LOOKED, not at a terminal state.**
`queued`/`claimed` withhold with a
sentence that says so plainly and distinctly from the secret/PII refusal's own sentence — reusing
the exact same wording for two different reasons was the largest usability defect this surface
carried. `failed` stays withheld too, as an accepted residual (a run that failed
before the gate leaves genuinely unscanned material, and — unlike `queued`/`claimed` — nothing
automatic will look at it again), with its own third sentence rather than the pending one's false
promise of reappearing.

**D4 — a secret/PII rejection purges its payload immediately, not on the ordinary 30-day window.**
The one rejection reason for which 30 days was always the wrong clock: `librarian.worker._finish`
calls `capture.retention.purge_secret_capture_immediately` right after the row lands `rejected`
with that reason code, from the SAME seam every other terminal outcome already goes through.

**D5 — a refusal's shipped prose is composed by the server, from structured facts, never by the
model.** `answer.service.run_facts_reason` is the one composer for all five refusal shapes, built
from `ctx.searched` (queries tried) and `ctx.read_paths_order`/`out.citations` (pages surfaced) —
facts the ACL-scoped tools already populate. `AnswerOutput.reason` is DROPPED from the schema
entirely rather than merely unread, closing the false-explanation defect
architecturally: there is no longer a field on the model's output a steered agent could fill with
persuasive-but-unverified prose for a future edit to "helpfully" reconnect.

**D6 — entity-first resolution is registry aliases only, hard-scoped, with an unconditional
fallback.** A question's registered alias/name resolves to its entity
(`server.entity_aliases`, a plain-file reader — packages talk through files, so nothing above the
service imports the entity subsystem that writes the registry) and the search runs
`filters={"entity": id}` first; an empty scoped result
falls back to the ordinary unscoped search, so the change can only ever recover hits, never lose
any. No query expansion into the lexical arm: that is a ranking change, and ranking changes are
arbitrated by the golden set, not by inspection.

The resolution shipped inside `AnswerBrain.search_text`, which made `ask`'s own search tool the
only client that had it. It has since moved DOWN into `BrainService._search`
([ADR 022](./022-entity-navigation.md) D4), where every client gets it and `search_text` is a thin
renderer again.

*Amended 2026-08-05:* the SEMANTICS above are no longer current either. Hard-scoping
with a zero-hit fallback eclipsed rather than layered — any hits at all for the resolved entity
meant the blended ranking never ran, so a company-wide page (`entity: []`) was unreachable through
every query naming a registered company. Resolution now feeds the rank-time boost and the lexical
alias expansion instead: one blended search, in which resolving an entity may change the ORDER of
the results and never their membership. The ruling is
[ADR 022](./022-entity-navigation.md) D4's own amendment; the sentence this note replaces claimed
the semantics were unchanged, which they now are not.

**D7 — the read site's ACL rule was presence-excludes, and Quartz was fetched fresh, never
vendored.** A page carrying an `acl` key at all was excluded, whatever the value — a filter over
known labels rots the day a new one is invented. Quartz has no installable npm package (it is used
by cloning its own repository and configuring it in place), so nothing here ever vendored its
TypeScript source into this Python repo; the build script and its workflow both cloned it fresh.
**The read site was later deleted whole** — zero readers, never deployed — and with it the
presence-excludes predicate, whose strictness turned out to be the wrong default anyway; see
[ADR 022](./022-entity-navigation.md) D9 for the argument and for what a rebuild would have to
decide on purpose.

**D8 — `audit_log.result` is a per-tool outcome SUMMARY, never a transcript, and the test that
pins it is written as a negative assertion for that reason.** `{refused, suppressed, verdict,
citations, retried}` for `ask` (citations as PATHS, never quotes, to keep a drafted answer's own
quoted prose out of a log
table), `{hits}` for `search_brain`. Both ride the exact `_call`/`call_async` seam every row
already goes through, via an optional `summarize` callback — additive, not a new write path.

**D9 — `capture.latency` relocates from `stigmergy.librarian`.** `stigmergy-pilot-report` needed the
same percentile/rendering logic from `stigmergy.server`, which may not import `stigmergy.librarian`;
the module was already pure and already depended only on `capture`, so it moved to the layer both
callers can reach rather than being duplicated at each.

## Known limits

- **The evidence bucket's lifecycle policy** and **a stranger reading the operator guides
  unassisted** are manual steps, tracked in the operator runbook's checkpoint checklist rather than
  enforced by anything here — this record does not claim either is done.
- **The real (OpenAI-embedder) golden R@5/QA measurement** needs `OPENAI_API_KEY` and a built
  reference corpus, so it does not run keylessly and never gates CI. D6's change cannot move it in
  either direction: retrieval-golden's own code path (`index.search.search_arms`) is untouched by
  entity-first resolution, which sits above it.
- **Concurrency is proven locally, not under load.** The genuinely-concurrent exactly-once
  claim/finish test runs against a real Postgres here; re-measuring a multi-capture burst against
  two REAL deployed workers needs a deployment, and no test in this repo substitutes for it.
