# ADR 015 — The librarian: the agent judges, code vetoes the diff

**Status:** accepted · 2026-07-26

## Context

[ADR 014](./014-capture-queue-and-attribution.md) made the brain writable and stopped there. A
capture is durably queued, attributed by the server to the real human, and archived in the evidence
plane — and then nothing drains it. `capture_queue` declares seven states and the queue's own
transitions reach three of them; no capture has ever become a page.

Three specific things were missing, and none of them could be built separately:

- **A consumer.** Capture without filing is an inbox, and an inbox nobody drains is where knowledge
  goes to be forgotten. The promise the ack makes ("queued — the librarian files it") had no
  referent.
- **A boundary for the trust machinery.** The PII gate, figure verification, the contract linter and
  ACL resolution are all specified *at the commit*, and there was no commit. They were designed and
  unbuilt.
- **A measurement.** "capture→page p50 < 5 min" needs a page, and so does "pages need zero manual
  formatting fixes".

What makes this the highest-stakes component in the system is the combination it holds: **the only
repo write credential, and a job description that consists of reading untrusted material.** Every
control upstream of it is defeated by a document that can talk it into a commit. So the question this
ADR answers is not "how do we file a page" but *where the judgment lives and where the veto lives*.

## Decision

### 1. The agent judges. Code vetoes. They are different jobs and they do not share a seam.

The filing decision — what type is this, which folder, what title, which existing pages does it
relate to, is this a near-duplicate of something — is judgment over prose, and no amount of code will
make it deterministic. It is the Claude Agent SDK, headless, with its operating procedure versioned
as a skill in the **knowledge repo** (`.claude/skills/librarian/`) rather than in this one, because
it is the company's filing policy and belongs where the people whose knowledge it files can review
and PR it.

Everything that decides whether the result *leaves* is deterministic code: the zone and path
whitelist, gitleaks over the diff, four PII patterns, the contract linter, the server-owned
frontmatter stamp, ACL resolution, the anchoring outcome. Gates check; they never interpret.

**This is the shape of the thing, and the reason it exists.** A document can talk an agent out of a
finding and it cannot talk gitleaks out of one. The asymmetry is the whole security model: the
agent's output is untrusted by construction, and the gates are the trusted half precisely because
they have no judgment to be argued with.

**Rejected: an LLM reviewing the agent's work.** The same argument that settled it for the ingestion
pipeline. A reviewer that can be persuaded is not a control, and two models agreeing is not evidence.

**Rejected: prompt-level defense as the primary mitigation.** The skill does fence all captured
material as `UNTRUSTED DATA` — and material that tries to steer the librarian is itself a reportable
finding on the submission — but that is defense in depth, not the boundary. The boundary is that the
agent physically cannot write anywhere the gates do not see.

### 2. The agent works in an ephemeral git worktree, and the diff is the unit of veto.

Each item gets a fresh `git worktree add --detach` of the knowledge repo. The agent reads the whole
graph through it (Read/Grep/Glob) and writes only inside it; it has no network and no shell. Code
then diffs the worktree against its base commit and runs every gate over that diff. The worktree is
removed in a `finally`, and leftovers from a crash are reaped at startup — never reused, because a
reused worktree carries the previous item's uncommitted work into this item's diff.

**Why a worktree and not a structured proposal.** The alternative — the agent returns a JSON object
describing a page and code writes the file — has a smaller blast radius and a trivial test story. It
was rejected for *whole pages* for two reasons. It needs an invented schema for every kind of edit,
which is a second contract to keep in step with the page contract; and the fleet would end up with
two different ways to write to this repo, since the gardener and the digest genuinely need a
checkout. The diff, by contrast, is the natural unit of veto: **anything the agent does beyond its
lane is visible in it and refused.** Not enumerated in advance — visible.

**Why `--detach` and not a branch.** The knowledge repo has `main` checked out in a human's own
working copy, and `git worktree add` refuses a branch that is already checked out elsewhere. A
detached worktree at the same commit sidesteps that and cannot move anybody's branch.

