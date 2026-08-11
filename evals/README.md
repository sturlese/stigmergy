# Evals — quality measured, not assumed

Tests answer *"does the code do what the code says"*. Evals answer *"does the **system** produce
the quality we promised"*.

**Three instruments, one per model surface, and that is the whole of it** — golden retrieval,
golden QA and golden filing, each run over a frozen fixture and each appended to a git-resident
series. The posture is deliberate: not a broad offline scorecard scoring many dimensions at once,
but three REAL measurements against real models, run by hand and recorded.

Two of them read. **The third writes**, and that is the whole reason it exists: filing is where a
model's judgment becomes a permanent commit in somebody's knowledge repo, and until the filing
golden there was no instrument on it at all. "Is backend X as good as Sonnet at filing?" was
answered by reading pages by hand and watching gate bounce-rates — enough to catch a disaster,
blind to the gradual kind.

All three need a real key for the measurement that counts, so **none is in CI** — CI stays keyless
by design. What CI *does* check, keylessly, is that the fixtures themselves are intact:
`tests/evals/`.

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

## Golden filing (the write path)

[`filing/`](filing/) — **10 golden captures, 12 scored phases**, driven by
[`run_filing.py`](run_filing.py) through the REAL filing path: `worker.process_next` over real
Postgres, a real bare remote, a real `git worktree`, the eight deterministic gates and the
knowledge repo's own contract linter. No part of it is stubbed, and none of it is a second
implementation of the write path — it is the write path.

### What it files into

[`filing/repo/`](filing/repo/) is a **frozen mini knowledge repo**: five hand-authored pages,
three invented organizations (Northwind Freight, Quillon Labs, Marlowe Publishing — deliberately
sharing no name with the corpus's set or with the test suite's Acme Corp, so the three fixtures
can never be confused), its own `ops/entity-registry.json` and `ops/acl.json` in the real on-disk
dialect, and **byte-for-byte frozen copies of the knowledge repo's contract linter and both agent
skills**, each with a `FROZEN.md` recording the commit it was taken at.

Those copies are what make the instrument hermetic, and they are **not** kept in sync with the
live originals. That is the opposite of `tests/librarian/fixtures/repo/`'s rule, and deliberately:
a drift guard keeps a test honest about the present, while a yardstick has to stay still. The
librarian's brief is the largest single input to filing quality, so freezing it is what lets a
future brief change be *measured* — the run before and the run after each name the brief version
they were judged under. Resyncing it silently re-grades every score already recorded.

### The nine facets, and why there is no single number

Each capture's expectation names the facets it is scored on, and **each facet keeps its own
denominator** — the same rule the QA axes follow, for the same reason: a backend that starts
filing everything as a note must not be able to hide that behind a rising anchor score.

| Facet | What it asks | Denominator |
|---|---|---|
| `status` | the terminal state each capture reached (`filed`/`needs_input`/`rejected`) | 12 |
| `reason` | a refusal's own `reason_code` | 1 |
| `type` | the `type:` of the page that ACTUALLY landed, read back out of git | 9 |
| `folder` | where it landed | 9 |
| `anchor` | the page's server-stamped `entity:` — resolved registry ids, or company-wide | 7 |
| `edits` | which OTHER pages the commit changed, from the agent's declaration | 1 |
| `park_question` | for a park: the unresolved name the question actually captured | 2 |
| `decisions` | for a meeting: one decision page per decision, each with its OWN anchor | 2 |
| `reuse` | a park did not cost the capture a decision | 1 |

