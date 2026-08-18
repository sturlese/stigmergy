# ADR 024 — the gardener and the digest: a two-pass health report, an ACL-scoped broadcast

Status: accepted. Narrative:
[`docs/reference/gardener-digest.md`](../reference/gardener-digest.md). Code maps:
[`src/stigmergy/gardener/index.md`](../../src/stigmergy/gardener/index.md),
[`src/stigmergy/digest/index.md`](../../src/stigmergy/digest/index.md).

## Context

The corpus had no health surface: cross-document patterns are invisible to any single filing, and
every control this system had added at the filing level said so explicitly — anchor concentration
was measured at 14 of 18 pages on one entity, `gate_anchoring` cannot see a distribution across
filings, and the company-wide escape hatch was taken in production with nothing standing behind it
but the promise that a gardener would detect it. Meanwhile a week's worth of corpus activity —
pages filed, entities born, findings raised — was rendered nowhere a human actually looks.

The original design sketched an agent fleet — gardener, digest, supervisor — plus a gaps report
the digest was meant to consume, and all of it existed as prose only. (Of the three sketched
agents, the supervisor was built and then removed; ADR 026 D3.) The shape borrowed for the two
that remain is a pattern, not code: the two-pass lint — *the deterministic tool finds the
mechanical problems for free; you add the judgment calls* — scaled from one script to two packages
and a cron.

## Decisions

**D1 — findings-only, structurally, and a two-pass shape: deterministic checks as the provable
core, a bounded model sweep for what only judgment sees.** `stigmergy-gardener` fixes nothing,
writes nothing, vetoes nothing — not asserted, grep-provable: the package imports no git plumbing
beyond two declared, pure-policy symbols (`librarian.page.is_provenance_type`, `librarian.config`'s
`--repo` default) and holds no literal path under `wiki/`, both pinned in
`tests/test_architecture.py`, and the first of those two proofs has a transitive sibling beside it
(see D6 for why an AST-level check alone was not enough). Eight deterministic checks
(`gardener/checks.py`) are exact and cheap: each a query over `pages_index`/`capture_queue`/the
entity registry/the repo checkout, none interpreting meaning. The model editorial sweep
(`gardener/sweep.py`) is the judgment half — PydanticAI structured extraction through
`stigmergy.kernel.llm.build_processor`, the one fake/real dispatch every agent-building module in
this codebase shares, down to the one-retry, log-and-skip discipline.

The fleet sketch had these agents running on the Claude Agent SDK (Claude Code headless) with a
repo checkout and skills versioned in the knowledge repo — the librarian's own shape. The sweep
does none of that: it never checks out a repo, carries no skills and calls no tool at all, and
`SWEEP_LIMITS.tool_calls_limit=0` is a structural property of the agent's own usage limits, never
a request made in a prompt, so bounded agency is satisfied by construction rather than by a
narrower prompt. The SDK-checkout shape becomes necessary only when a gardener WRITES — opens an
issue, files a PR — which this one never does. A stated deviation, not an oversight.

