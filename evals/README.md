# Evals — quality measured, not assumed

Tests answer *"does the code do what the code says"*. Evals answer *"does the **system** produce
the quality we promised"*.

Three instruments, one per model surface: golden **retrieval**, golden **QA**, golden **filing**.
Each runs over a frozen fixture, needs a real key, runs BY HAND, and appends to a git-resident
series. None is in CI — CI stays keyless and checks only that the fixtures are intact
(`tests/evals/`).

Two of them read. The third **writes**: filing is where a model's judgment becomes a permanent
commit in somebody's knowledge repo.

## The corpora are frozen — treat them that way

[`corpus/`](corpus/) (the read instruments) and [`filing/repo/`](filing/repo/) (the write one) are
committed, not generated. A fixture is a yardstick: editing one invalidates every
[`history.ndjson`](history.ndjson) entry recorded before it. Never add a page to make a question
pass. Each carries a `PROVENANCE.json` naming the origin of every part.

`filing/repo/` additionally holds byte-for-byte frozen copies of the knowledge repo's contract
linter and both agent briefs, each with a `FROZEN.md`. These are deliberately **not** kept in sync
with the live originals — the opposite of `tests/librarian/fixtures/repo/`'s rule. A drift guard
keeps a test honest about the present; a yardstick has to stay still, or every score recorded under
the old brief is silently re-graded. Byte integrity is enforced keylessly by
`tests/evals/test_filing_golden_fixture.py`.

## Retrieval golden — the hybrid index

[`retrieval_golden.json`](retrieval_golden.json) — page-id expectations; part of the set carries
`filters.entity`, which is what lets the golden witness the `entity` filter at all (proven by
controlled sabotage: breaking the membership clause in `index/search.py` must move the result). The
rest is a deliberate unfiltered control.

[`run_retrieval.py`](run_retrieval.py) reports **Recall@5 per arm** — `fts`, `vec`, `rrf`, and
`final` (RRF + contract factors), the arm `bars.BAR_RECALL` reads on. Needs `make db-up`.

```bash
# keyless self-check (plumbing only)
python evals/run_retrieval.py --embedder fake --rebuild --repo evals/corpus

# the real measurement
python evals/run_retrieval.py --embedder openai --rebuild --repo evals/corpus \
    --report evals/out/retrieval.json
```

## Golden QA — the answer

[`qa_golden.json`](qa_golden.json), driven through the full answering loop (agent → deterministic
verifier → strict gate) by [`run_qa.py`](run_qa.py). Three quality axes, each with its own
denominator so a change to one family cannot move another:

- **honesty** — refusal rate over genuinely unanswerable questions (`kind: refusal` alone). Armed
  at `bars.BAR_HONESTY`.
- **groundedness** — answerable questions answered with the expected figure/citation and a verdict
  that is not `failed`. Armed at `bars.BAR_GROUNDEDNESS`.
- **refutation** — corrective questions (`refute`, `disambiguate`) handled by EITHER an honest
  refusal OR a cited correction carrying the corpus's real figure. Reported, not gated. These leave
  the honesty denominator on purpose: both kinds ARE answerable, and honesty has to keep meaning
  "refusal rate".

**retry rate** and **seconds per question** (median/mean/max) carry no bar. A draft that fails the
verifier earns a second full agent run, and a retried ask usually still ends `verified`, so the
three quality axes are blind to that cost by construction.

Scoring is deliberately **not literal**: a figure matches any numerically equivalent spelling
(`1.074`/`1074`, `512k`/`512000`), an ISO date any rendering of the same day, and `cites` accepts a
chain of pages where any one is valid.

ACL-probe questions run under a scoped identity from
[`qa_identities.json`](qa_identities.json); every other question runs unrestricted.

```bash
# keyless self-check (plumbing + verifier only — the fake synthesizer, not model judgment)
python evals/run_qa.py --embedder fake --llm fake --rebuild --repo evals/corpus

# the real measurement
python evals/run_qa.py --embedder openai --llm openai --model gpt-5.6-terra \
    --rebuild --repo evals/corpus --report evals/out/qa-terra.json
```

## Golden filing — the write path

[`filing/`](filing/) — **14 golden captures, 14 scored phases**, driven by
[`run_filing.py`](run_filing.py) through the REAL filing path:
`worker.process_next` over real Postgres, a real bare remote, a real `git worktree`, the nine
deterministic gates and the knowledge repo's own contract linter. Nothing is stubbed and nothing is
a second implementation — it is the write path.

