# Evals — quality measured, not assumed

Tests answer *"does the code do what the code says"*. Evals answer *"does the **system** produce
the quality we promised"*.

**Two instruments, and that is the whole of it** — golden retrieval and golden QA, both run over a
frozen corpus, both appended to a git-resident series. The posture is deliberate: not a broad
offline scorecard scoring many dimensions at once, but two REAL measurements against real models,
run by hand and recorded.

Both need `OPENAI_API_KEY` for the measurement that counts, so **neither is in CI** — CI stays
keyless by design. What CI *does* check, keylessly, is that the fixture itself is intact:
`tests/evals/test_golden_corpus_fixture.py`.

## The corpus: frozen, and treat it that way

**[`evals/corpus/`](corpus/) — 38 pages, COMMITTED and frozen.** It is committed rather than
generated precisely so that it cannot drift: a corpus assembled on demand is a corpus that quietly
changes underneath a score series, and then no two entries in that series are comparable.
[`corpus/PROVENANCE.json`](corpus/PROVENANCE.json) carries the origin of every part, including the
knowledge-repo SHA the baseline entry names.

**Do not regenerate it, and never add a page to make a question pass.** It is the fixed thing the
moving parts are measured against, so a silent edit invalidates every `history.ndjson` entry
recorded before it.

## Retrieval golden (the hybrid index)

[`retrieval_golden.json`](retrieval_golden.json) — 16 real questions. Ten of them carry
`filters.entity`, which is what lets the golden see the `entity` filter at all: proven by
controlled sabotage — breaking the membership clause in `index/search.py` must move the result.

[`run_retrieval.py`](run_retrieval.py) reports **Recall@5 per retrieval arm** — `fts`, `vec`,
`rrf`, and `final` (RRF + contract factors, the arm the R@5 ≥ 0.80 bar reads on). Needs the docker
postgres (`make db-up`).

```bash
# keyless self-check (plumbing only)
python evals/run_retrieval.py --embedder fake --rebuild --repo evals/corpus

# the real measurement
python evals/run_retrieval.py --embedder openai --rebuild --repo evals/corpus \
    --report evals/out/retrieval.json
```

## Golden QA (the answer)

[`qa_golden.json`](qa_golden.json) — 26 questions over the frozen corpus, driven
through the full answering loop (agent → deterministic verifier → strict gate) by
[`run_qa.py`](run_qa.py). It reports three quality axes, each with its own denominator so a change
to one family cannot silently move another, plus two latency numbers (below) that carry no bar:

- **honesty** — the fraction of genuinely unanswerable questions the brain correctly **refuses**
  (the anti-hallucination metric). The denominator is the `refusal` kind alone: 9 questions.
  **≥ 0.90 is the armed bar.**
- **groundedness** — the fraction of answerable questions answered with the expected
  figure/citation and a verdict that is not `failed` (the false-refusal watch). Denominator: the
  14 answerable questions (`exact` and `prose`). **≥ 0.84 is the armed bar.**
- **refutation** — the fraction of corrective questions handled correctly, where correct means
  EITHER an honest refusal OR a cited correction carrying the corpus's real figure. Denominator:
  the 3 `refute` questions (false premise). Reported, not gated.

  This axis exists because refusal-only scoring was wrong: a brain answering *"the benchmark says
  2.3x, not 5x"* — the best behaviour available — was being recorded as a miss. The scorer also
  understands a `disambiguate` kind (mixed-entity, scored the same way: a real figure correctly
  attributed is as correct as a refusal), though the current set carries no such case.

**retry rate** and **seconds per question** (median/mean/max) ride alongside the three axes and
carry no bar: a first draft that fails the deterministic verifier earns a second full agent run,
and because a retried ask usually still ends `verified`, the three quality axes above are blind to
that cost by construction — these two numbers are the instrument for it instead. Both are recorded
per question, and both reach [`history.ndjson`](history.ndjson) alongside the three axes.

Scoring is deliberately **not literal**: a figure expectation matches any numerically equivalent
spelling (`1.074`/`1074`, `512k`/`512000`, `2,3x`/`2.3x`), and `cites` accepts a chain of pages
where any one is a valid citation.

