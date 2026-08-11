# evals — the three instruments

Narrative doc: [`README.md`](README.md) (what each axis means and why). This file is the code map.

## Purpose

Answers *"does the **system** produce the quality we promised"* — as opposed to the unit tests,
which answer *"does the code do what the code says"*.

This directory holds three instruments, one per model surface the system has: **retrieval**,
**the answer**, and **filing**. All three need a real key and real model judgment, so all three
run BY HAND and append to a git-resident series; CI stays keyless and never runs them.

Two read, one WRITES. That is the line worth holding when adding a fourth: the filing golden
drives the ONE writer of the knowledge repo through a real `git worktree` and the eight gates,
which makes it the most expensive of the three and the only one whose fixture is also an *input*
to what it measures.

## Key entry points

| Entry | File |
|---|---|
| `make retrieval-golden` → Recall@5 per arm | `run_retrieval.py` (needs `make db-up`. The target passes neither `--rebuild` nor `--repo`, so it scores whatever index is already in the local database — after `make test` that is the suite's fixture index, not the frozen corpus. Pass `RETRIEVAL_ARGS="--rebuild --repo evals/corpus"` to measure the corpus, the way `qa-golden` bakes in) |
| retrieval golden (16 questions) | `retrieval_golden.json` — page-id expectations, 10 carrying `filters.entity` |
| `make qa-golden` → honesty · groundedness · refutation · retry rate · seconds/question | `run_qa.py` (needs `make db-up` + `OPENAI_API_KEY`) |
| QA golden (26 questions) | `qa_golden.json`; ACL-probe identities in `qa_identities.json` |
| `make filing-golden` → nine quality facets, each with its own denominator | `run_filing.py` (needs `make db-up`, `gitleaks` on PATH, and a Claude credential for the default `sdk` backend — `--backend pydantic` authenticates with its OWN provider key instead, never a Claude one. `BACKEND=double` is the keyless plumbing self-check; `--kinds` measures one kind of capture only) |
| filing golden (10 captures, 12 scored phases) | `filing/captures/manifest.json` (what is submitted) + `filing/expected/expectations.json` (the yardstick), kept apart on purpose |
| `make gates` → one verdict, one exit code | `run_gates.py`; the armed thresholds live in `bars.py`. It arms the first TWO instruments only — the filing golden is far more expensive and is not wired in |
| **the frozen reference corpus** | `corpus/` — 38 committed pages + `PROVENANCE.json` + its own `ops/entity-registry.json`; the first two runners take `--repo evals/corpus`, and for `run_qa.py` that flag is also what gives `Settings` an alias map (without it entity-first resolution is inert for the whole measurement). Guarded keylessly by `tests/evals/test_golden_corpus_fixture.py` |
| **the frozen mini knowledge repo** | `filing/repo/` — 5 pages, 3 invented organizations, its own `ops/` and its own `PROVENANCE.json`, plus byte-for-byte frozen copies of the knowledge repo's contract linter and both agent briefs (each with a `FROZEN.md`). Frozen, not drift-guarded — the two guards are different things and only one is refused: see the gotcha below. The librarian brief was RE-FROZEN for [ADR 033](../docs/decisions/033-structured-filing-flow.md), so the series' comparable history starts again at the baseline recorded under it |
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

**`run_filing.py`** — submits each golden capture through `capture.queue.submit` and drains it with
`worker.process_next`, one at a time, against a throwaway bare-remote-plus-clone seeded from
`filing/repo/` (`tests.librarian.support.build_repo(source=…)`, imported rather than rewritten).
Every facet is read back **out of git at the sha in `result_ref`** — the page that actually
landed, never the agent's account of it — and the anchor comes from the page's server-stamped
`entity:`, so it is a resolved registry id rather than the report's rendered prose.

Two seams carry the whole design. `processing.Deps.agent` is injected, so `CountingAgent` counts
the passes one capture spent without any production report having to carry the number — which is
why this instrument needed no production change. And everything below the module's
pure-scoring banner is a function of data: `score_phase`/`aggregate`/`render` import nothing
heavier than the standard library, so a keyless test scores canned outcomes through exactly the
code a real run uses.

A parking capture yields TWO scored phases — the park, and the re-file after its stored reply
travels back through `BrainService.reply` (the real answer channel, the same object
`tests/librarian/test_human_loop_pg.py` drives). A backend that never parks does not lose the
second phase; it scores it as a miss, because a vanishing phase would shrink its facets'
denominators and quietly reward not asking.

**`--kinds` measures a SUBSET, and everything downstream is recomputed from it.** With a filter,
`_check_set` derives the per-facet denominators from the subset instead of holding them against
`EXPECTED_DENOMINATORS` — that pin describes the whole shipped set and only it — and refuses a
subset that scores no facet at all; the report's caption and the history row both record the kinds
measured, so a three-phase meeting-only score can never be read later as the twelve-phase set's.
**`--backend pydantic` used to REQUIRE `--kinds meeting`**, because that backend served the meeting
flow only and would have refused every ordinary capture — a column of refusals is not a measurement.
[ADR 033](../docs/decisions/033-structured-filing-flow.md) gave it the ordinary flow too, so it now
runs the whole set and the guard that enforced the pairing is gone with the limitation. What that
run measures is a different FLOW on the same captures — a deterministic gatherer, a tool-less call,
code writing the page — which is why it is worth running on the SAME model as the `sdk` baseline:
that isolates the flow change from a model change. Every real measurement appends to the history
series; only `--backend double` never does.

Three things it does before it measures anything, all in `_run` and all unconditional: it deletes
the librarian App's five environment variables and pins `$CLEAN_LLM` to the fake backend (`make`
exports the operator's env file, and this is the only make target that reaches
`processing._file`); it runs the librarian's own `worker.startup_checks`; and `_check_set` refuses
a set whose halves have drifted, whose expectations name a facet the scorer does not know, or
whose per-facet denominators are no longer `EXPECTED_DENOMINATORS`. A run that ends with a `failed`
phase, or with no agent pass anywhere, prints why and appends **no** row to the series.

