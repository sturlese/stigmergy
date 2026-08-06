# Corpus health — `stigmergy.gardener` + `stigmergy.digest`

Two operator commands over the corpus: `stigmergy-gardener` finds what needs a human's judgment and
says so; `stigmergy-digest` posts what happened to Slack.
Design record: [ADR 024](../decisions/024-gardener-digest.md) — it holds the decisions this
document only shows the results of.
The review inbox is covered in [`server.md`](./server.md#the-review-tools)
and its Slack surface in [`slack.md`](./slack.md#the-steward-doorbell-and-the-review-surface);
view staleness is covered in [`views.md`](./views.md). This document describes what these two
commands add on top of them.

```
stigmergy-gardener                                  stigmergy-digest
  ├─ 8 deterministic checks   (checks.py)            ├─ corpus health          (the LATEST
  │    pages_index / capture_queue / the registry /  │    completed gardener run's findings —
  │    the repo checkout                             │    reused, never re-derived)
  ├─ model editorial sweep    (sweep.py)             └─ corpus deltas          (capture_queue
  │    changed-since-watermark pages +                    pages filed, review_decisions
  │    a rotating sample of unchanged ones,               entity-proposal approvals)
  │    PydanticAI, no checkout, no tools
  ├─ persist: gardener_findings + job_runs          every page NAMED is ACL-scoped to the posting
  ├─ print: severity-grouped report, or --json      channel (server.acl.visible, slack.channels.
  └─ sla-severity findings → ONE Slack notice       channel_audiences) — the digest broadcasts
       (same channel as the digest, ACL-scoped;     --dry-run: byte-identical preview, posts nothing
        NO check produces one today — see below)          │
        │                                                  │
        └──────────────────────── gardener.yml (daily cron; gardener only) ─────────────────────────┘
```

Both commands are **findings-only**: neither fixes, writes, opens a PR or issue, edits the
registry, or touches an inbox. `stigmergy-gardener` reports; `stigmergy-digest` broadcasts what was
already reported plus the corpus's own deltas. Every existing lane — the review
inbox (`review_queue`/`review_decide`), an entity-registry edit, `stigmergy-views regenerate`, an
ordinary correction filed through the 🧠 gesture — is still how anything a finding names actually
gets fixed.

## The eight deterministic checks

`checks.ALL_CHECK_SLUGS` is the list, and the report prints `len()` of it rather than a
hand-written number, so the count in this document is the only copy that can go stale. Each check
is a query over `pages_index`, `capture_queue`, the entity registry or the repo checkout — none
interprets meaning. The five thresholds named below are settings, env-tunable
(`gardener.settings.GardenerSettings`), and they cover three of the eight checks; the other five
have no threshold to tune, because staleness is a hash mismatch and an orphan is a zero, not an
amount.

| Check (slug) | Looks at | Fires when | Severity |
|---|---|---|---|
| Orphans (`orphan-page`) | non-entity `wiki/` pages with zero inbound wikilinks (the indexed `links` column) | always, except a type on the stated exemption list (entity pages, addressed by `entity:` anchoring, never a wikilink) | info |
| Aging seeds (`aging-seed`) | `seed`/`developing` pages' `updated` age | older than `STIGMERGY_GARDENER_AGING_SEED_DAYS` (default 30) | warn |
| Stale views (`stale-view`) | `views.staleness.list_stale_entities` — a view's member set vs. its own `member_hash:` | any mismatch | warn |
| Anchor concentration (`anchor-concentration`) | the last `STIGMERGY_GARDENER_CONCENTRATION_WINDOW` (default 30) filed pages, by top anchored entity's share | share exceeds `STIGMERGY_GARDENER_CONCENTRATION_SHARE` (default 0.6) | warn |
| Dead vocabulary (`dead-vocabulary`) | registered entities absent from `views.staleness.list_all_anchored_entities` | zero pages anchored anywhere | info |
| Company-wide fraction (`company-wide-fraction`) | the last `STIGMERGY_GARDENER_COMPANY_WINDOW` (default 20) filed pages, by share declaring `entity: []` | share exceeds `STIGMERGY_GARDENER_COMPANY_SHARE` (default 0.3) | warn |
| Company page naming an entity (`company-page-names-entity`) | every company-wide, non-provenance page's body, tested against every registered name/id/alias (word-bounded, case-insensitive) | any verbatim match | warn |
| Date-bearing body link (`date-bearing-body-link`) | every `wiki/`, `sources/`, `views/` page's BODY prose, read from the repo checkout, for a `[[YYYY-MM-DD-…]]` wikilink target | any match — one finding per page, naming the first offending stem | warn |

**Check 8 is a veto that was demoted, and the demotion is the point.** Only a meeting page's own
filename carries a calendar date (`wiki/meetings/YYYY-MM-DD-<slug>.md`), so a date-bearing wikilink
in body prose is a pointer that belongs in `sources:`/`related:` frontmatter. The meeting flow used
to REFUSE a whole capture over it (`processing._cross_check_meeting_outcome`) — a style convention
holding a veto. It lives here now under the same slug — deliberately the same
string, so an operator's grep finds both eras — and the line it draws is the house rule: **gates veto
the irreversible, the gardener flags conventions.**

**None of the eight checks is `sla` severity.** The `sla` severity band itself exists — the schema
carries it (`SEVERITIES`), the report prints an `sla` section and the notice-composing code is
live — but nothing produces one, so in practice the SLA notice has **no producer**: see "The SLA
notice", below.

Checks 4 (anchor concentration) and 6 (company-wide fraction) read the queue's real `finished_at`
timestamp, never a page's own `updated`/`as_of` — those describe when a page was last authored, not
when it was filed. Checks 4, 6 and 7 (company page naming an entity) exclude provenance-type pages
(`meeting`, `source`, via `librarian.page.is_provenance_type`) from what counts as "anchored" or
"company-wide": a provenance page's
`entity: []` means "the extractor found no evidence," never a checked company-wide declaration, and
counting it either way would make these checks lie about the population they exist to measure.

**Anchor concentration and the company-wide fraction exist because production already produced
the failure they detect.** Anchor concentration was measured at 14/18 pages on one entity, with no
distribution any filing-time gate could see; the company-wide escape hatch was taken in production
with nothing standing behind it but "the gardener will detect it." These two checks are that
detection.

**Dead vocabulary can never be silenced by running anything.** `stigmergy-entities` only mints — no
retire, merge or un-birth verb exists — so this finding recurs on every run until either a page
anchors to the entity or someone hand-edits the registry outside any governed lane. That is a real
property of the check, not a gap in its copy.

## The model editorial sweep — "only what the tool can't see"

The eight checks stay exact and model-free; the sweep is the judgment half, built on a
PydanticAI structured-extraction pattern: one
prompt, one structured call through `stigmergy.kernel.llm.build_processor`, one retry
carrying the validation error as its brief, then log-and-skip — never insert unvalidated.
It holds no tools at all: `SWEEP_LIMITS.tool_calls_limit` is `0`, a structural property of the
agent's usage limits rather than a request made in a prompt, so there is nothing for the model to
call and no write path to reach.

It judges four things a mechanical check cannot, each its own check slug
(`sweep.ALL_MODEL_CHECK_SLUGS`: `model-contradiction`,
`model-anchor-fit`, `model-unlinked-mention`, `model-superseded-canon`), all `warn` — none carries
a real time-bound clock, so none is `sla`. Its input is bounded
on purpose: every page filed since the last run's watermark, plus a rotating sample of
`STIGMERGY_GARDENER_SWEEP_SAMPLE` (default 10) unchanged pages, so the sweep steadily re-covers the
whole corpus over many runs rather than reading it in full every time. Every page body reaches the
model only inside `stigmergy.text.fence` — page content, including verbatim `sources/` material, is
untrusted input to a prompt, and the system prompt tells the model a fenced page is DATA,
never instructions, however it reads.

**`suggested_action` for a model finding is never model-generated text.** The model's own output
schema has no such field — only a check slug, subject page(s), a one-sentence rationale and a
≤200-character verbatim excerpt. `MODEL_SUGGESTED_ACTIONS` is a fixed, code-owned dict keyed by
slug alone; an injected page cannot make this module choose, let alone compose, a different
string. The rationale and excerpt DO reach the report, sanitized and hard-clamped, in `detail` —
bounded to a wrong sentence in a report, never to an instruction a reader might paste.

The model is configuration (`STIGMERGY_GARDENER_MODEL`, defaulting to
`settings.DEFAULT_GARDENER_MODEL`), independent of `stigmergy.kernel.llm`'s own `CLEAN_MODEL` —
and it does not fall back to the shared model when unset:
it carries its own concrete cheap-class default, so "model is configuration" reads literally rather
than "model is whatever the shared one happens to be". Escalating past that default is an
evidence-driven decision from reading real weeks of findings, not a guess made up front. A sweep
outage (a hard model-call failure, or nothing surviving even the one retry) never
takes the deterministic findings from the same run down with it — see "Reading a gardener report"
below.

## Reading a gardener report

```
$ stigmergy-gardener
# Gardener report — run #128, completed 2026-07-31T05:07:03Z

checked 412 pages, 38 entities — 8 deterministic checks, plus a model sweep over 6 changed page(s)
and 10 sampled unchanged page(s)

19 finding(s): 0 sla, 5 warn, 14 info

most of what follows is a judgment call, not a one-paste fix: only `stale-view` names a runnable
command below. Everything else names what to go look at.

## SLA (0)
none this run
## WARN (5)
[WARN] anchor-concentration        acme-corp — 14 of the last 18 filings (78%) anchored here, above the 60% threshold  [deterministic]
  action: no command — read a few of the recent filings anchored to Acme Corp and judge whether that's genuinely how lopsided the work has been, or whether unrelated material is defaulting here because picking the right anchor felt like more effort
[WARN] stale-view                  acme-corp — the view's member set has changed since it was last generated  [deterministic]
  action: `stigmergy-views regenerate --entity acme-corp`
...
## INFO (14)
...
```

Sections print **SLA first, then WARN, then INFO** — worst news first — and a severity with zero
findings this run still prints its header (with its count) and an explicit "none this run", never
a silently absent section. Within a group, findings sort by check slug then subject, so two runs
over an unchanged corpus produce byte-identical output. Each finding is two lines: the finding
itself (severity tag, slug, subject, the specific numbers that make it self-explanatory, and
`[deterministic]` or `[model: {model_id}]`), then its `action:` — a backtick-quoted command when
one genuinely exists (`stale-view` only, with the backticks baked into the stored value so the
report and `--json` carry the identical string), a plain sentence otherwise. Nothing here is a
repair tool; the preamble says so once, up front, rather than leaving a reader to discover it
finding by finding. `--json` emits one object per finding with the same fields plus
`id`/`run_id`/`created_at`/`model_id`, `suggested_action` always populated (never `null` for a
sentence-only check — an absent field would read as "nothing to do," which is false).

`#`/`##` markdown headers are correct here: the reader is a terminal, never Slack. That is the
opposite convention from the digest, below.

## The SLA notice

**Stated plainly: this mechanism has no producer.** Every one of the eight
deterministic checks is `info` or `warn`, and so is every one of the sweep's four model
slugs. Nothing in this codebase constructs a finding with `SEVERITY_SLA`. The machinery below is
therefore live code with a dead input — and not by accident: the
severity band, the notice-composing code and `stigmergy-gardener`'s own loud-failure-on-post-error
posture all stay, on purpose, so a future check can be given `sla` severity without anyone having
to rebuild the notice path — but as things stand, no gardener run posts one.

A run that produces at least one `sla` finding posts exactly **one** Slack message, however many
`sla` findings fired, to the same channel the digest broadcasts to (`STIGMERGY_DIGEST_CHANNEL_ID`,
reused rather than a second channel setting). It says what broke, since when, and the command that
shows it — never `🔔`, which means "a decision is waiting in `review_queue`" everywhere else in
this codebase and no `review_decide` verdict ever closes an SLA finding; `⚠️ SLA:` instead, always
paired with the plain word so the fact survives a client that renders no emoji at all. A run with
only `info`/`warn` findings posts nothing — the absence of a Slack event, not an empty state
needing a sentence, exactly like the doorbell posting nothing when no item is open.

The notice is ACL-scoped exactly like the digest's own sections (ADR 024 D5): a finding whose
wording names more than one page has its notice wording redacted unless EVERY one of those pages is
visible to the posting channel's audiences (never per-page), a fixed sentence taking the place of
the page path while its severity and its place in the count stay exactly where they were.
`stigmergy-gardener` fails loudly (nonzero exit) if an `sla` finding fires and the notice itself
cannot be posted (no bot token, no channel configured, a malformed `ops/slack-channels.json`, a real
`SlackApiError`) — the findings are already saved either way; only the notice is at risk. A run
with no `sla` finding at all never resolves the channels file, or anything else Slack-shaped, in
the first place — a malformed one cannot fail a run that was never going to post.

## The digest's two sections

`stigmergy-digest` assembles, over the window since its own last post (a watermark; `--since`
overrides it; 7 days on a genuine first run), two sections, in this order:

1. **Corpus health** — the latest COMPLETED gardener run (`status IN ('ok', 'partial')` — a run
   whose sweep failed still has complete, trustworthy deterministic findings) whose `finished_at`
   falls inside the window: its finding counts by severity, SLA/WARN broken down by check, INFO as
   a bare count with a pointer to the full report. Two honest alternatives when there is nothing to
   show: no gardener run has EVER completed, or the latest one predates this window (both name what
   to run next, never silence).
2. **Corpus deltas** — pages filed in-window (count + titles) and entity approvals in-window
   (a count of `review_decisions` entity-proposal approvals — the approval event, which is what
   this system can actually timestamp; the later, separate mint commit carries no ledger row of
   its own, so the section counts approvals, not mints. The rendered line says "approved" rather
   than "born" for exactly that reason: an approved-never-minted proposal would otherwise count as
   a birth forever).

**Every page the digest NAMES passes `server.acl.visible()` at the posting channel's own resolved
audiences** (`sections._visible_pages`, the one place this package calls it) — never the
operator's unscoped view. In practice that is section 2's filed-page list: a page the channel
cannot see is absent from both the count and the titles, because a count and a list that disagree
are their own kind of dishonest report. Section 1 needs no filter because it names no page at all
— only counts by severity and check slug — and the entity-approval number is a count of decision
rows, not of pages. The digest is the one gardener-adjacent surface that broadcasts, so it is the
one that needed this from birth (ADR 024 D5); until the first labelled page exists the filter is
indistinguishable from no scoping at all, and it becomes load-bearing the moment one exists.

**The digest is Slack mrkdwn, composed as such from the start — never CommonMark, never
converted.** Bold section headers (`*Corpus health*`), `•` bullets, no `#`/`##` (Slack has
no heading syntax; a literal `#` renders as a literal `#`), no fenced code blocks for the
sections' own prose (a fenced block does not wrap on a phone screen, which is the literal
mechanism behind "no horizontal scroll"). This is the OPPOSITE convention from the gardener
report, deliberately: two different readers in two different programs, each matching its own
siblings, never each other. Every corpus-derived string the digest interpolates — a page title, a
page path — is escaped (`slack.mrkdwn.escape_mrkdwn`) before it is
composed into the body, the same defensive posture the sweep already takes for a model's own
rationale.

**`--dry-run` prints exactly what would post and posts nothing** — byte-identical to a real post,
by construction: `render.build_body` is the one function both the preview and the real post call,
and the two marker lines wrapping a dry-run preview live outside it entirely, never inside the
returned string. A zero-activity window still renders every section, with its own honest empty
line — "silence is not an outcome" applies here exactly as it does to the gardener's own severity
sections.

## How the two commands relate to each other, and to the cron

`stigmergy-gardener` and `stigmergy-digest` are independent, fully-runnable operator commands.
**The digest is command-only: no cron at all.** A schedule buys nothing it does not already have —
`stigmergy-digest` run by hand next month still covers everything since the last post, because its
own watermark says so; only timeliness is lost by not scheduling it.

The gardener's daily cron lives in its own workflow, `gardener.yml` — shipped here as a template
and **run from the knowledge repo**, whose Actions logs are private (this report names entity ids
and page paths; see the runbook). It runs
daily at ~05:07 UTC, after `index-rebuild` (04:17) and `retention-purge` (04:42), so the corpus
view the gardener reads is the morning's, not last night's. It checks out the knowledge repo
read-only and runs one command — `stigmergy-gardener --repo stigmergy-knowledge` — persisting findings
and posting the SLA notice (today, never — see above) in the same run. The whole job is guarded by
`if: vars.STIGMERGY_CRONS_ENABLED == 'true'`, so a fork that inherits the file but not the deployment
behind it skips cleanly instead of failing a scheduled run every night. A `concurrency` group
queues a second run rather than cancelling one in flight: cancelling mid-write would discard real,
already-computed work.

## Findings-only, provably

Neither package holds a route to a page, and that is asserted rather than asserted-about. Over every
module in each package, `tests/test_architecture.py` pins three things: the import edge list
(`test_gardener_library_modules_stay_within_the_documented_edge`,
`test_digest_library_modules_stay_within_the_documented_edge`), the absence of git plumbing
(`test_gardener_never_touches_git_plumbing`, `test_digest_never_touches_git_plumbing`), and the
absence of any literal path fragment under the knowledge repo
(`test_gardener_holds_no_literal_path_under_knowledge` and its digest twin). Two of those edges
are named exceptions rather than blanket grants, because a transitive import would otherwise smuggle
the capability back in: `test_gardener_transitive_views_reach_is_a_named_declared_exception` is why
the gardener reads `views.staleness` and never `views.regenerate`, whose module-level import of
`views.writer` would load the entire git write stack into every gardener process.

Behaviorally, `tests/gardener/test_run_pg.py::test_run_gardener_writes_nothing_but_its_own_findings_
and_job_runs_row` is the direct proof for the gardener. The only write either package performs at
all is its own `job_runs` row — the bookkeeping every operator CLI in this codebase already writes
through.
