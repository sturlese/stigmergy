# ADR 045 — the audience comes from the door, and a model never reads what it may not cite

- **Status**: accepted
- **Date**: 2026-08-22
- **Supersedes**: [ADR 010](./010-acl.md)'s *derivation* — ordered rules over a path, resolved by
  the worker at the base commit. Its *enforcement* (one predicate, `visible()`, at one point,
  fail-closed, no existence oracle) stands untouched and is the reason this change is small.
- **Amends**: [ADR 021](./021-views.md) D4 — the view intersection is replaced;
  [ADR 013](./013-http-transport-and-token-auth.md) — the identity seam keeps its shape and loses
  two of its three value spellings; [ADR 042](./042-an-entity-is-born-written.md) — a birth from
  restricted material writes identity and What / Who and nothing else.
- **Built on**: [ADR 044](./044-the-capture-is-the-approval.md). One worker writes, so there is
  one place to stamp; the capture is the approval, so the capture is also where the audience is
  decided.
- **Tracker**: [#146](https://github.com/sturlese/stigmergy/issues/146).
  Narrative: [`docs/reference/server.md`](../reference/server.md),
  [`docs/reference/capture.md`](../reference/capture.md),
  [`docs/reference/page-contract.md`](../reference/page-contract.md),
  [`docs/reference/knowledge-repo.md`](../reference/knowledge-repo.md).

## Context

ADR 010 answered *who may see a document* with **where it lives**, and it was right about the
object: where the **source** lives. What was built resolved the label from the **page's own path
in the knowledge repo**, which is a different sentence, and it makes the only lever for
restricting anything a sub-directory under `wiki/`.

That collides with the structure this brain takes from the LLM-wiki pattern, and with all three of
its properties at once. **The folder is the type**: a leadership decision would have to live in
`decisions/`, where the placement table puts decisions, or in `leadership/`, where the rule could
find it, and every reader that keys on folder would need a second notion of where a decision
lives. **Links resolve by name**, so an audience in the path buys the graph nothing and costs a
`git mv` — an audience change would rewrite the path every index row and backlink was computed
from. **The graph is the value**: directories are a convention to suit the domain, and overloading
one with access control turns a convention into a second schema.

Two facts made this the moment. The corpus is **entirely open** — no page carries `acl:`, so there
is no label to migrate, and the machinery has never been exercised with a real one. And
[ADR 044](./044-the-capture-is-the-approval.md) had just removed the Drive door, which leaves the
platform observing exactly **one** source location: a Slack channel id, already carried on every
captured row (`source_channel_id`) and read by nothing.

Mapping the code before deciding also found six things the design had assumed away:

- `server.acl.all_visible()`, the all-or-nothing predicate for composed text, has **no caller**.
- `pages_index.inlinks` is a **write-only column**; the live backlink surface is a GIN containment
  query that already filters.
- Two surfaces name pages with no predicate at all: the filed card the Slack poller posts into the
  origin channel, and `brain_submissions`.
- A view's timeline renders every member's path and title with no per-member check, and the view
  synthesis authorizes its page reads by *membership* — both sound only because the view's
  audience is the intersection of its members'.
- `[]` means **open** in the librarian's resolver and **nobody** in the server's reader. The first
  real restriction written with the spelling `ops/acl.json` already used three times would have
  meant its opposite.
- Nothing about the submitter, their audiences, the door or the channel reached the resolver,
  although all of it was on the queue row.

## Decision

**The audience label is decided by a human act at the door and travels on the queue row; the
worker stamps it on everything that capture writes; a model never reads what it may not cite.**
Two predicates remain, and there is no table of rules.

**D1 — no audience axis in the repo path, ever.** The folder stays the type. `PAGE_TYPES`,
`FOLDER_BY_TYPE`, the contract linter and the knowledge repo's layout are untouched, and there is
no rules file left for a path rule to live in.

**D2 — the label is the door's, and the door is a person choosing.** Slack takes the channel's
groups (`ops/slack-channels.json`; a channel not listed is public, and public is open);
`brain_submit` takes an `audience` argument (omitted = open); the console's *Register* is open.
Authorization at the door is **`visible()` itself** — you may file only what you could read
afterwards — so it is the one read predicate applied to the writer rather than a second rule. The
decision is stored on the row as a server-owned column, `capture_queue.acl`, and the worker stamps
**that value** on every page the capture writes, `sources/` pages included: a source does not
restrict itself, it restricts what is distilled from it. The worker reads no ACL configuration at
all. Deleted: `kernel.acl.resolve_acl` and its four matchers, `librarian/acl_rules.py` whole,
`base_inputs.load_acl`, and the knowledge repo's `ops/acl.json`.

**D3 — a model never reads what it may not cite.** The upward link — an open page carrying a
restricted page's title — is not a human's doing: the librarian's agent searched the brain
unrestricted, found the page and wrote its title. So the correction is on the **input** side.
One content-flow predicate, `kernel.acl.flows_into(content_acl, page_acl)` (today's
`visible_to_view`, renamed because it now answers the same question in four places), scopes every
page a model reads while writing a page at label L: the filing port, the meeting distiller's
corpus context, the repair proposer, the view synthesis. The write lane is checked with the same
predicate in `gate_zone`, and dedup matches only pages the capture could cite. A human-authored
upward link raises a gardener finding and nothing else.

**D4 — every aggregating surface filters, and the architecture test names them all.** The
enumeration gains a second reader pattern — the checkout reads, `load_pages` and
`read_entity_pages` — and `flows_into` as a valid predicate name, so a module reading pages for a
model on behalf of a page being written must choose, in a reviewed diff, between scoping and
justifying itself.

**D5 — a view carries no label; its members are filtered to open.** The intersection never
widened, correctly, but it **collapsed**: one leadership note anchored to a popular entity made
that entity's view vanish for everyone else. `kernel.acl.view_acl` is deleted.

**D6 — an entity is the shared vocabulary: born open, kept open.** An entity page never carries
`acl:`. A birth from restricted material writes identity and What / Who **only** — facts and
connections belong to the material and stay on the anchored, restricted page — and
`entity_updates` from a restricted capture are dropped and reported.

**D7 — identity is a list of groups, in one shape.** `ops/identities.json` maps identity to
`[group, …]`; `"*"` and the bare-string spelling go. Unrestricted is membership of
`brain-admins`. **Open is the absence of a label**, which `visible()` has always understood, so
"everyone sees open" costs no injection anywhere; `all` becomes a reserved word, refused as a
group name and as an `audience` value.

**D9 — one dialect.** Absent = open, `[]` = nobody, on the queue row, the page, the index and the
reader. A **principal's** empty group list is a different fact — no groups, reads open, files
open — and the door stores `NULL` for such a capture, never `{}`.

### Residuals, named rather than softened

Three places where the rule above is not fully enforced, each with the reason it is acceptable and
the signal that would change the answer:

- **The deletion sweep** (`repair/sweep.py`) fences the doomed pages' bodies and every referring
  page's body into one prompt, across audiences. It is not scoped, because a removal is authorized
  on an unrestricted identity (ADR 044 D3): the person who asked can already read every page in
  that prompt, and the model rewrites bodies they named. It becomes a real gap the day a scoped
  identity may remove anything.
- **A link the capture's own MATERIAL names.** D3 stops a model LEARNING of a page it may not
  link to; it does not stop it repeating a name the submitter typed. That reduces to the
  human-authored upward link this ADR already decided to report rather than police, and the
  gardener's `link-to-narrower-page` is where it lands — including when a declared edit writes it
  onto a third page.
- **The repair proposer runs at OPEN and therefore never repairs a restricted page.** A repair has
  no capture behind it, so there is no human act naming an audience for it; running it at open is
  the fail-closed answer, and the price is a lost convenience rather than a lost invariant.

## What this deliberately does NOT do

- **It does not change enforcement.** `visible()` and its truth table, including malformed →
  nobody, are byte for byte what ADR 010 shipped. Every gap this system has had was a path that
  never called it, which is why D4 is a reachability test and not a rule in prose.
- **It does not let a model near the label.** `acl` stays server-owned and refused as an argument;
  `audience` is a *request*, `capture_queue.acl` is the *decision*.
- **It does not build the general mechanism for a connector that does not exist.** A connector,
  when one returns, is a door that maps its containers to groups the way the channels file maps
  channels. Building the ordered-rules resolver for it a second time is how two of the four
  matchers came to have no input in the first place.
- **It does not narrow a page because a model linked somewhere.** Narrowing would let a model's
  retrieval choice silently restrict a human's capture; demoting the link to plain text would
  leave the title, which is the leak. The input scope closes it at the cause.
- **It does not hide a name.** The existence of an entity is open vocabulary, which is the cost
  stated plainly: if a name is itself the secret, it stays out of the brain — the enclave rule,
  unchanged. The upgrade path is additive and named in the tracker.
- **It does not add admission control or a publisher.** Classification (a second axis, a tenth
  gate) and the projection seam are real and separable; a gate whose ceiling no deployment has set
  is permanently green, and a seam nothing implements is an interface. Both keep their design in
  their own issues.

## Consequences

- Two predicates where there were three, one of them dead: `visible()` decides who reads,
  `flows_into()` decides what may enter a page. `view_acl`, `resolve_acl`, `all_visible` and
  `librarian/acl_rules.py` are deleted outright.
- The knowledge repo loses `ops/acl.json`, and its linter learns that `source` and `meeting` pages
  may carry `acl:`. The platform suite cannot see that half; the change is landed when both
  repositories are green.
- `ops/identities.json` is rewritten in one shape. Every deployment must re-express `"*"` as
  `["brain-admins"]` before the new server starts — a fail-closed refusal, not a silent
  downgrade.
- `brain_submit` grows one argument. It is a contract change to a tool clients rely on: omitted
  means open, which is what every existing caller already gets.
- A capture at a channel whose groups the capturer does not hold is refused at the door with one
  sentence and nothing queued. The brain's roster is the brain's truth; the fix is the roster.
- The filed card and `brain_submissions` become safe by construction rather than by a filter, and
  are pinned by tests that say so.
- Every new refusal ships with its benign twin, because a gate that has never let anything through
  has been measured for sensitivity and never for specificity.

## Alternatives rejected

- **Keep the path resolver, re-key it to the source's location.** With the Drive door gone the
  only source location the platform observes is a Slack channel, already on the row. A general
  resolver with one live input is the dead-matcher problem, rebuilt.
- **A page inherits the tightest label it links to (fail-closed), or the link is demoted.** The
  first punishes the human's capture for the model's retrieval; the second leaves the title. Both
  treat the symptom on the way out.
- **A view at its entity's own audience.** Correct, and moot once an entity is open — it reduces
  to "views are open", with one predicate fewer.
- **An entity born at the label of its material, widened by a human act.** Widening is a
  hand-written label change, which this design forbids everywhere else, and ADR 042 D3's refusal
  of emptiness leaves most such findings with no legal resolution. The end state after widening is
  an open identity; this starts there.
- **`all` as an ordinary label.** It would be degenerate with "absent" for anyone who holds it and
  invisible to everyone who does not — `ana: ["finance"]` would lose every page stamped `[all]`
  unless the resolver injected the label everywhere, which is a rule that has to be remembered in
  three files.
- **A per-page manual override of the derived label.** The LLM-assigned label's cousin: if the
  label is wrong, fix the channel map or resubmit through the right door.