The three ACL-probe questions run under the `analyst` identity
([`qa_identities.json`](qa_identities.json), scoped to `all`, no `sales`) so the sales-scoped pages
they target are correctly out of scope; every other question runs unrestricted, under `steward`.

```bash
# keyless self-check (plumbing + verifier only — the fake synthesizer, not model judgment)
python evals/run_qa.py --embedder fake --llm fake --rebuild --repo evals/corpus

# the real measurement
python evals/run_qa.py --embedder openai --llm openai --model gpt-5.6-terra \
    --rebuild --repo evals/corpus --report evals/out/qa-terra.json
```

Figure questions are answered from page bodies — there is no separate facts store to consult.

## The gates

[`run_gates.py`](run_gates.py) (`make gates`) arms the bars over both instruments plus the
adversarial suite, and returns one verdict and one exit code. The thresholds live in exactly one
place, [`bars.py`](bars.py), so a report and the gate cannot drift apart about what PASS means.

Because a real model over a real corpus is not deterministic, a failing bar is re-run **once** —
but only when every failing bar sits within one question's weight of passing. A bar missed by more
than one case is a regression and fails on the first attempt.

## The series

Both runners append a real-instrument run (`--llm openai` / `--embedder openai`, never the keyless
self-check) to [`history.ndjson`](history.ndjson), each entry carrying the corpus and its
`stigmergy_sha` — so an entry always says what it was measured on. This is the only durable score
record: git is the store, appended by real runs, never by CI.

Appending never fails a run (see [`eval_history.py`](eval_history.py)'s own docstring): an eval
that died because its bookkeeping failed would be worse than one that quietly loses a row.

**`git_sha` has two spellings, and the second covers two cases.** A bare 40-char sha: cleanliness
was CHECKED and the tree matched — the measured code IS that commit. `-dirty`: either the check ran
and the tree did not match (a real measurement whose exact code is not recoverable from the sha
alone), or the check itself failed (a `git status` timeout, an index lock) and cleanliness is simply
not known. Unknown is spelled the same as dirty on purpose rather than getting a third spelling: a
probe that fails must not be able to launder a dirty tree into a clean-looking row, and "assume
dirty when unsure" says that with one concept instead of two. Outside a git checkout entirely there
is no sha to report and the field is empty — never a guess.

**The series file itself is excluded from the probe.** `append_run` writes this tracked file, so
without the exclusion one instrument's own row would make the NEXT instrument's row `-dirty` over
nothing but bookkeeping. "Dirty" has to mean the measured CODE differs.

**Some early rows predate one or both of those fixes and are mislabelled — read them with this
footnote:**
- the two `qa` entries at `8030527` (2026-08-01T10:05, T10:09) predate the marker entirely and
  measured that commit PLUS an uncommitted change — the very defect that prompted the suffix. They
  are the 0.80 and 0.867 samples that established this instrument's run-to-run noise.
- the `qa` entry at `126286a-dirty` (T10:15) is marked dirty by self-contamination: the only
  modified file was the `retrieval` row appended three minutes earlier. The measured code was
  exactly `126286a`.

**Read groundedness as a noisy number.** Two of those runs are the same code on the same corpus
with the same model: 0.80 and 0.867, and `benchmark-speed` — a deterministic miss in one — passed
the other. A one-case delta on a 14-question denominator is inside the noise.

## The growth protocol

The question sets grow **with every observed miss**: a question is added when the system is caught
getting something wrong, not to pad a score.

Two known expectation defects stay recorded rather than quietly fixed, because the sets are a
yardstick and silently changing a yardstick destroys comparability with every entry already in the
series:

- `globex-meeting-budget` expects the literal word "budget", which an answer is free to
- `benchmark: foxglove prospect` names a second expected page (`foxglove-health`) that matches no
  page id or stem, capping that question's recall at 0.5 forever.

Both distort every entry in the series identically, so comparability is untouched — and both
lessons (literal-word expectations; an exact-id preflight) belong in whatever set replaces this one.