**One capture, one phase.** Two captures used to be scored twice — a park and a re-file after a
stored reply — and ADR 041 removed the park with the states it waited in. A name the registry does
not know is now PROPOSED: the librarian creates the entity page itself with `approved_by:` empty and
anchors the capture to it in the same commit, and a steward confirms it afterwards from an inbox
this instrument never reaches.

`filing/repo/` uses invented organizations that share no name with the corpus's set or the test
suite's, so the three fixtures can never be confused.

### The eight facets, and why there is no single number

Each expectation names the facets it is scored on, and **each facet keeps its own denominator**: a
backend that starts filing everything as a note must not be able to hide that behind a rising
anchor score. Denominators below are the shipped set's, pinned in `run_filing.EXPECTED_DENOMINATORS`
and refused on drift before a model call is spent.

| Facet | What it asks | Denominator |
|---|---|---|
| `status` | the terminal state each capture reached (`filed`/`rejected`/`failed`) | 14 |
| `reason` | a refusal's own `reason_code` | 1 |
| `type` | the `type:` of the page that ACTUALLY landed, read back out of git | 13 |
| `folder` | where it landed | 13 |
| `anchor` | the page's server-stamped `entity:` — resolved registry ids, or company-wide | 10 |
| `edits` | which OTHER pages the commit changed, from the agent's declaration | 1 |
| `proposals` | for an unregistered name: the identity the filing proposed for it | 2 |
| `decisions` | for a meeting: one decision page per decision, each with its OWN anchor | 2 |

**`edits` is scored by CONTAINMENT.** Every path the expectation names must have been edited; an
edit beyond them does not fail the facet. A declared edit is confined by `edits.validate` to
additive growth on an existing page and judged by the same gates, so an extra one is harmless by
construction. Containment is also required because the throwaway repo **grows during the run** — a
later capture may legitimately backlink a page an earlier one created. Under containment `edits: []`
is vacuously true for every backend while still filling the denominator, so "owes no edit" is said
by naming no `edits` key at all.

**`attempts`, `bounces` and cost carry no bar and gate nothing.** A backend reaching the same page
in two passes is more expensive, not worse. Their denominator is 12, not 14: the two proposing
captures name no attempts/bounces expectation, because each of the three honesty checks in
`librarian.identity` answers with a finding the corrective pass can act on, so a proposal may
legitimately spend a second pass an ordinary filing does not. `agent_passes` in the report is a
different number, over all fourteen phases.

Scoring is deterministic — **no LLM judge**. The two title matchers are the only loose ones and
match on normalized *words*, never literally. They do no stemming, with exactly one exception:
grammatical NUMBER is folded at the comparison (`run_filing._same_word`), a trailing `s` in either
direction, because number is measured run-to-run noise. Nothing wider is folded — an `es` plural
still misses. So an expected title is written in the uninflected content words a paraphrase cannot
drop, never in the verb form one run produced;
[`filing/expected/expectations.json`](filing/expected/expectations.json)'s `_morphology` note states
the same rule where the expectations live.

The loosening is **one-directional** — strictly weaker than the predicate it replaced, so a recorded
PASS cannot become a FAIL and only a FAIL can flip. The one caveat is `_decisions_match`'s greedy
one-to-one pairing, which `test_no_expected_decision_title_can_swallow_a_later_ones_page` guards
using the same matcher.

