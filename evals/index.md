# evals — the two instruments

Narrative doc: [`README.md`](README.md) (what each axis means and why). This file is the code map.

## Purpose

Answers *"does the **system** produce the quality we promised"* — as opposed to the unit tests,
which answer *"does the code do what the code says"*.

This directory holds exactly two instruments. Both need a real key and real model judgment, so
both run BY HAND and append to a git-resident series; CI stays keyless and never runs them.

## Key entry points

| Entry | File |
|---|---|
| `make retrieval-golden` → Recall@5 per arm | `run_retrieval.py` (needs `make db-up`. The target passes neither `--rebuild` nor `--repo`, so it scores whatever index is already in the local database — after `make test` that is the suite's fixture index, not the frozen corpus. Pass `RETRIEVAL_ARGS="--rebuild --repo evals/corpus"` to measure the corpus, the way `qa-golden` bakes in) |
| retrieval golden (16 questions) | `retrieval_golden.json` — page-id expectations, 10 carrying `filters.entity` |
| `make qa-golden` → honesty · groundedness · refutation · retry rate · seconds/question | `run_qa.py` (needs `make db-up` + `OPENAI_API_KEY`) |
| QA golden (26 questions) | `qa_golden.json`; ACL-probe identities in `qa_identities.json` |
| `make gates` → one verdict, one exit code | `run_gates.py`; the armed thresholds live in `bars.py` |
| **the frozen reference corpus** | `corpus/` — 38 committed pages + `PROVENANCE.json` + its own `ops/entity-registry.json`; both runners take `--repo evals/corpus`, and for `run_qa.py` that flag is also what gives `Settings` an alias map (without it entity-first resolution is inert for the whole measurement). Guarded keylessly by `tests/evals/test_golden_corpus_fixture.py` |
| durable eval-score series | `eval_history.py` (`append_run`/`read_history`/`resolve_git_sha`/`corpus_provenance`) → `history.ndjson`, appended by a REAL-instrument run only |
| reports (gitignored) | `out/` |

## What each runner does

**`run_retrieval.py`** — loads the golden set through `index.golden.load_golden`, rebuilds the
index from `--repo` when asked, and scores Recall@5 for four arms: `fts`, `vec`, `rrf`, and
`final` (RRF + contract factors — the arm the R@5 ≥ 0.80 bar reads on). Passes `filters=` through
to `search_arms`, which is what lets the entity questions witness the `entity` filter at all —
without it they are structurally blind to the one mechanism they exist to guard, and controlled
sabotage proved it.

**`run_qa.py`** — builds one `AnswerService` per identity in the golden set and drives every
question through the full answering loop. `_score` is deliberately non-literal: numeric
equivalence via `answer/numbers.py`, and `cites` accepts any page in an expected chain.
`_aggregate` reports the three quality axes and their denominators separately, so a change to one
family cannot silently move another, plus retry rate and seconds/question (median/mean/max) —
latency numbers that carry no bar, since a retried ask is a second full agent run that usually
still ends `verified`, leaving the three axes blind to that cost by construction.

**`run_gates.py`** — runs both instruments plus the adversarial suite and judges them against
`bars.py`. Re-runs a failing bar once, and only when it sits within one question's weight of
passing; a larger miss is a regression and fails immediately.

Both golden runners self-pin their own checkout's `src/` on `sys.path`. That is a known trap when
comparing two checkouts: run the OTHER checkout's copy of the script, not this one with a
different PYTHONPATH.

## Gotchas

- **Never run either instrument while anything else is using the docker stack** — they rebuild
  the index, and the stack is single-tenant while a measurement runs.
- **`--llm fake`/`--embedder fake` is a plumbing self-check, not a measurement.** It exercises the
  path keylessly and deliberately does NOT append to `history.ndjson`.
- **The corpus is a fixture.** Editing it invalidates every earlier entry in the series. The two
  known bad expectations in the golden sets (a literal word expected from a free-form answer; a
  second expected page that matches nothing) are recorded in `README.md` and deliberately left in
  place — they distort every entry identically, and comparability is the whole point.
