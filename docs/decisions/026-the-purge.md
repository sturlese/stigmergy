# ADR 026 — the purge: the inherited organs come out, a kernel stays

Status: accepted.
**Amended by** [ADR 044](./044-the-capture-is-the-approval.md) D6: D6 here closed the digest's cron and it stays closed — the digest is a command. What changed is the other half of the sentence: the purge's own "daily cron" is now a daily pass inside the librarian worker, at `STIGMERGY_LIBRARIAN_RETENTION_AT`.

## Context

This platform was built by importing a working earlier system and rewiring it onto a new
substrate, piece by piece. That was the right way to start — the operational lessons
(lease fencing, TOCTOU on a gated diff, NUL-byte gates, deploy/index-rebuild ordering) live in
code and tests, and a greenfield rewrite would have re-paid every one of them.

It also meant the system arrived carrying organs the design never asked for. Reviewed against the
references it is actually built on — Karpathy's LLM-wiki, the Camunda lessons — the core matched
and the rest was inherited: a Drive mirror and a corpus-distillation pipeline in a system whose
thesis is *capture by gesture*; an ingest-time figure verifier in a system that also verifies at
answer time; a canon lane with PR ceremony in a system whose page contract says `status` is a
maturity axis, not a court; a fleet supervisor watching six scheduled jobs for one operator.

The verdict: **refactor-purge, not greenfield.** Keep the substrate and the hardening; remove
the organs.

## Decisions

**D1 — the canon lane goes whole.** `brain_propose`, the code-composed case file, the promotion
commit, `brain_promote`, `canon_proposals`, the branch-protection live suite, CODEOWNERS, and the
`canon-pr`/`contradiction` review kinds. The anti-design names it directly: the canon lane and any
PR ceremony go, because `status` is a maturity axis, not a court. What survives is `review_queue` /
`review_decide` over the kinds a human genuinely decides — an entity proposal and a parked
capture. (A third kind, the learning loop's candidates, survived this decision and left with the
loop itself — ADR 027.) `server/canon.py` became `server/review.py`: the same file with the lane
cut out, not a rewrite.

**D2 — ingest-time figure verification dies; answer-time verification is the whole of it.** The
ingest-side trust tiers go entirely: `gate_trace`, its briefs, the `untraced-figure` reason code,
`trust/verify.py`, the facts store, `query_metrics`, and the `verification:` frontmatter field the
fast lane used to stamp. The reasoning, stated once: at ingest the check taxes the model's own
prose with false positives (a real one is measured — the figure tokenizer did not know the `x`
multiplier, and a correct page-backed `2.3x` was refused; `answer/numbers.py` has since learned it,
on the answer side that survived), and it cannot catch the dangerous class anyway, because an
invented CLAIM passes every figure check. The reader's protection is what it
has always been: the verbatim source one click away, plus **the answer-time cites-or-refuses
check** — pure code, kept and now sole.

Consequence, accepted with eyes open: an invented figure CAN sit on a page. The mitigations are
the evidence link, the gardener, and human reading.

**A field nothing computes is not stamped — and nothing reads it either.** `verification` stays
in `SERVER_OWNED_KEYS` and in `brain_submit`'s declared-refusal parameters: an agent's draft still
has it STRIPPED and passing one is still an explicit error, because that enforcement is what makes
the deletion STICK. Nothing writes it back.

**Amended after the first cut.** This decision originally kept every READER — the `pages_index`
column, the `verification` filter, `rank.py`'s `failed`/`partial` demotions, the `verification=`
trust flag in the agent's search listing, the line in `read_page`'s page text — on the grounds
that moving a retrieval score was a change of its own and that leaving the readers alone kept the
measured baseline comparable. The ruling that closed it: *don't leave legacy of things that have
disappeared.* Every reader is gone. Three things make that safe rather than reckless:

1. `rank.py` never penalized ABSENCE, only explicit `failed`/`partial` — so post-purge pages were
   never being demoted, and the factor could only ever fire on pages written before the purge. A
   ranking input with no producer is a score nobody can reason about.
2. The measurement was re-run rather than argued: both goldens over the frozen corpus, after the
   removal. Retrieval `final` R@5 stayed **0.925**, the same two misses by name.
