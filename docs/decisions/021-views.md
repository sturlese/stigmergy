# ADR 021 — views: a derived rollup, and the audience rule a rollup can break

Status: accepted. Narrative: [`docs/reference/views.md`](../reference/views.md).
Code map: [`src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md),
[`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).
Sibling: [ADR 020](./020-meeting-distiller.md), decided alongside this record.

## Context

*"What do we know about X"* was answered by whichever single page happened to be most similar —
demonstrated live against a real corpus, not argued from first principles. The instrument that
should answer it is a **per-entity view**: one derived page per entity, regenerated when its
members change, judged by the same page contract as everything else in the corpus. The `views/`
zone existed from the first index build and the fast lane is forbidden to write it — but nothing
regenerated it, so the zone stayed empty.

One constraint makes this load-bearing rather than merely useful. **A rollup must never widen
access to what it summarizes** — and no other surface in the system has to implement that rule,
because no other page's content is drawn from other pages' governed material. If the writer built
here does not implement it, nothing in the system does.

## Decisions

**D1 — the rollup is derived, and the entity's account of what points at it stays derived.** No
page accretes by hand. The member set is computed from the repo, the timeline and the backlinks
from the repo's own parse. This is the boundary drawn around the single-user LLM-wiki pattern this
design borrows from: read-time navigation is worth exploiting, write-time accretion is not,
because at multi-user scale that pattern's 10–15-page ripple per ingest is exactly the chaos this
system exists to prevent.

**D2 — the members come from the repo, never from the index.** The index is a disposable cache
([ADR 012](./012-hybrid-index.md)); a generator that read it would make the derived view derive
from a derivative, and a rebuild would silently change what a view says. The parse is
`index.corpus`'s own, reused rather than reimplemented — one parser, so a view's member set can
never drift from what a rebuild would compute for the same commit.

**D3 — the skeleton must not wait on the synthesis.** A view is one page holding two things with
different failure modes: a deterministic skeleton (timeline newest-first, backlinks — pure code,
no model) and a bounded agent's synthesis. Splitting them this way is what lets a failed synthesis
still ship a useful page. The decomposition was decided before the ownership question,
deliberately: *who* regenerates views is a scheduling question, *what a view is* is a design one,
and answering the first without the second is how the cheap half ends up waiting on the expensive
one.

**D4 — the audience rule needed TWO gates, and shipping only the obvious one was the worst defect
in this design.** `kernel.acl.view_acl` computes the **intersection** of the members' audiences —
a rollup must never widen access to what it summarizes, and the naive inheritance produces the
union. That much was built correctly, and its sabotage twin genuinely fails when intersection is
swapped for union.

It was not enough. A view's **content** is also fed by a source that is not a member and carries
its own label: **backlinks** — any page in any zone that wikilinks the entity's own page. Backlinks
participated in neither the intersection nor any filter, so an open view could render a restricted
page's title and path. The rule to keep is the general one: *no string derived from a governed
source may render on a view whose audience is not a subset of that source's audience.* That is a
second, separate read gate (`kernel.acl.visible_to_view`), applied at the backlink feed — and
deliberately **not** folded into `view_acl`, because a backlink must never *narrow* the view. Its
default is fail-closed: a caller that forgets to pass the view's real audience gets an open view's
audience, which admits only equally-open backlinks.

The synthesis needs no third gate, and the reason is worth stating rather than assuming: the
agent's one tool reads member pages only, and every member is visible to the view **by
construction** — the view's audience is the intersection of the members', so it is a subset of
each. The second gate exists precisely and only for feeds drawn from outside the member set.

The lesson generalises past views: **the ACL model had quietly assumed a page's audience governs
its content**, and a rollup is the first page whose content is drawn from other pages' governed
material. Any future page assembled from sources inherits this question.

**D5 — one commit per entity, deliberately diverging from ADR 020 D4.** The meeting flow files a
page set atomically because one capture is one indivisible fact. Entities share no such invariant,
so a batch commits per entity: a run that fails at entity k leaves k−1 genuinely done, the repo
coherent, and an interrupt honestly reportable. Two sibling subsystems, opposite rules, each
following from what the unit of work actually is.