**`run_gates.py`** — runs the first TWO instruments plus the adversarial suite and judges them
against `bars.py`. Re-runs a failing bar once, and only when it sits within one question's weight
of passing; a larger miss is a regression and fails immediately. The filing golden is not armed
here: it writes, it needs a real worktree per capture, and — even with its bars now calibrated
(`bars.py`'s `FILING_BARS`, fixed from the first Sonnet-5 baseline, `README.md`'s baseline section)
— wiring an instrument this much more expensive into every gate run is a cost decision, not a
calibration one.

All three golden runners self-pin their own checkout's `src/` on `sys.path`; `run_filing.py` pins
the checkout root as well, because it imports `tests.librarian.support`. That is a known trap when
comparing two checkouts: run the OTHER checkout's copy of the script, not this one with a
different PYTHONPATH.

## Gotchas

- **Never run an instrument while anything else is using the docker stack** — the two read
  instruments rebuild the index and the filing one wipes `capture_queue`, so the stack is
  single-tenant while a measurement runs.
- **`--llm fake`/`--embedder fake`/`--backend double` is a plumbing self-check, not a
  measurement.** Each exercises its path keylessly and deliberately does NOT append to
  `history.ndjson`.
- **A fixture is a yardstick.** Editing `corpus/` or `filing/` invalidates every earlier entry in
  the series. The two known bad expectations in the read instruments' golden sets (a literal word
  expected from a free-form answer; a second expected page that matches nothing) are recorded in
  `README.md` and deliberately left in place — they distort every entry identically, and
  comparability is the whole point.
- **Two different guards, and only one of them is refused.** *Drift* against the live knowledge
  repo — "has the real brief moved past this copy?" — is deliberately NOT checked, the opposite of
  `tests/librarian/fixtures/repo/`'s rule: a drift guard keeps a test honest about the present,
  while a yardstick has to stay still or every score recorded under the old brief is silently
  re-graded. *Integrity* of the frozen bytes — "is this still the copy the recorded scores were
  measured under?" — is enforced keylessly by `tests/evals/test_filing_golden_fixture.py`, which is
  where a byte pin belongs. Each copy's `FROZEN.md` records the sha it is frozen at, and
  `filing/repo/PROVENANCE.json` carries the same one for the whole tree.
- **The filing golden's bars are calibrated, but the instrument itself is advisory.** Each quality
  facet carries a real bar (`bars.py`'s `FILING_BARS`), fixed from the first Sonnet-5 baseline and
  recorded in `README.md` beside its row, and a run's own table marks each one PASS/FAIL against it.
  `run_gates.py` never reads `FILING_BARS`, though — nothing but a human reading the printed table
  acts on a miss.
