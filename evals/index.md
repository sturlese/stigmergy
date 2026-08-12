# evals — the three instruments

Code map. What each axis means and how the bars were fixed: [`README.md`](README.md).

## Purpose

Answers *"does the **system** produce the quality we promised"* — as opposed to the unit tests,
which answer *"does the code do what the code says"*.

Three instruments, one per model surface: **retrieval**, **the answer**, and **filing**. All three
need a real key and real model judgment, so all three run BY HAND and append to a git-resident
series; CI stays keyless and never runs them.

Two read, one WRITES. That is the line worth holding when adding a fourth: the filing golden drives
the ONE writer of the knowledge repo through a real `git worktree` and the eight gates, which makes
it the most expensive of the three and the only one whose fixture is also an *input* to what it
measures.

## Key entry points

| Entry | File |
|---|---|
| `make retrieval-golden` → Recall@5 per arm | `run_retrieval.py` (needs `make db-up`. The target passes neither `--rebuild` nor `--repo`, so it scores whatever index is already in the local database. Pass `RETRIEVAL_ARGS="--rebuild --repo evals/corpus"` to measure the corpus, the way `qa-golden` bakes in) |
| retrieval golden set | `retrieval_golden.json` — page-id expectations, part of them carrying `filters.entity` |
| `make qa-golden` → honesty · groundedness · refutation · retry rate · seconds/question | `run_qa.py` (needs `make db-up` + `OPENAI_API_KEY`) |
| QA golden set | `qa_golden.json`; ACL-probe identities in `qa_identities.json` |
| `make filing-golden` → nine quality facets, each with its own denominator | `run_filing.py` (needs `make db-up`, `gitleaks` on PATH, and the filing model's provider key. `BACKEND=double` is the keyless plumbing self-check; `--kinds` measures one kind of capture only) |
| filing golden (10 captures, 12 scored phases) | `filing/captures/manifest.json` (what is submitted) + `filing/expected/expectations.json` (the yardstick), kept apart on purpose |
| `make gates` → one verdict, one exit code | `run_gates.py`; the armed thresholds live in `bars.py`. It arms the two READ instruments only |
| the frozen reference corpus | `corpus/` — committed pages + `PROVENANCE.json` + its own `ops/entity-registry.json`. For `run_qa.py`, `--repo` is also what gives `Settings` an alias map; without it entity-first resolution is inert for the whole measurement. Guarded keylessly by `tests/evals/test_golden_corpus_fixture.py` |
| the frozen mini knowledge repo | `filing/repo/` — its own `ops/` and `PROVENANCE.json`, plus byte-for-byte frozen copies of the knowledge repo's contract linter and both agent briefs (each with a `FROZEN.md`). Frozen, not drift-guarded — see the gotcha below |
| durable eval-score series | `eval_history.py` (`append_run`/`read_history`/`resolve_git_sha`/`corpus_provenance`) → `history.ndjson`, appended by a REAL-instrument run only |
| reports (gitignored) | `out/` |

## What each runner does

**`run_retrieval.py`** — loads the golden set through `index.golden.load_golden`, rebuilds the
index from `--repo` when asked, and scores Recall@5 for four arms: `fts`, `vec`, `rrf`, and `final`
(RRF + contract factors — the arm `bars.BAR_RECALL` reads on). Passes `filters=` through to
`search_arms`, which is what lets the entity questions witness the `entity` filter at all.

**`run_qa.py`** — builds one `AnswerService` per identity in the golden set and drives every
question through the full answering loop. `_score` is deliberately non-literal: numeric equivalence
via `answer/numbers.py`, date equivalence, and `cites` accepting any page in an expected chain.
`_aggregate` reports the three quality axes with separate denominators, plus retry rate and
seconds/question — latency numbers that carry no bar.

**`run_filing.py`** — submits each golden capture through `capture.queue.submit` and drains it with
`worker.process_next`, one at a time, against a throwaway bare-remote-plus-clone seeded from
`filing/repo/` (`tests.librarian.support.build_repo(source=…)`, imported rather than rewritten).
Every facet is read back **out of git at the sha in `result_ref`** — the page that actually landed,
never the agent's account of it — and the anchor comes from the page's server-stamped `entity:`.

Two seams carry the design. `processing.Deps.agent` is injected, so `CountingAgent` counts the
passes one capture spent without any production report having to carry the number. And everything
below the module's pure-scoring banner is a function of data: `score_phase`/`aggregate`/`render`
import nothing heavier than the standard library, so a keyless test scores canned outcomes through
exactly the code a real run uses.

A parking capture yields TWO scored phases — the park, and the re-file after its stored reply
travels back through the real `BrainService.reply`. A backend that never parks does not lose the
second phase; it scores it as a miss, because a vanishing phase would shrink its facets'
denominators and quietly reward not asking.

`--kinds` measures a SUBSET and everything downstream is recomputed from it: `_check_set` derives
the per-facet denominators instead of holding them against `EXPECTED_DENOMINATORS` (which pins the
whole shipped set and only it) and refuses a subset that scores no facet; the caption and the
history row both record the kinds measured.

Three things it does before measuring anything, all in `_run` and all unconditional: it deletes the
librarian App's five environment variables and pins `$CLEAN_LLM` to the fake backend (`make` exports
the operator's env file, and this is the only make target that reaches `processing._file`); it runs
`worker.startup_checks`; and `_check_set` refuses a set whose halves have drifted, whose
expectations name a facet the scorer does not know, or whose denominators no longer match the pin. A
run that ends with a `failed` phase, or with no agent pass anywhere, prints why and appends **no**
row to the series.

**`run_gates.py`** — runs the two READ instruments plus the adversarial suite and judges them
against `bars.py`. Re-runs a failing bar once, and only when it sits within one question's weight of
passing; a larger miss is a regression and fails immediately. The filing golden is not armed here:
it writes and needs a real worktree per capture, so arming it is a cost decision.

All three golden runners self-pin their own checkout's `src/` on `sys.path`; `run_filing.py` pins
the checkout root as well, because it imports `tests.librarian.support`. Known trap when comparing
two checkouts: run the OTHER checkout's copy of the script, never this one with a different
PYTHONPATH.

## Gotchas

- **Never run an instrument while anything else is using the docker stack** — the two read
  instruments rebuild the index and the filing one wipes `capture_queue`, so the stack is
  single-tenant while a measurement runs.
- **`--llm fake`/`--embedder fake`/`--backend double` is a plumbing self-check, not a
  measurement.** Each exercises its path keylessly and appends nothing to `history.ndjson`.
- **A fixture is a yardstick.** Editing `corpus/` or `filing/` invalidates every earlier entry in
  the series. Two known bad expectations in the read instruments' golden sets are recorded in
  `README.md` and deliberately left in place — they distort every entry identically, and
  comparability is the whole point.
- **Two different guards, and only one is refused.** *Drift* against the live knowledge repo is
  deliberately NOT checked, the opposite of `tests/librarian/fixtures/repo/`'s rule: a drift guard
  keeps a test honest about the present, while a yardstick has to stay still. *Integrity* of the
  frozen bytes is enforced keylessly by `tests/evals/test_filing_golden_fixture.py`. Each copy's
  `FROZEN.md` records the sha it is frozen at, and `filing/repo/PROVENANCE.json` carries the same
  one for the whole tree.
- **The filing golden's bars are calibrated, but the instrument is advisory.** Each quality facet
  carries a real bar (`bars.FILING_BARS`) and a run's table marks it PASS/FAIL, but `run_gates.py`
  never reads them — nothing but a human reading the printed table acts on a miss.