3. `pages_index` is never migrated — `store.init_schema` DROPs and recreates it — so the column
   leaves with no migration to write. Nothing BREAKS without a rebuild either: every read and
   write names its columns explicitly, so an already-deployed table carrying a leftover
   `verification text NOT NULL DEFAULT ''` keeps working untouched. The rebuild is what actually
   removes it, so run one on deploy (`stigmergy-index --rebuild`, or
   `gh workflow run index-rebuild.yml`) — an obligation, not a landmine.

Not done here: the contract trim in the knowledge repo. Its templates and its linter still name
the field and its pages still carry it — harmless, since nothing reads it.

**D3 — the fleet supervisor and its playbook go.** `stigmergy.supervisor`, `supervisor.yml`, the
librarian's playbook read-and-inject, the playbook approve CLI. It watched six scheduled jobs for
one operator and proposed advisory memory a human had to approve anyway; the human gate that made
the playbook *approved text* rather than accumulated machine opinion is exactly what it cost to
run. `ops/` reverts to ONE path-scoped writer, `stigmergy-entities`, which is what governed entity
birth needs and all it needs.

**D4 — the pipeline becomes a kernel, and the kernel is a library, not a layer.** Deleted: the
Drive mirror and the Slack-export connector, the ingest agents/worker/ops/main, the corpus
distillation stages, the trust layer, the facts/claims/versions extractors, the legacy view
generator, the graph build and its merge lane, `stigmergy.benchmark`. Kept and re-homed to
`stigmergy.kernel`: the PydanticAI dispatch (`llm`, `result`, `settings`), the page contract's
constants and scalar emitter (`page`), the frontmatter parser (`frontmatter`), the ACL resolver
(`acl`), the entity registry (`registry`, `normalize`), `fsutil`, and — the one thing kept for a
door that did not exist yet — the document converters and `vision_extract`, which the Drive door
now runs at the worker (ADR 028). The agent-orchestrated `ocr` tool shape they were kept beside
did not survive that door's own design (ADR 028 D5).

`stigmergy.kernel` may import nothing from this project, exactly like `stigmergy.text`. That is what
makes it safe to depend on from anywhere, and it is a test, not a comment.

**D5 — the learning loop is PARKED, not deleted.** Its code, its tests and its inbox kind stay,
dormant. What goes is the schedule: the distill step comes out of the nightly cron. It wakes when
real users exist. Its coverage stays green while it sleeps, deliberately — a parked subsystem
whose tests rot comes back broken.

**Superseded by ADR 027**, which deleted the loop outright two days later: a dormant subsystem is
a factory of tests pinning contracts every live refactor has to move, and that drag was being paid
before any user had earned the loop back.

**D6 — the digest's trial cron is closed: command-only.** The digest had been scheduled daily as a
trial, with the cadence itself under test and a review of a real staging week still pending. The
trial is closed early rather than run out: it broadcasts about a loop that no longer runs and a
corpus about to be replaced. `stigmergy-digest` stays a full operator command; its own watermark
means running it by hand next month still covers everything since the last post. The gardener's
daily cron is unchanged.

**D7 — the eval floor changes shape.** CI's two scoring steps go with their subjects: the
benchmark scorecard, and the curation/placement/trust/facts/graph dimensions — both scored over
GENERATED corpora, which is what made them cheap and what made them mean little. The instruments
that remain are the two REAL ones — golden retrieval and golden QA over the frozen corpus at
`evals/corpus/` — and they need an API key, so they are run by hand and appended to
`evals/history.ndjson`. CI stays keyless, and what it checks keylessly is the fixture's own
integrity.

## Consequences

- MCP tools 13 → **10**; `ask`'s bounded tool set 3 → **2** (`describe_entity` was restored later
  as the third).
- Code gates 9 → **8**: zone · binary-page · body-rewrite · secrets · pii · frontmatter ·
  contract · anchoring.
- A view is Timeline + Backlinks + Synthesis; the facts section and the verdict are gone, and
  the synthesis caption now says plainly that its figures are not machine-verified.
- Eight ADRs were deleted rather than superseded (002, 003, 004, 005, 009, 011, 019, 025); this
  record replaces the reasoning behind all of them, and their numbers are not reused.

## What this ADR does not decide

The knowledge repo is untouched here: emptying `wiki/` and trimming its templates and its linter
to the current contract are that repo's own work. So is re-arming the gates against a fresh
baseline once the corpus is rebuilt.