**D6 — the withheld synthesis is prose, never a new enum value.** When the synthesis cannot ship,
the page carries the skeleton plus an explicit statement that the synthesis was withheld, in words,
and nothing invents a frontmatter value the contract linter has never heard of. That temptation was
caught **before any code existed**, and it is a recurring defect class in this repo: a design names
a value the contract does not have, and the linter refuses every page that carries it.

There was, at the time, a second road to a withheld synthesis — a figure check failing after its
corrective retry — and the two roads were distinguished on the page and in every operator surface
by an explicit reason, because "did not pass verification" is false of a run that never got that
far. Ingest-time figure verification has since been removed whole
([ADR 026](./026-the-purge.md) D2), so **one road remains**: the bounded agent exhausted its
request/tool-call budget before producing a checkable draft. `render.WITHHELD_BLOCK` states that
budget as a fact and never as a verdict, since no verdict is computed for a view at all — and
`render.SYNTHESIS_CAPTION` says so on every page that DOES ship one, rather than letting silence
read as a check.

**D7 — the failure of a derived view must never taint the fact it was derived from.** The
post-meeting trigger runs after the meeting's own commit and push. A view fault is caught, logged
and recorded to `job_runs`, and never re-raised into the meeting's `Result` — the page set is
already an irreversible success by then. The pattern is the librarian's own: convert an SDK
exception into a domain outcome at the boundary, and let the caller route it.

**D8 — a contract change this ADR exists partly to record**: the branch tip after a meeting filing
is no longer guaranteed to be the meeting's own commit — a successful view regeneration pushes a
second commit on top. `result_ref` (captured before that step) and the returned sha remain the
correct handle for *what this capture filed*. Any code that reads "the current branch tip" to
answer that question is wrong. This bit a test, which is how it was found.

## Known limits

- **The bounded agent catches exactly one of the agent framework's exception classes.**
  `UsageLimitExceeded` degrades honestly into a withheld synthesis; a model outage or an
  unexpected-behaviour error still propagates and fails the regeneration. Whether an outage should
  ship a page at all is a real question, not a patch.
- **Retrieval does not reliably surface a view for broad phrasings** that do not echo its own
  section headings. Measured and recorded as a witness rather than tuned away — a ranking change is
  arbitrated by the golden set, never by inspection, and the entity-shaped cases added alongside
  this work are the first instrument capable of seeing the problem at all.
- **The mechanism is proven against synthetic, single-operator traffic**, and a view is a rollup of
  material one person wrote. How the intersection rule behaves over a corpus many people label
  differently is untested.

## Amendment — a view is never stale, whatever wrote the corpus (2026-08-18, closes #76)

The decisions above stand as written. This section records where the answer to "*who* regenerates
views" — D3 called it a scheduling question and deliberately deferred it — actually landed, and why
"Known limits" never mentioned staleness even though staleness was the limit.

**The problem it answers.** Views regenerated in exactly two places: `librarian.processing` after a
MEETING files, and `stigmergy-views regenerate` run by a human. Every other door left a view stale
indefinitely — an ordinary `brain_submit`, a Slack capture, a Drive drop, an applied repair, an
entity mint, a hand edit in the knowledge repo. `gardener.checks.check_stale_views` flagged it and
nothing acted: the one check with a detector and no actor, because its fix is a REGENERATION and
not one of the three additive edit shapes the repair loop can express. Read at the right level this
is [ADR 039](./039-governed-repair-loop.md)'s complaint once more, for the one finding
class that loop deliberately does not cover. Staging, 2026-08-17: `stale-view` flagged for one
entity and nothing acted on it, and an entity minted the same day with one anchored page had **no
view at all** — nothing would ever have created one.

### C1 — the fix is state-based convergence, never another trigger

"Regenerate after each write" makes every new door remember to call it, and the two doors that must
NOT call it — an applied repair and an entity mint, both running inside the HTTP server process
whose availability [ADR 039](./039-governed-repair-loop.md)'s own audit fixed — would
still leave views stale. `regenerate_entity` was already written for the other shape: an unchanged
member hash with no `--force` is `action="unchanged"`, with **no model call and no commit**; a
changed one is one entity and one commit; no members left, or a de-registered entity, is a REMOVAL.
So a pass that asks the corpus what diverges converges `views/` regardless of what wrote it, and
costs a corpus parse plus a hash per entity when nothing changed. That convergence IS the coverage
guarantee, and it is structural rather than a promise somebody has to keep at each call site.