**A `decisions` entry may assert a title, an anchor, or both**, and which one is empirical: write
whatever held across runs. What must never happen is an entry asserting neither (it matches whatever
page is left and measures nothing) or a title-less entry written before a titled one (it is the
weakest matcher, so greedy order lets it eat the titled entry's page). `_check_set` refuses both.

### Two expectations deliberately weaker than they look

**`proposals` asserts names, not ids, and the two proposing captures assert no anchor at all.** A
proposed entity's registry id is `slugify` of the name the AGENT chose, so `Halcyon Grid` and
`Halcyon Grid pilot` are the same judgment and two different ids. Pinning the id would score the
spelling; the loose matcher scores which unregistered thing was given an identity, which is the
judgment. `proposed_aliases` rides beside the observation unscored, because the interesting near
miss is a filing that read the name as a registered entity's *spelling* and proposed an alias
instead — a red `proposals` cell is much faster to diagnose with that in front of you.

**`F09`'s decision pages are paired on title alone.** Both of its anchors used to be assertable
because a stored reply named a registered entity; nothing names one now, so each decision anchors
either to the proposed identity or company-wide and asserting either would pin the yardstick to a
single sample. The count and the one-to-one pairing still carry the granularity check that facet
exists for.

### The bars

Each quality facet carries a bar in [`bars.py`](bars.py)'s `FILING_BARS`, fixed from a recorded
baseline — except `proposals`, which is a facet ADR 041 created and no run has ever scored, so its
bar is `None`: REPORT, DO NOT JUDGE. **A bar is the baseline's own score, with a fractional value
floored a point** (8/9 =
0.888… must satisfy its own bar, and a two-decimal 0.89 would refuse the very run that set it). The
0.88 pair on `type`/`folder` therefore tolerates exactly one disagreement — a defensible reading
kept as a recorded disagreement rather than flipped into the expectation, which would over-fit the
yardstick to one model. The per-capture misses list, not the bar, is the diagnosis surface.

Bars are never re-derived from a run nobody has made yet: that would be a number invented to be met.
When a frozen brief is re-frozen, nothing is re-scored and nothing is back-filled — a model briefed
differently is a different measurement. Read the first row under new brief bytes as a fresh baseline
candidate, not as a regression.

### Running it

```bash
# keyless plumbing self-check (the offline double — NOT a measurement, appends no history row)
make filing-golden BACKEND=double

# the real measurement (needs the filing model's provider key and gitleaks on PATH)
make filing-golden FILING_ARGS="--report evals/out/filing-sonnet-5.json"

# one KIND of capture only
make filing-golden FILING_ARGS="--kinds meeting"
```

**`--kinds` is a different measurement and the instrument says so.** Per-facet denominators are
derived from the subset rather than held against `EXPECTED_DENOMINATORS`; the table's caption and
the history row both name the kinds. A filter selecting captures that score no facet is refused
rather than printed as a table of zeros.

`make` exports the operator's gitignored env file into every target, so the runner **deletes the
librarian GitHub App's five variables and pins the LLM backend to the fake one** before it builds
anything — otherwise a configured App would push this fixture's commits to the real knowledge repo,
and a filed meeting's view regeneration would spend money the instrument does not price. Same
structural defence as `tests/conftest.py` and `scripts/e2e_isolate.sh`. It then runs the librarian's
own `startup_checks`, so a missing `gitleaks` or credential is one loud line rather than a table of
`failed` rows.

Read the double's table with its limits in mind: `librarian/double.py` has no NLP. It files one
well-formed page per capture, always a `note`, always anchored to the first registry entity, and
proposes an identity only on an explicit `DOUBLE:propose=` directive, which the golden captures
contain none of. So it
scores 1.00 on the facets code decides and well below it on every facet judgment decides — the
plumbing reporting itself healthy.

## The gates

[`run_gates.py`](run_gates.py) (`make gates`) arms the bars over the two READ instruments plus the
adversarial suite and returns one verdict and one exit code. The thresholds live in exactly one
place, [`bars.py`](bars.py), so a report and the gate cannot drift apart about what PASS means.

The filing golden is deliberately **not** armed here: it writes, and it needs a real worktree and a
real agent pass per capture, so wiring it into every release gate is a cost decision.

Because a real model over a real corpus is not deterministic, a failing bar is re-run **once** — and
only when every failing bar sits within one question's weight of passing. A wider miss is a
regression and fails on the first attempt.

## The series

[`history.ndjson`](history.ndjson) is the only durable score record: git is the store, appended by
real runs, never by CI. **A run with a fake backend, a fake embedder or `--backend double` appends
nothing** — a plumbing check has no quality number worth keeping.

`suite` says which instrument wrote a row: `retrieval`, `qa` or `filing`. Every row carries the
fixture it measured and that fixture's `stigmergy_sha`, so a row always says what it was measured
on. A filing row additionally carries `backend`, `model`, `kinds`, per-facet scores with their
hit/denominator counts, `statuses` (what each phase ENDED as), `total_cost_usd` and `wall_s` — so
the instrument prices itself and a model-policy argument starts from recorded dollars.

**A filing run that did not measure appends nothing, loudly.** A phase that ended `failed` is the
instrument breaking rather than a backend filing badly, and a run where the agent was never called
is plumbing. Either would sit in the series indistinguishable from a real score and drag every trend
line it appears in, so the row is withheld and the reason printed. The run's exit code and table are
unaffected.

Appending never fails a run: an eval that died because its bookkeeping failed would be worse than
one that quietly loses a row.

### Re-freezes, and the rows they end

A frozen brief re-frozen is a new measurement, not a regression — nothing is re-scored and nothing
is back-filled, so read the first row under new bytes as a fresh baseline candidate. The re-freezes
so far, newest first:

- **`34cd668` (2026-08-21)** — the linter alone: a wikilink target resolves by its title (last
  segment minus `.md`), not by `Path().stem`, which amputated a dotted title such as
  `[[Acme Inc. Invoices]]` and vetoed a live page as a dead link. The briefs did not change, so
  the re-freeze moves the three provenance shas together and no other byte.
- **`7feee01` (2026-08-21, ADR 041)** — all four frozen files in one commit, which is what makes
  this the cleanest re-freeze the fixture has had: the librarian brief and the meeting-distiller
  brief stop telling the agent to park a capture on the person who wrote it and tell it to PROPOSE
  the identity instead, the contract linter learns the `approved_by:` / `proposed_aliases:`
  lifecycle those proposals land in, and `ops/templates/entity.md` grows the `approved_by` field a
  proposal is marked with. Every score above this row was measured under a brief that could answer
  a capture with a question; nothing under it can. Read the first row after it as a fresh baseline
  candidate, and note that `proposals` has no bar at all yet.
- **`abf6790` (2026-08-18, issue #77)** — the librarian brief, because entity resolution stopped
  being a suffix table in Python and became the agent's judgment fenced by code, and the fixture
  gained the three entities that judgment needs (`Cofers`, `Cofers Legal`, `Meridian Nexus`) plus
  four captures. The MEETING-DISTILLER brief was re-frozen in the same commit for a different
  reason, and it is the one worth reading: it had fallen 106 lines behind the knowledge repo's own
  (ADR 038 gave the distiller corpus context), so `stigmergy_sha` no longer described the whole
  tree and the meeting half of this instrument had been measuring a brief production does not run.
  One sha describes the tree again.
- **`03aab87`** — the original freeze.

**`git_sha` has two spellings.** A bare 40-char sha means cleanliness was CHECKED and the tree
matched. `-dirty` means either the tree did not match, or the check itself failed and cleanliness is
unknown — unknown is spelled the same as dirty on purpose, so a failed probe cannot launder a dirty
tree into a clean-looking row. Outside a git checkout the field is empty, never a guess. The series
file itself is excluded from the probe, or one instrument's own row would make the next one `-dirty`
over nothing but bookkeeping.

**A recorded row is never rewritten**, including its `backend` label. Which harness or brief produced
a row is recoverable from its `git_sha` and from `filing/repo/PROVENANCE.json`.

## The growth protocol

The golden sets grow **with every observed miss**: a question — or a capture — is added when the
system is caught getting something wrong, never to pad a score.

Two known expectation defects stay recorded rather than quietly fixed, because silently changing a
yardstick destroys comparability with every entry already in the series:

- `globex-meeting-budget` expects the literal word "budget", which an answer is free to paraphrase
  around.
- `benchmark: foxglove prospect` names a second expected page (`foxglove-health`) that matches no
  page id or stem, capping that question's recall at 0.5 forever.

Both distort every entry identically, so comparability is untouched — and both lessons are spent:
the filing golden matches titles on normalized *words* because of the first, and
`run_filing._check_set` refuses to spend a model call on a set whose halves have drifted because of
the second.

A yardstick change is only allowed to be FIXED rather than recorded-and-kept when it is verified by
re-scoring every recorded run's own observed set against the edited yardstick and no recorded PASS
becomes a FAIL. `evals/out/*.json` carry the per-capture observations that make that checkable,
which is the reason to keep passing `--report`.

**Adding a filing capture is not free the way adding a question is.** Every capture costs an agent
pass on every future run, and a capture that names a facet changes that facet's denominator — so
scores before and after are comparable per facet but not per run. Say so in the `history.ndjson`
row's own commit.
