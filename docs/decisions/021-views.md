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