**Three things the diff taught us, and only two are fixed by how it is invoked.** `git diff` needs
`--text`, or a single NUL byte anywhere in a written page makes it emit `Binary files differ` and no
content lines — which the secrets gate, the PII gate and the body-rewrite gate each read as "nothing
to object to". And it needs `core.quotePath=false`, or a page titled `Café` arrives as
`"wiki/notes/caf\303\251.md"`, matches no `wiki/` prefix test, and is refused as a system
fault while the linter's findings for it are silently dropped. A veto surface that can be blinded is
not a veto surface, and both blindings were reachable from content.

The third could not be fixed by an invocation flag, because it was never about the diff being blind —
it was about `gate_body_rewrite` reading the diff's *rendering* at all. Classifying lines by their
`+`/`-` prefix is defeatable from page content: a deleted body line spelled `--…` reads as a header,
and a page line merely shaped like a `related:` field was handed unread to the superset proof that
judges that field — position-blind, so a body line spelled that way was admitted whenever the real
frontmatter field happened to be a superset. Three human-authored lines were deleted with the gate
silent before this was found. The fix is not a stricter parser but no parser at all: the gate now
reads the base version straight out of the object database (`git show HEAD:<path>` — unforgeable,
since nothing in a page's content can change what a commit already contains) and the worktree's copy
off disk, and compares the two directly. A modified page may only gain appended callout lines and grow
its `related:` link set — never lose a link, reorder one, or change anything else in the frontmatter —
and an edit whose "before" cannot be read at all is refused rather than assumed additive.

### 3. Edits to existing pages are DECLARED by the agent and PERFORMED by code.

The agent writes only *new* files. When it wants a reciprocal `related:` link, or an
overlap/contradiction callout on a page that already exists, it names that edit in its outcome and
code applies it — validated against the real graph, landing in the same diff, judged by the same
gates.

**This is an amendment, and the evidence for it came out of real runs.** The original decision let
the agent make its own additive edits, with a gate refusing anything that was not a pure addition. On
the first real agent run the agent rewrote the body of an existing human-authored page, the gate
correctly refused the whole capture, and it did the same again on the corrective retry after being
handed the finding. Two for two — and the mechanism explains why: for a language model, *"insert a
line into this file and change nothing else"* is a strictly harder and less reliable operation than
*"say which link you want"*.

Cross-linking is not decoration. It is the difference between placing a page and integrating one, and
it is the librarian's whole value — an unlinked, unanchored page is met by retrieval only by luck. So
a mechanism that fails at it is not acceptable, and the declarative form removes the failure class
rather than defending against it.

**The retry it spent is no longer spent.** "Two for two" was the measurement; the second of the two
was always going to fail, because by then the agent could not have caused the finding and cannot
reach it — the modified page in the diff is code's own edit. `zone/body-rewrite` and its
`unreadable-edit` sibling are therefore marked as naming no agent-side repair
(`gates.unrepairable`), and the item refuses after one agent pass with the report saying so. Same
terminal state, one agent run sooner, and no brief handed back telling the agent to repair work it
did not do.

**The gate that refused was right and stays.** What changed is who performs the edit. And the change
*tightens* the security surface rather than loosening it: the agent's write confinement becomes "a
new `.md` file in one of the fast lane's folders, with no modifications at all", which is a strictly
smaller allow-list. Code's own edits are provably additive by construction, so they pass the same
gate they are subject to.

**That confinement guarantee holds only because "does not exist yet" is asked correctly.** The write
hook and the edit validator both ask "is this page already in the repo" through `page.path_key`,
which folds Unicode normalization form and case before comparing — never through `==` against
`git`'s own tracked-path spelling. macOS/APFS, the primary deployment platform, is case- and
normalization-insensitive, so an exact byte comparison answered "no, this is new" for
`EXISTING NOTE.md` and for the NFD spelling of an accented title, and a write under either spelling
landed on the human's page with a diff showing only added lines — regaining, from a re-spelled name,
exactly the capability this decision removed. `agent.confined_write` and `edits.validate` share the
one helper now, so the question cannot be answered two different ways in two places again.

**Rejected: letting the agent retry until it gets the edit right.** Two attempts is the per-item
budget, and an agent that cannot satisfy a deterministic gate in two tries will not on the third.
Spending the budget on a mechanism that is known to fail is worse than changing the mechanism.

### 4. Figure verification bounces the capture. It does not file with a banner.

Every numeric token on the filed page must trace to the archived material. If it does not, the agent
gets exactly one corrective retry with the verifier's findings; if it still does not, the submission
is **`rejected`**, naming which figure did not trace, and no page enters `wiki/`.

This is deliberately *different* from what the ingestion pipeline did. That pipeline banners and
demotes, and that is right there: it processes documents that exist and must be recorded, so
refusing to record them loses information that is real. **A conversation has no such obligation.**
The material survives in the evidence store, the submitter can resubmit, and nothing is lost by
refusing.

What *would* be lost by filing: a page carrying a number nobody can trace, protected only by a
ranking penalty, inside the corpus the answering agent cites from. A `verification: failed` page in
`wiki/` is the thing that kills the project.

**Rejected: filing to `triage` instead of rejecting.** `triage` means "a human has to decide
something" and is drained by a steward. An untraceable figure is not an open question; it is a
correction the submitter can make in ten seconds and nobody else can make at all.

**This check no longer exists, and the reason it went is worth as much as the reason it came.**
Ingest-time figure verification — the gate this section is about — was removed whole
([ADR 026](./026-the-purge.md)), along with the fact store and the `verification:` frontmatter field
it stamped. Two things decided it. At ingest the check taxes the model's own prose with false
positives, one of them measured: a correct, page-backed `2.3x` was refused because the checker did
not know the `x` multiplier. And it cannot catch the dangerous class anyway, because an invented
*claim* passes every figure check. What protects the reader is what it was always going to be: the
verbatim source one click away, plus the answer-time verifier that cites or refuses. What survives
of the decision above is its posture — a capture is refused rather than filed with a caveat — which
is still how the secrets and PII gates behave.

### 5. Nothing is filed ownerless. Every page declares its anchoring outcome.

Each filed page carries either **≥1 wikilink to an entity page that resolves through
`ops/entity-registry.json`**, or an **explicit company-wide scope declaration with a written
reason**. Silence is not an outcome. When the librarian believes the material is about an entity but
cannot resolve the name, the submission goes to **`triage`** with that name recorded as its open
question — no page, no commit.

The rule is uniform across every fast-lane type. A per-type exemption — the first proposal had some
types requiring an anchor and others not — is precisely how `notes/` becomes an undifferentiated
dumping ground, which is the failure mode of a company brain: not bad ranking, but **ambient content
with no owner**.

### 6. Three page types the fast lane may create, and always `status: developing`.

`note`, `decision`, `concept` — `wiki/notes`, `wiki/decisions`, `wiki/concepts`. Everything else in
the vocabulary may be read, linked and cross-referenced but never created here: identity pages
because entity birth is governed; `source` and `meeting` because they are provenance records written
by code from captured material, never drafted; `view` because it is regenerated from an entity's
members. A capture the librarian judges to be one of the excluded types lands in `triage` with the
reason — never silently downgraded to `note`.

Note the exclusions are **not** about maturity: `status` is a maturity axis, not a type, which is
why `decision` is in. `owner` is **stripped** from a fast-lane page entirely — a `developing` page
has a submitter, not an accountable owner, and a capture must not be able to assign accountability
to somebody — and a page that declares `status: canonical` about itself is filed as `developing`
anyway. That last rule originally leaned on a promotion lane with PR ceremony as the one path to
canon; the lane was removed later ([ADR 026](./026-the-purge.md)) and the rule did not need it: the
fast lane writes one status, whatever the material claims about itself.

### 7. The librarian has its own GitHub App identity.

Commits are authored by the `stigmergy-librarian` GitHub App (fine-grained, `contents: write` on one
repo), with the human recorded in a `Submitted-by:` trailer and in the page's `submitted_by`.

The point of git as the substrate is that the audit is already done: *who changed what* is in the
history. **A librarian committing with a human's own disk permissions makes `git blame` lie about the
one thing the substrate exists to record.** Credentials come from the environment, the installation
token is minted per push and reaches git through `GIT_CONFIG_*` in the child's environment rather
than through argv — argv is world-readable via `ps` and `/proc/<pid>/cmdline`, so a token passed
positionally is a token published to every other process on the machine.

**Rejected: a fine-grained PAT.** It is a *person's* token wearing a service's hat; it inherits that
person's identity in the history and their lifecycle in the org.

**Absent configuration is a supported state, not an error.** A run with no App configured pushes to
`origin` as whoever the process is, which is exactly what the test suite and the docker e2e do
against a bare local remote that needs no credential at all. What is refused instead is the dangerous
*combination*: a remote on `github.com` with no App configured, which is the only case where the
misattribution can actually happen.

### 8. One capture, one commit — and the sha in the report is the one that landed.

A 1:1 trace from submission to sha. `result_ref` is `wiki/<folder>/<Title>.md@<sha>`, and
`git show <sha>` shows exactly one added file.

**"One" is enforced, not merely reported.** An outcome that created more than one new page is
refused outright (`multiple-pages`) before a commit is ever attempted — not filed with only the
first page named. Without that check, a second page would be committed and pushed while appearing on
no surface a human reads: `result_ref`, the commit subject and the dedup pointer can each only name
one path, and `gate_anchoring` unions wikilinks across every new page and is satisfied if any ONE of
them resolves — so an unlinked second page could ride in on the first one's anchor, past the very
check that exists to stop an ownerless page.

`pull --rebase` before push, retry on a race, and a genuine conflict fails the item rather than being
resolved — nothing here gives the librarian merge judgment, and a librarian that resolves conflicts
is a librarian that can silently drop a human's edit.

**The sha is read back AFTER the push, and that is a correction the docker e2e forced.** A rebase
rewrites the commit, so the sha the commit produced names an object in no reachable history once a
retry has happened — and that string is the submitter's report and the sha `git show` is supposed to
accept. Three of twelve pages were reported at shas the remote had never heard of. The same e2e
showed that a three-attempt push budget with a flat backoff let *contention* fail an item, which
contradicts the rule above: a race is explicitly not supposed to fail anything, only a conflict is.

### 9. The lease must outlive the item, and the worker refuses to start if it does not.

The worker claims with the queue's `claim_next` (`FOR UPDATE SKIP LOCKED`) and finishes with `finish`
fenced by `attempts`. That fencing is reused and never reimplemented: it fixed a real defect caught
before the queue shipped, and it is what makes single-writer serialization a property of the queue
rather than of there happening to be one worker.

But the fence is not sufficient on its own, because **the commit and the push happen before `finish`
is attempted.** A worker whose lease expired mid-item would go on to file a capture a second worker
is also filing, and `finish` would refuse the row afterwards — correctly, and far too late. So two
things guard it: `startup_checks` refuses to run unless
`visibility_timeout_s > MAX_AGENT_ATTEMPTS × timeout_s + GATE_BUDGET_S`, and the lease is re-asserted
immediately before the push, which narrows the window from "a whole agent run plus the gates" to
"one push".

The visibility timeout is therefore **derived from this worker's own bounds**, not inherited from the
queue's default. Inheriting the number was the right doctrine and the wrong value: 300s was chosen
for a human-scale `stigmergy-queue claim` and one librarian item is two agent attempts plus two
gitleaks runs, two whole-repo linter runs, a commit and a retrying push.

## Consequences

- **A page whose prose is subtly wrong passes every gate.** That is the semantic half this system
  explicitly leaves out of scope. The fast lane's answer to it is `status: developing` plus
  `submitted_by` — the page says who brought it and does not claim to be reviewed — not
  verification. Maturity is something a human confers by reading, and that is where they take it on.

- **Refusing is a worse failure than it looks.** A secrets or PII refusal means a person's capture is
  rejected, and capture dies with friction. So every gate carries a *benign twin* in the suite — an
  email address, a person's name, a figure quoted from its own source, a 16-digit number that fails
  Luhn — because a defense tested only against attacks has been measured for sensitivity and never
  for specificity. The lab measures specificity; only real use measures the rate.

- **`filed` is not `searchable`.** After the commit the page is invisible to `search_brain`/`ask`
  until the index catches up. Every report says so, in the same sentence as the success rather than
  as a trailing footnote an edit can drop. An incremental upsert on push arrived later, but it is
  best-effort — inert until an operator configures it, and deferring to the nightly rebuild whenever
  it cannot keep up — so the promise the report made stayed the conservative one for a while.
  *Amended 2026-08-04:* on a webhook-enabled deployment the conservative wording was
  false in the common case — the page was searchable seconds after the push while the report still
  promised invisibility — so the report's sentence now names both paths ("at the next index rebuild
  or at the webhook's incremental upsert, whichever lands first"). The clause is still welded to the
  success sentence; only its content moved from conservative to true-everywhere.

- **Triage fills up before it can be drained.** Anything about an entity the registry does not hold
  parks. That is the anchoring contract working, and the governed birth door
  ([ADR 016](./016-human-loop-and-entity-governance.md)) is what drains it. The lever to revisit, if
  the rate makes the system unusable, is how liberally a page may declare company-wide scope — never
  whether an orphan may be filed.

- **Agent output is not deterministic.** Two runs over the same material may pick different titles,
  links or placement within the whitelist. The gates bound what is *unsafe*, not what is *good*, and
  the honest lever for quality is the skill file — versioned, diffable, improvable — not more gates.

- **The write credential is a real credential.** Bounded by a gitignored env file and by fine-grained
  permissions on one repo, it can still rewrite the company's knowledge, from a machine that also
  runs agents.

- **Cost per capture is unmeasured.** An agent run with a repo checkout is the most expensive
  operation in the system. Per-item bounds cap one runaway run; nothing caps a day of them.

- **N > 1 workers is not safe against one checkout**, whatever the queue guarantees:
  `startup_checks` reaps every librarian worktree registered in the checkout it starts in, so a
  second worker starting while the first is mid-item would delete that worktree underneath it.
  Separate checkouts is the shape the claim holds for.

## Alternatives rejected

- **The agent returns a structured proposal and code writes everything** — smaller blast radius,
  trivial to test, and it needs an invented schema per edit kind plus a second way to write to the
  repo. Adopted for *edits* (§3) and rejected for *whole pages*, because determinism is worth more
  than expressiveness exactly where the thing being protected is somebody else's writing.
- **A commit-without-pushing fallback** when the App is unconfigured — it would become the silent
  default and the push path would never be exercised.
- **Presidio for PII** — in a brain whose org chart is pages about people, name-level detection
  refuses legitimate work constantly, and a gate that cries wolf is a gate people route around.
- **A per-type anchoring exemption** — see §5. It institutionalizes the failure mode.
- **Ask-back, at first** — the flow that asks the submitter "which client is this about?" needs a
  push channel MCP does not have, so those captures parked in `triage` instead. It arrived later:
  `brain_reply`, the `needs_input` state and the Slack thread that carries the question
  ([ADR 016](./016-human-loop-and-entity-governance.md)).
- **Deploying the worker, at first** — the early runs are judged by watching them, and keeping the
  worker on the operator's own machine is what lets it be watched. It runs as its own deployed
  process group now, booted by `stigmergy-librarian-boot`, which clones the knowledge repo with the
  App's identity before the worker ever claims a row.