### C2 — the population is a UNION, and neither existing target was a superset

This is the crux, and it was found by scoping rather than by reasoning from the names. `--all`
(every entity with ≥ 1 anchored page) includes an entity that never had a view and MISSES an
orphaned view whose members have all disappeared. `--stale` (an existing view whose member hash no
longer matches) catches those removals and MISSES every entity that never had a view. So `--stale`
alone — the obvious choice, and the population `check_stale_views` reuses verbatim — would silently
never CREATE a missing view, which was the very case the issue was filed on. The union got a named
helper in the read-only `views.staleness` (which stays git-free, so the gardener keeps importing it
without the write stack) and its own CLI target, `--sweep`, rather than a third meaning on `--stale`.

### C3 — it runs in the librarian worker, and the four crons stay four

A fifth GitHub Actions cron would have been the first one needing the librarian App's private key —
`repair-propose.yml` was built with no write credential at all, on purpose — and a new credential
surface deserves its own argument. The worker already holds that credential, already calls
`views_regenerate.run(..., guarded=False)` after a meeting, and already runs continuously with
`job_runs` bookkeeping. The seam is `Worker.run`'s idle branch: "the queue is empty" is precisely
where maintenance belongs, and `sweep()` (the stranded-claim recovery) was already precedent for
maintenance in that class. The existing post-meeting call becomes a latency optimisation on top of
a guarantee rather than the only road.

### C4 — the idle pass materializes its OWN worktree, and that is what keeps `guarded=False` honest

The post-meeting call BORROWS the capture's worktree, which is where `guarded=False`'s stated
justification comes from: *"the librarian worker, whose ephemeral worktree is always a fresh
checkout"*. An idle pass has none to borrow, so it builds one off a freshly-fetched `origin/<branch>`.
That justification is load-bearing — it is the whole reason the steward guards may be skipped — and
a new caller inheriting the words without the fact is how a documented invariant quietly stops
being one.

### C5 — a per-run ceiling, and it is #69's lesson applied to a second unattended loop

N changed entities are N model calls, and nothing bounded them. The pass stops at a settings-backed
ceiling with an env override — how the repair loop does it — records what it left in
`job_runs.stats.skip_reasons`, and reuses the WORDING of `repair.proposer.RUN_CEILING_REASON` so an
operator does not learn two spellings of one fact. The ceiling counts entities REGENERATED, never
entities examined: an `unchanged` entity costs a hash, and charging the ceiling for it would leave
the tail of the population permanently unconverged however little was actually changing. Nothing is
lost — the population is recomputed from state every pass, so what one defers the next one sees.

### C6 — a fault is recorded and swallowed

A regeneration fault leaves a `job_runs` error row and does not stop the worker draining the queue.
The same best-effort posture the post-meeting hook already has, for the same reason: filing must
never depend on a rollup.

### What this deliberately does NOT do

- **The filing agent does not append to the entity page.** `wiki/entities/` is a governed lane
  outside the agent's write prefixes; an entity page carries ONE `acl` while the accumulated content
  comes from pages with different labels — which the view solves by INTERSECTION and an appended
  page cannot; and the decision would then live both as its own page and as a line elsewhere, with
  `as_of`, supersession and ACL applying to one and not the other.
- **`entity-body` does not become a recurring refresh.** Its dismissal key is `kind+path` on purpose
  ("a re-drafted body is the same question", ADR 039 A3), so recurrence would invert a recorded
  decision and every refresh would cost a steward a review. The split is deliberate: the entity page
  is IDENTITY (long half-life, steward-approved), the view is the ACCUMULATED STORY (short
  half-life, regenerated).
- **No trigger on the repair-apply or mint paths.** Both run in the HTTP server process, which is
  the availability finding #69 already fixed.

### Consequences for "Known limits" above

The staleness limit is closed for the MEMBER SET and only for it. What remains, and is now stated
rather than absent: a view's Backlinks section can drift without its `member_hash` changing — a page
elsewhere gaining a wikilink to the entity's own page is not a member-set change — and a withheld
synthesis over an unchanged member set still has no automatic retry, because the pass converges on
the same hash. `--force` is the recovery for both, and it is an operator's act by design.