**D2 — the digest is deterministic assembly, never model prose: a second, separate deviation from
the same sketch.** `digest/render.py`'s two sections — corpus health and corpus deltas — are
built by plain code from plain dicts (`digest/sections.py`): the same inputs always produce the
same body, byte for byte (`--dry-run`'s own byte-identity requirement rests on this). The sketch
also framed the digest as "top-model synthesis"; that framing is declined outright, for one
reader: data beats marketing when the only reader is the operator who already ran the checks.
Slack-only, no `meta/` pages. Both deviations are recorded here rather than left for a future
reader to notice that the agents that exist do not match the original description of them.

**D3 — watermarks live in `job_runs.stats`, and the anchor is a captured instant, never
`started_at`.** The rule applied: prefer `job_runs` stats if the shape fits; a dedicated tiny table
only if it does not. It fits — the sweep needs exactly one thing carried run to run (how far it has
read, and where the rotating unchanged-page sample left off), and `job_runs` already carries a
JSONB `stats` column per run, the same table the report's own counts and the digest's own "latest
gardener run" read already use. A second table would duplicate a place this one already has.

The first cut anchored the next run's `since` to the previous run's `job_runs.started_at` — wrong,
because `capture.ops.record_job_run`'s own INSERT writes `started_at = now()` at INSERT time,
after the deterministic checks ran, after the sweep read the corpus and called the model, after
every finding for the run had already been computed. A page filed between "the sweep read the
corpus" and "the row committed" therefore fell in NO sweep window at all: not the run that had
already resolved its own boundary before the page existed, and not the next run either, whose
`since` (read from `started_at`) started strictly after the page did. The fix:
`_run_sweep_pass` captures `selected_at = datetime.now(UTC)` immediately before `select_pages`
runs and persists it in `stats["sweep"]["selected_at"]`; `previous_run_watermark` prefers that
value over `started_at`, falling back to `started_at` only for a pre-fix row with no `selected_at`
at all. The digest carries the identical fix under its own name: `run_digest` resolves
`until = now()` before any of its section queries run and persists it as `stats["until"]`,
never trusting `job_runs.started_at` for the same reason, one package over
(`digest/run.py`'s own docstring states it symmetrically).

**D4 — `job_runs.status` grows a third value, `partial`, and a failed sweep never advances the
watermark it would otherwise poison.** Before this fix, a run whose deterministic checks succeeded
but whose model sweep failed committed `status='ok'` — the report already showed the right thing
(deterministic findings intact, an honest stderr line about the sweep), so the run "looked"
complete. But `ok` is also the one value `previous_run_watermark` trusts as a baseline for the NEXT
sweep's `since`. A week of model outage under the daily cron therefore meant seven `ok` rows, a
watermark advancing every night regardless, and every page filed during that week permanently
excluded from "changed since last time" — recoverable only through the rotating sample, whose own
offset had also silently advanced past pages nothing had actually judged — while
`job_runs WHERE status='error'` reported zero failures the whole time.

The fix: the run's own status becomes `partial` whenever the sweep pass's own stats carry an
error, in the same transaction the deterministic findings commit in — the report and the exit code
are unchanged. `gardener.store.latest_completed_run` (the digest's own read of "the latest run")
widens to `status IN ('ok', 'partial')`, because the deterministic findings are exactly as
trustworthy either way; `gardener.sweep.previous_run_watermark` stays `status='ok'`-only, on
purpose, because that reader specifically needs to know whether the SWEEP's own baseline is safe
to build on, and a `partial` row's sweep contributed nothing to build on. Two readers of the same
column, deliberately disagreeing about which values they trust — `capture/ops.py`'s own module
docstring is now the shared spec for this vocabulary (`ok` / `error` / `partial`, the last
introduced here), so a fourth job reaching for a fourth status value reads it first rather than
reusing `partial` for an unrelated meaning.

**D5 — the digest broadcasts, so it is ACL-scoped at the destination channel; the SLA notice is
scoped identically; the residual is named, not hidden.** `stigmergy.digest` is the one gardener-
adjacent surface that renders page titles and page paths into a channel rather than an operator's
own terminal — `acl.visible()` is the one place read access is decided in this codebase, and a
channel is a broadcast surface, not a scoped reader with its own audience. Every page-shaped fact
the digest names — a filed page's title, a filed page's path — is read through
`server.acl.visible(acl, audiences)` at `audiences = slack.channels.channel_audiences(channels_path,
digest_channel_id)`: that CHANNEL's own scope, never the operator's, never unscoped.

This is also why `digest/sections.py` reads `pages_index`/`capture_queue`/`review_decisions`
directly, by raw SQL, rather than through a `gardener`-precomputed shape. The tempting alternative
is to have the gardener compute "pages filed"/"entities born" into its own `job_runs.stats` (it
already reads the corpus every run) and have `digest` read them through the ONE edge it already
has — the findings store — for no new import and a clean gardener-reads-corpus →
digest-reads-gardener's-output pipeline. Not taken, because the gardener has no caller identity
and no destination channel at all (an operator tool, terminal output only — see that package's own
layering notes); a page title it precomputed into `job_runs.stats` would necessarily be rendered
against no audience, or the wrong one, and sit there unscoped for any future reader of that run's
stats to see. Reading the tables directly, at the one place — `digest`, at post time — that
actually knows the posting channel's own audiences, is what keeps every page title's ACL check
where `server.acl.visible()` is the one place it is ever made.

The same rule reaches the SLA notice `stigmergy.gardener` posts at run time, which uses the exact
same channel and originally carried no ACL scoping of its own at all. The mechanical guard
(`tests/test_architecture.py::ACL_REACHABILITY_EXCEPTIONS`) could not see the gap, because the
notice reads findings rows, never `pages_index` — the predicate the guard actually checks for.
`gardener.notice.scope_findings_to_channel` is the fix: every SLA finding whose page is not
visible at the posting channel's own audiences gets its notice wording — never its report row —
redacted before `compose_notice` composes anything. The scoping runs over the run's pre-insert,
in-memory finding list, never the list re-read from `gardener_findings` after the insert:
`store.py` round-trips only the columns the table has, so the persisted list has already lost
every `_notice_*` key by the time it would reach here, and scoping THAT list would silently no-op
every redaction.

**A correction to that fix, kept because its lesson outlives it: the scoping key's shape must
match the composition's shape.** The first version was only apparently closed — a scalar key over
list-composed text. It named only the finding's own page path, but the wording it protects was
composed from a case file's own contradiction item, which for one item shape printed a SECOND
page's path literally and for another was informed by a second page's content without ever
printing it. A finding whose own page was unlabelled but whose text named a restricted page posted
that restricted page's identity unredacted — the scalar key was checking the wrong (or an
incomplete) fact, and the existing test could not catch it because it hand-built the leaked text
and the redaction key naming the SAME page, by construction. The fix: `_notice_page_paths`, always
a list, enumerating every path a rendered item's text is composed from *or* informed by, plus
`server.acl.all_visible` — an all-visible-or-drop predicate extracted to `server.acl` and reused
by the digest's own multi-page filing rule rather than invented twice. The general defect is a
scalar key over list-composed text, and it reappears every time a new sentence template names a
page the key does not already know about — a risk the first labelled pages will exercise for real.

The two checks that ever produced an `sla`-severity finding were the contradiction SLA's two arms
(D7), and both left with their subjects — the canon lane (ADR 026) and the learning loop
(ADR 027). Everything above survives them with no producer: `notice.py` still runs, and its
"exactly one message, never zero-to-many" contract is true today only in the degenerate sense that
it is always zero. Giving the notice a producer again means a check that actually emits an
`sla`-severity finding, and that check inherits every rule in this decision.

The redaction is a substitution, never a drop: `_redact` replaces a finding's notice-facing detail
and action with a fixed sentence ("redacted — the page this finding is about is not visible at
this channel's scope") and leaves the finding itself, its severity and its position in the count
exactly where they were. Record the residual honestly: a redacted line still occupies its slot and
is still counted, so an aggregate count tells a channel reader that N findings exist about pages
they cannot see. That is the accepted, proportionate trade while the digest channel is effectively
operator-only: the alternative — dropping the finding from the notice entirely — kills the alert
for exactly the pages most worth alerting on, since a contradiction about a page a scoped channel
cannot see does not stop mattering because the channel cannot see it.

Also recorded plainly, because the code's own docstrings already say it and a design record must
not imply otherwise: `ops/slack-channels.json` is optional, and where it has not been created —
`scripts/deploy_staging.sh` bakes an empty `{}` when the knowledge repo carries none — every
channel resolves to the empty audience set (`slack/channels.py`'s own docstring). Per
`acl.visible()`'s truth table an empty audience set sees every page carrying no `acl` label —
which, while no page carries one, makes this filter indistinguishable from no scoping at all. It
becomes load-bearing the moment the first labelled page exists. Built now, deliberately, because
retrofitting a filter onto an already-shipped broadcast surface is how leaks happen.

**D6 — `views/staleness.py` is its own module, because an architecture test's own claim was false
one layer down.** The stale-view and dead-vocabulary checks need exactly two read-only facts
`views.regenerate` already computed: which entities have a view whose member set has drifted, and
which registered entities are anchored at all. The obvious thing to do was import
`views.regenerate.list_stale_entities`/`.list_all_anchored_entities` directly.

The obvious thing was wrong: `regenerate.py` also module-level-imports `views.writer` — the
commit-and-push path, `librarian.gitcmd`/`.githubapp`, GitHub App credential minting. Importing
`regenerate` for its two read-only functions therefore loaded the entire git write stack into
every gardener process the moment `gardener.checks` imported it — one attribute access
(`writer.commit_and_push`) away, inside that same module's namespace.
`tests/test_architecture.py::test_gardener_never_touches_git_plumbing` claimed, by its own
docstring, to rule this out "by construction". That was true at the AST level (`checks.py` names
no `stigmergy.librarian` symbol beyond `librarian.page`) and false at the loaded-module level,
because an AST check sees only a module's own direct imports, never its transitive closure.

The fix is a relocation, not a rewrite: `views/staleness.py` holds `view_relpath`/`view_path`/
`existing_member_hash`/`existing_view_ids`/`list_stale_entities`/`list_all_anchored_entities` —
every symbol either check needs — and imports neither `writer` nor `synthesis`. `regenerate.py`
imports these six names FROM `staleness.py` (unchanged for its own existing callers — `views/cli.py`,
the librarian worker's post-meeting trigger), and `gardener.checks` imports `views.staleness`
directly, never `views.regenerate`. The architecture test's claim is true now because the import
graph makes it true, not because its own docstring asserts it more carefully — the second,
subprocess-level test (`test_gardener_transitive_views_reach_is_a_named_declared_exception`)
watches the loaded-module set itself, and is the standing proof this stays true.

**D7 — the contradiction SLA's two arms were two named, tested approximations, not two silent
guesses.** Both arms are gone — the canon-proposal arm with the canon lane (ADR 026), the
promoted-candidate arm with the learning loop (ADR 027) — and the decision is kept because the
rule it states outlives its subject: an approximation gets named in the record, never smoothed
into a sentence implying a truer measurement exists.

Arm (a) — an open canon proposal whose case file already carried a contradiction — aged off
`opened_at`, not a "contradiction detected" timestamp, because no such timestamp existed: the case
file was composed once, at propose time, by a deliberately narrow contradiction finder
(paragraph-scoped co-occurrence, not full extraction) and never updated after that. The SLA clock
that check reported was genuinely "how long has this proposal sat open with a contradiction
already in its case file", not "how long has the contradiction existed"; the two would have
differed only if a contradiction could be added or discovered after a proposal opens, which
nothing did.

Arm (b) — an approved, promoted `correction` candidate whose filed page had produced no follow-up
proposal — joined across two string conventions that no foreign key protected: the loop's
`promoted_ref` (`'capture:<capture_queue.id>'`, written once at approval time) and
`capture_queue.result_ref` (`'<page_path>@<sha>'`, written once by the librarian at filing time).
Neither was a typed reference; both were parsed defensively (a compiled regex, an
`rpartition("@")`), and a row that did not match either shape was skipped and counted, never
guessed at or reported as an anomaly. Both conventions were pinned by tests specifically so a
future change to either format would be caught there rather than silently starving the check of
matches. The check's own silence was itself an expected steady state, not a weaker claim than it
could support: "no proposal yet" is what a freshly-promoted correction looked like until a steward
deliberately made one, since nothing created that proposal automatically.

**D8 — `sweep.select_pages` runs outside the sweep pass's own try/except, deliberately: a code
defect there fails the run loudly rather than degrading quietly.** `gardener.run._run_sweep_pass`
wraps the judge call and its one retry in a broad `except Exception`, specifically because a model
outage or a `SweepGarbage` retry exhaustion must not cost the operator the deterministic findings
the SAME run already computed — D1's own "two independent passes" promise, made to survive a sweep
failure. `select_pages` — the pure-SQL query that decides which pages the sweep even looks at —
runs BEFORE that try block, unguarded.

The reasoning, stated rather than left implicit: a bug in `select_pages` (a malformed query, a
logic error in the rotation arithmetic) is a defect in this package's own code, not a "sweep
outage" in the sense that phrase is meant to cover (a model-call failure or unusable model output,
both external to this codebase). Catching it the same way would hide a real code defect behind the
same honest-degradation story a model outage gets, and the two deserve different responses from
whoever reads the failure. The acknowledged cost, named rather than absorbed: a transient DB error
in that one query — a connection drop, a statement timeout, nothing to do with a defect in the
query itself — aborts the whole run (`status='error'`, zero findings persisted) and costs the
operator the eight deterministic checks' already-computed findings too, even though nothing about
THEM failed. That trade was taken deliberately: the alternative (catching everything around page
selection) would make a real code defect indistinguishable from an honest model outage, and this
package's own discipline is to fail loud on the former rather than blur the two.

## Consequences

- Two more entrypoints join the set that must never enqueue or file anything, and they are held
  to it structurally rather than by discipline: no git plumbing and no literal `wiki/` path in
  either package (`tests/test_architecture.py`) rules out every route to a page except the capture
  queue, which leaves exactly one route that has to stay provably shut.
- `gardener` is granted a `stigmergy.slack.gateway` edge originally intended for `digest` only — the
  SLA notice is posted by the run that found it, because nothing listens for "gardener finished"
  and the digest is not even on a schedule. Recorded here as a deliberate, one-line layering
  amendment, not discovered as a failing architecture test after the fact.
- `job_runs.status`'s vocabulary (`ok` / `error` / `partial`, documented in `capture/ops.py`) is
  now load-bearing for a second job beyond the all-or-nothing jobs (`capture-purge`, `digest`)
  that use only `ok`/`error`. A future job with its own independently-failable auxiliary sub-pass
  reaches for `partial` the same way `gardener` does, rather than inventing a fourth value or
  reusing `partial` for an unrelated meaning — `capture/ops.py`'s own docstring is the place to
  read, and update, before either happens.
- Four hardening fixes are worth naming because each marks a trap rather than a design choice: a
  malformed `updated` date no longer aborts the aging-seed and stale-view checks; a registered
  alias ending in punctuation can now match the company-page-names-entity check; a silent
  `job_runs` write failure after a real digest post is a loud, nonzero-exit refusal naming the
  duplicate-repost risk; and the daily cron and a manual dispatch queue rather than race.
- Two of those needed a second pass, which is the more interesting half. The malformed-date guard
  checked the value's SHAPE, not its VALIDITY, so a calendar-invalid date (`"2026-02-30"`) still
  aborted the same two checks through the identical mechanism the first fix was supposed to have
  closed — and `AND` never guaranteed the regex ran before the cast in the first place. It is now
  `CASE WHEN pg_input_is_valid(updated, 'date') THEN … ELSE false END` (PostgreSQL 16+), which
  validates the value and forces evaluation order the way a plain `AND` cannot. Separately, a
  malformed `ops/slack-channels.json` could fail — and lose the report of — a run that had nothing
  to post at all; the posting channel's audiences are now resolved no earlier than the SLA
  short-circuit already promises, with `IdentityError` joining the notice-path exception tuple for
  the case where an `sla` finding DOES fire and the channels file is ALSO broken.
- The digest's "entities born" line is labelled to name what `review_decisions` actually records:
  an approval, not the later, separate mint commit.

## Amendment — the empty-body pass: a second model pass, and coverage instead of sampling (2026-08-18, closes #78)

The decisions above stand as written; this section records what the model half of the gardener
grew, and why it could not be a fifth bullet in the sweep's existing prompt.

**The problem it answers.** ADR 039's first amendment gave the corpus a check for an entity page
that still carries its template (`entity-placeholder-body`) and a repair that drafts its body. That
check is deliberately literal: the page has to still contain `<one clear paragraph…>` to be seen.
The wider half of the same gap is the one that matters — **a body that is WRITTEN but says nothing
is completely invisible.** `Cofers is a company we work with.` passes every deterministic check
there is, and so does anything somebody typed in thirty seconds. It is exactly the page a drafted
body would improve most, and it was the one page class the repair loop could never be offered.

### A1 — a fifth model-check slug, and its OWN pass

`model-empty-entity-body` observes a body that does not say anything about the entity in
particular — no specific facts, nothing named, no links to the pages that would state them.

It is a second pass rather than a fifth bullet in `SWEEP_SYS`, and the reason is the population.
The editorial sweep judges a batch of changed-plus-sampled pages drawn from `pages_index`; a slug
hung on that prompt would inherit the sample, and an entity page would be judged only when the
rotation happened to reach it — which is precisely the silent miss this check exists to end. The
new pass has its own prompt, its own population and its own model calls, and REUSES everything
that is not one of those three: the output schema, the validator, the one-retry discipline, the
finding assembler.

That reuse forced one change with a property behind it: `_validate` now takes its allowed slug set
as a PARAMETER instead of reading the module's full vocabulary. It is load-bearing in both
directions — it is what lets the new pass accept only its own slug, and what stops the four-check
sweep from emitting the fifth on a sampled page whose text argues for it.

### A2 — coverage, not sampling, with a ceiling that is never silent

Entity pages are a bounded population — a few dozen — so this pass sees all of them, batched
`STIGMERGY_GARDENER_EMPTY_BODY_BATCH` (8) per call. A sampled judgment check would leave pages
unjudged while the report read as "nothing wrong", which is the failure mode, not a lesser version
of it.

A ceiling exists anyway (`STIGMERGY_GARDENER_EMPTY_BODY_CEILING`, 150) because nothing else bounds
the model spend if a corpus ever grows hundreds of entity pages. It is a spend bound and not a
sampler: when it binds, the run RECORDS what it deferred — in `job_runs.stats`, as a run skip
reason, and as a log warning naming the variable that raises it. A ceiling that truncated in
silence would read as a clean bill of health for the pages it never opened.

There is deliberately no rotation and no watermark. Adding one would make the pass a sampler with
extra steps, and the honest answer to a ceiling that binds every night is an operator raising it,
which is what the recorded sentence asks for.

### A3 — the interaction with the deterministic twin is settled by EXCLUSION

A page still carrying literal placeholder lines is already reported by `entity-placeholder-body`,
and it is removed from this pass's population **before the model is asked**. One finding per page
across both checks by construction, rather than by a downstream de-duplication that a later
re-ordering or re-keying of findings could defeat — and therefore one repair proposal for one page
on one night, never two.

That exactness requires both halves to be talking about the same page set at the same instant, so
the entity zone is walked ONCE per run — `run.run_gardener` calls `checks.entity_zone_pages` and
hands the same list to both consumers, which are pure functions of it — and "still carries
placeholders" is one predicate (`checks.placeholder_lines`). One function called twice was not
enough: the two calls straddled the editorial sweep's model call, so a page that lost its
placeholders in between was reported by both checks and one that gained them by neither. The zone
is still spelled in two segments, because the gardener may hold no literal path fragment under the
knowledge checkout.

**That walk is also a confinement boundary, and it was the only reader of a checkout in this
codebase that was not.** What it reads no longer stays on the machine: a body is fenced into a
model prompt and shipped to the provider, and a sanitized excerpt is persisted into
`gardener_findings.detail`, printed in the terminal report and rendered in the admin console. A
committed `wiki/entities/Acme.md -> /proc/self/environ` would have made all three happen to the
target. The walk now refuses a symlinked leaf AND resolves every path component
(`librarian.page.is_inside`, the same pair `librarian.gather._confined` uses), refuses to open a
file above a fixed byte cap, and COUNTS every page it declines to open into
`stats.empty_body.walk_exclusions` — a check whose whole justification is "a silent miss reads as
nothing wrong" cannot drop a page from its own population in silence.

### A4 — `info`, not `warn`, and the severity table is now spelled out

It is the judgment twin of an `info` deterministic check, and what it invites is a drafted body a
steward reads before approving; `warn` would inflate the digest for a page nobody is at risk from.
`MODEL_CHECK_SEVERITY`'s blanket comprehension over the slug tuple became an explicit entry per
slug, so a severity is a decision somebody made rather than something a new slug inherits by being
added to a tuple.

### A5 — the slug joins the repair loop's body road

Without that, the finding has no path to zero and this amendment would only move the problem to a
report nobody can act on. `BODY_PROPOSABLE_CHECKS` gains it, `subjects=[path]` so the dismissal
memory keys on the page and survives finding-id churn, and the road itself is unchanged: the two
checks are the deterministic and the judged halves of ONE question, so they share an answer rather
than each getting one. The `MIN_ANCHORED_PAGES` floor stays exactly where ADR 039's A2 put it — in
the proposer, before the model call — so a reported page with too little evidence produces a
finding, no draft, and a recorded reason.

### A6 — how the two model passes' failures combine

They fail independently and are reported independently (`RunResult.sweep_error` and
`RunResult.empty_body_error`, `stats["sweep"]` and `stats["empty_body"]`), and a failure of either
commits `partial`: a run's status is the one place an operator learns a whole model pass did not
happen.

**That makes `job_runs.status` an aggregate over two independent passes, and nothing may read it as
a verdict on one of them.** `previous_run_watermark` used to select the last `status='ok'` run,
which was correct while the sweep was the only model pass and became false the moment a second one
could commit `partial` beside a healthy sweep. It now asks the sweep's OWN recorded outcome —
`stats.sweep.error` empty, on an `ok` or `partial` row — which is the read `digest/sections.py`
already performs on the same blob for the same reason. Two consumers of one question, one reading.
There is no price to pay for a `partial`: each pass's watermark, or absence of one, follows that
pass's own outcome, so a failed empty-body pass costs the sweep's `since` and sample rotation
nothing. Had the run's status stayed the input, a nightly empty-body failure would have pinned
`since` at the last flawless run and grown the sweep's single unbatched prompt every night until it
killed the editorial sweep too, while the rotating sample re-judged the same pages forever.

Within the pass, a failing BATCH stops the pass and the batches already judged are kept: a failure
is a fact about the judge or its answers rather than about page 57, so continuing would most likely
just repeat the bill — and validated findings are validated however the next batch went.

### Consequences

- `job_runs.stats` grows an `empty_body` block beside `sweep`. It carries `population`,
  `excluded_placeholder`, `considered`, `judged`, `deferred`, `unjudged`, `walk_exclusions`,
  `batches`, `inserted`, `skipped` and `error` — every page the pass did not judge is counted under
  one of them. `skipped` means validation rejections in BOTH passes; the ceiling's own count is
  `deferred`.
- `partial` now has two independent producers within one job. The vocabulary in `capture/ops.py` is
  unchanged; what changed is that a `partial` gardener run no longer implies the editorial sweep
  specifically failed, and a reader — human or code — has to look at `stats` to know which pass did.
- The terminal report names both passes: the empty-body pass's own failure and the run ceiling's
  deferred count reach the corpus line, not only `job_runs.stats` and the log. A run summary that
  reads clean while a whole model pass did not happen is the failure this ADR's check exists to
  end, one layer up.
- The knowledge repo's `repair-proposer` skill names the checks it answers, and now names one more.