**`edits` is scored by CONTAINMENT and its denominator is 1.** Every path the expectation names has
to have been edited; an edit beyond them does not fail the facet. That is not leniency — a declared
edit is confined by `edits.validate` to additive `related:` growth or a callout on a page that
already exists in one of the fast lane's folders, and `gate_zone` and `gate_body_rewrite` judge the
resulting diff, so an extra edit is harmless by construction and refusing it would score the
yardstick's imagination rather than the filing. It also has to be containment because the throwaway
repo **grows during the run**, exactly as production does: the captures file one after another, so a
later one may legitimately backlink a page an earlier one created — a path no expectation written
against the frozen fixture could ever name. What the facet asserts is the other direction, the one
nothing else can see: the reciprocal link an existing page was *owed* actually happened. One capture
owes such a link (F03); the base case names no `edits` key at all, because under containment an
empty list would be true for every backend while still filling the denominator — and because the
first Sonnet-5 baseline showed the empty list was an assumption about one backend, not a
requirement (`expected/expectations.json`'s F01 `why`).

**`attempts`, `bounces` and cost carry no bar and gate nothing** — the same posture the QA
instrument takes with retry rate and seconds/question. A backend reaching the same page in two
agent passes instead of one is more expensive at filing, not worse at it, and a quality axis that
absorbed that would measure two things through one number. Their denominator is **8, not 12**:
the two parking captures name no attempts/bounces expectation, because what a park costs depends
on how the backend parked. `agent_passes` in the report is a different number and counts all
twelve phases.

Scoring is deterministic and there is **no LLM judge**: every facet is a functional fact with one
spelling. The two title matchers are the only loose ones, and they match on normalized *words*
rather than literally — the lesson `globex-meeting-budget` taught this repo the expensive way (see
the growth protocol below).

**The matcher does no stemming, and the obligation for that sits on the expectations.** "tracked"
and "tracking" are two different words to `run_filing._words`, and a stem table would be a second
yardstick nobody reviews. So an expected title is written in the uninflected content words a
paraphrase cannot drop — a proper noun plus a stable noun — and never in the verb form one run
happened to produce. `expected/expectations.json`'s own `_morphology` note states the rule where
the expectations are written, which is where it has to be obeyed.

**With exactly one exception, and it was forced by measurement rather than chosen.** Grammatical
NUMBER is folded at the comparison (`run_filing._same_word`): a trailing `s`, in either direction,
so `review` and `reviews` are one word. Three independent runs of the same code on the same model
titled F08's review decision two ways — singular twice, plural once — with the anchors right every
time, and the 2-denominator `decisions` facet flipped on which one came out. That is the instrument
measuring the model's grammar, which is `globex-meeting-budget`'s defect approached from the other
side: there an expectation demanded a literal word, here it demanded a literal ending. Nothing
wider is folded — a plural written `es` still misses (`process`/`processes`), which is the measure
of how narrow the rule is.

The loosening is **one-directional**, and that is what makes it safe to apply to a live series
rather than a reason to restart one: the predicate is strictly weaker than the one it replaces, so
every pairing that matched before matches now — a recorded PASS cannot become a FAIL and only a
FAIL can flip. The single caveat is `_decisions_match`'s greedy one-to-one pairing, where a weaker
predicate could in principle re-bind a title to a different page; that is exactly what
`test_no_expected_decision_title_can_swallow_a_later_ones_page` guards, and because it calls the
same matcher, widening the match widened its net in the same commit.

**A `decisions` entry may assert a title, an anchor, or both — and which one it asserts is an
empirical question, not a style.** The facet pairs one expectation to one decision page, greedily
and one-to-one; an entry that omits `title` pairs on its anchor alone, and an entry that omits
`anchor` pairs on its title alone. What must never happen is an entry asserting neither (it matches
whatever page is left and measures nothing) or a title-less entry written before a titled one (it
is the weakest matcher, so greedy order lets it eat the titled entry's page). `_check_set` refuses
both, before a model call is spent.

The reason this is empirical: three distinct brittleness shapes were measured on this one facet,
and each moved the assertion somewhere different. Grammatical number moved it to `_same_word`.
Cross-title word distribution — F09's after_reply, where the model put "summary" on the sibling
decision the yardstick had assumed owned it — moved it off that word pair entirely. And when the
seven recorded runs of that phase were re-scored to decide what to assert instead, the answer
**inverted the obvious assumption**: the tracking decision was stable in both its anchor (7/7) and
its words (7/7), while the summary decision was stable only in its TITLE (`shared summary weekly`,
7/7) and *not* in its anchor (`quillon-labs` six times, company-wide once). So an expectation is
written against whatever held across runs, and a yardstick edited from the newest run alone would
have scored six correct runs a miss.

### One expectation that is deliberately weaker than it looks

The `reuse` facet scores whether a meeting re-filed after a park **kept the decisions it had
distilled** — not whether it re-filed them without calling the model again. Both were on the
table; the second is the wrong yardstick. Whether a park leaves a reusable distillation behind is
decided by *how* it parked (`processing._with_park_outcome` keeps one only when the agent decided
to FILE and the gates then vetoed the anchor), and an agent following the meeting brief correctly
parks with `decision: "triage"` instead, storing nothing. Scoring the reuse itself would mark a
brief-following backend down for following the brief. What must not happen on either road is
losing a decision, so that is what is scored; whether the model ran is recorded beside it and
reported.

Because of that, a `reuse` score of 1.00 means one of two different things, and the report says
which: `reuse_at_risk` records whether a stored distillation was there to preserve at all, so
"the capture kept what it had distilled" is readable apart from "there was never anything to
lose". Reported, never scored — like `reused` and `redistilled` beside it.

### How the bars were fixed

From the **first Sonnet-5 baseline** (2026-08-10, platform sha `2b6964f`, fixture
`stigmergy_sha` `0a988bd1`): backend `sdk` — the exploring backend, retired since — model
`claude-sonnet-5`, and its noise twin run
immediately after — **facet-identical scores across the pair** (costs $2.11 and $1.92, walls
716 s and 661 s), which is the determinism the instrument promises on outcome-shaped facets.
Both rows are in [`history.ndjson`](history.ndjson).

| facet | baseline | bar | | facet | baseline | bar |
|---|---|---|---|---|---|---|
| status | 12/12 | 1.00 | | edits | 1/1 | 1.00 |
| reason | 1/1 | 1.00 | | park_question | 2/2 | 1.00 |
| type | 8/9 | **0.88** | | decisions | 2/2 | 1.00 |
| folder | 8/9 | **0.88** | | reuse | 1/1 | 1.00 |
| anchor | 7/7 | 1.00 | | *(attempts / bounces: cost axes, no bar)* | 8/8 · 8/8 | — |

A bar is the baseline's own score, with the fractional pair floored a point: 8/9 = 0.888…
must satisfy its own bar, and a two-decimal 0.89 would refuse the very run that set it. **The
one miss, identical in both runs**: F03 filed as a `decision` in `wiki/decisions` where the
expectation says `note` — a defensible reading of material that records a settled practice
("what the team settled on…"), kept as the recorded disagreement rather than flipped into the
expectation, which would over-fit the yardstick to Sonnet and fail a future backend for the
equally defensible answer. The 0.88 pair therefore tolerates exactly one type/folder
disagreement; the per-capture misses list, not the bar, is where a reader learns *which* cell
moved.

The first run of the instrument (same day, one commit earlier) scored `edits` 0/2 and both
misses were the yardstick's own — that run taught the containment semantics recorded above,
its row was discarded with the defect it measured, and no `suite: "filing"` row predates the
semantics the shipped scorer implements.

**The brief was re-frozen under those bars, and the chain is worth stating exactly.** The `sdk`
retirement closed ADR 033's last debt in the knowledge repo: the brief's environment paragraph still
said *"some runs of this skill hold tools and a checkout, and write the page themselves"*, which
stopped being true of any run when the tool-holding backend went. Knowledge-repo commit
`c1e0996` (*"chore(skills): the brief describes one run style"*) replaced that paragraph with the
one-run-style statement; `31e49f8` is the predecessor every row above was measured under. **Only
that paragraph moved** — the linter and the meeting brief are byte-identical at both commits, which
is why `FROZEN_SHA256`'s other two numbers did not change.

**This re-freeze cannot be validated the way the decisions-facet fixes were.** Those were scorer
corrections: the recorded observations stayed valid, so re-scoring them proved the fix changed the
number it was supposed to and nothing else. Here the INSTRUCTIONS changed, and the observations
above were produced under the old ones — a model briefed differently is a different measurement, and
no arithmetic over stored rows can stand in for running it. So nothing is re-scored and nothing is
back-filled.

What that leaves, stated rather than implied: **the bars stand exactly as fixed above**, under
`31e49f8`, because a bar re-derived from a run nobody has made yet would be a number invented to be
met. The two rows at `907ccc7` are the **first measured under `c1e0996`**, and they held the bars.
Read a row under new brief bytes as a fresh baseline candidate, not as a regression against the
table above. If it moves a facet, the honest reading is "the brief changed", and the choice is the
one `PROVENANCE.json`'s editing policy already names: keep the bars and explain the gap, or retire
the series and record a new baseline here.

**The brief was re-frozen a SECOND time, and the direction reversed.** ADR 034 gave the ordinary
run its tools back, so a brief describing one run style stopped being true again — the opposite way
round from the paragraph above. Knowledge-repo commit `0bf3c546` makes the preamble
environment-CONDITIONAL: it defers the mechanics to the platform's own preamble, states the tools
in a paragraph that applies when the run holds them, and adds the one instruction a run that writes
its own file needs (read `ops/templates/<type>.md` for the container's shape). **Only the librarian
brief moved again** — `FROZEN_SHA256`'s other two numbers are unchanged, and every frozen record in
the tree carries `0bf3c546`.

The same rule applies to it as to the first re-freeze, for the same reason: nothing is re-scored,
nothing is back-filled, and **the bars still stand un-re-derived**. The rows at `907ccc7` were
measured under the previous bytes AND under a one-shot harness; the next row is measured under both
new things at once, which is what the harness-era footnote below is for. Neither re-freeze
relabelled a recorded row — a row says which brief produced it, and that is the whole point of
recording the sha.

### Running it

```bash
# keyless plumbing self-check (the offline double — NOT a measurement, appends no history row)
make filing-golden BACKEND=double

# the real measurement (needs the filing model's provider key and gitleaks on PATH). The default
# backend IS the structured one, and its default model is anthropic:claude-sonnet-5 — the same
# model the retired exploring baseline ran, which is what makes the two rows comparable.
make filing-golden FILING_ARGS="--report evals/out/filing-sonnet-5.json"

# one KIND of capture only — here the two meeting captures
make filing-golden FILING_ARGS="--kinds meeting"
```

**`--kinds` is a different measurement and the instrument says so.** It scores only the captures of
the named kinds, and everything downstream is recomputed from that subset: the per-facet
denominators are derived rather than held against `EXPECTED_DENOMINATORS` (which pins the whole
shipped set and only it), the table's caption names the kinds, and so does the history row — so a
three-phase meeting-only score can never be read later as the twelve-phase set's. A filter that
selects captures scoring no facet at all is refused rather than printed as a table of zeros.
`--backend pydantic` used to REQUIRE `--kinds meeting`, because that backend served the meeting flow
only and would have refused every ordinary capture. ADR 033 gave it both flows, so it runs the whole
set and that guard is gone with the limitation it described. It is a real backend on a real model,
so its row is appended to the series like any other; only `--backend double` never appends.

`make` exports the operator's gitignored env file into every target, so the runner **deletes the
librarian GitHub App's five variables from its own environment and pins the LLM backend to the
fake one** before it builds anything — otherwise a configured App would push this fixture's
commits to the real knowledge repo on GitHub, and a filed meeting's view regeneration would spend
OpenAI money the instrument does not price. Same structural defence as `tests/conftest.py` and
`scripts/e2e_isolate.sh`, in the third place that needs it. It then runs the librarian's own
`startup_checks` before claiming a capture, so a missing `gitleaks` or credential is one loud line
instead of a table of `failed` rows.

Read the double's table with its limits in mind: `librarian/double.py` has no NLP. It files one
well-formed page per capture, always as a `note`, always anchored to the first entity in the
registry, and it parks only on an explicit `DOUBLE:` directive — which the golden captures contain
none of, on purpose. So it scores 1.00 on the facets that are code's (the duplicate refusal's
`status` and `reason`, the cost counters) and well below it on every facet that is judgment's.
Those rows are the instrument reporting that its plumbing works and that the thing it measures was
not present — which is exactly what a self-check should say.

## The gates

[`run_gates.py`](run_gates.py) (`make gates`) arms the bars over the two READ instruments plus the
adversarial suite, and returns one verdict and one exit code. The thresholds live in exactly one
place, [`bars.py`](bars.py), so a report and the gate cannot drift apart about what PASS means.

The filing golden is deliberately **not** armed here. It writes, it needs a real worktree and a
real agent pass per capture, and even with its bars now calibrated ("How the bars were fixed", above),
wiring an instrument this much more expensive into the release gate would make it slower without
making a release decision any more informative.

Because a real model over a real corpus is not deterministic, a failing bar is re-run **once** —
but only when every failing bar sits within one question's weight of passing. A bar missed by more
than one case is a regression and fails on the first attempt.

## The series

All three runners append a real-instrument run (`--llm openai` / `--embedder openai` / any
filing backend but `double`) to [`history.ndjson`](history.ndjson), each entry
carrying the fixture it measured and that fixture's `stigmergy_sha` — so an entry always says what
it was measured on. This is the only durable score record: git is the store, appended by real
runs, never by CI.

The `suite` field says which instrument wrote a row: `retrieval`, `qa` or `filing`. A filing row
additionally carries `backend`, `model`, `kinds` (which capture kinds it measured), per-facet scores
with their hit/denominator counts, the count of what each phase ENDED as (`statuses`),
`total_cost_usd` and `wall_s` — so the instrument prices itself, and a model-policy argument starts
from recorded dollars rather than from an estimate. A row written before `kinds` existed simply has
no such key, which is visibly older rather than ambiguously a subset.

**A filing run that did not measure appends nothing**, loudly: a phase that ended `failed` is the
instrument breaking rather than a backend filing badly, and a run where the agent was never called
is plumbing. Either one would sit in the series indistinguishable from a real score and drag every
trend line it appears in, so the row is withheld and the reason printed. The run's own exit code
and table are unaffected.

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

**Filing rows are keyed by BACKEND ID, and one id has measured three different harness shapes —
read the series with this footnote:** `backend: "pydantic"` means "the real backend" and nothing
more specific. Under ADR 032 it served meetings only; under ADR 033 an ordinary capture was ONE
structured, tool-less call over a gathered context; under
[ADR 034](../docs/decisions/034-agentic-pydantic-harness.md) it is an ITERATING run with five tools
over the checkout, seeded by that same gathered context. Cost, latency and pass counts are
comparable WITHIN an era and are a harness comparison across one — which is exactly what the M4 A/B
reads them for. Nothing was relabelled: a recorded row is never rewritten, and the era a row belongs
to is recoverable from its `git_sha` and from the brief sha in `evals/filing/repo/PROVENANCE.json`.

**Some early rows predate one or both of the `git_sha` fixes and are mislabelled — read them with
this footnote:**
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

The golden sets grow **with every observed miss**: a question — or a capture — is added when the
system is caught getting something wrong, not to pad a score.

Two known expectation defects stay recorded rather than quietly fixed, because the sets are a
yardstick and silently changing a yardstick destroys comparability with every entry already in the
series:

- `globex-meeting-budget` expects the literal word "budget", which an answer is free to
  paraphrase around.
- `benchmark: foxglove prospect` names a second expected page (`foxglove-health`) that matches no
  page id or stem, capping that question's recall at 0.5 forever.

Both distort every entry in the series identically, so comparability is untouched — and both
lessons are already spent: the filing golden matches titles on normalized *words* rather than
literally, precisely because of the first, and `run_filing._check_set` refuses to spend a single
model call on a set whose captures and expectations have drifted apart, precisely because of the
second.

Two further footnotes belong to the filing series and are recorded here for the same reason —
a reader comparing rows needs to know what changed under them:

- **The `decisions` facet stopped measuring the distiller's prose (2026-08-11), in two steps.**
  First, grammatical number: three independent runs of the same code on the same model titled F08's
  review decision as `…-for-review-…` twice (scored PASS) and `…-reviews-…` once (scored FAIL),
  with the anchors right in all three; the matcher now folds a trailing `s`
  (`run_filing._same_word`). Then, on the very next run, a third shape: F09's after_reply produced
  `project-wren-tracked-formally-under-quillon-labs` + `weekly-summaries-consolidated-into-the-`
  `shared-summary`, putting "summary" on the OTHER decision than the expectation `Wren summary` +
  `Wren` assumed. `_decisions_match` now lets an entry assert a title, an anchor, or both, and
  F09's entries were rewritten against what held across all seven recorded runs of that phase —
  see the loose-matching note above, including the part where the seven-run re-score inverted the
  assumption about which half is the stable one.
  Unlike the two defects above, both steps are FIXED rather than recorded-and-kept, and the reason
  is the direction: each was verified by RE-SCORING every recorded run's own observed set against
  the edited yardstick, and neither turned a single recorded PASS into a FAIL (0 of 7 runs, 3 gained
  a PASS). Every score already in the series remains exactly as valid as it was; only a future run
  can benefit. A change that could have lowered a past score would have had to be recorded and left
  alone, like the two above — and the re-score is what makes that a checked fact rather than a
  claim. `evals/out/*.json` carry the per-capture observations that make it checkable, which is the
  reason to keep writing `--report`.
- **Two M2-era rows name superseded shas.** The rows whose `git_sha` is in the `fa55010`/`2be308f`
  era point at commits that no longer exist on any branch: the M2 work was replayed before merge,
  and the replayed trees are byte-identical to `56c0566`/`1466d28`. The replay was not cosmetic —
  CI's history scan caught a credential-SHAPED literal in a test fixture, and the honest fix for a
  string that looks like a secret in git history is to erase it before the history is permanent
  rather than to add a commit on top. So the sha in those two rows is stale while the code they
  measured is fully reachable, at the identical tree, under the new sha. Nothing about the scores
  is affected; a reader resolving those rows should use the pairing above.

**Adding a filing capture is not free the way adding a question is.** Every capture costs an agent
pass on every future run, and — because facets carry their own denominators — a new capture that
names a facet changes that facet's denominator, so scores before and after are comparable per
facet but not per run. Say so in the `history.ndjson` row's own commit when it happens.
