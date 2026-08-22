# Corpus health — `stigmergy.gardener` + `stigmergy.digest`

Two operator commands over the corpus: `stigmergy-gardener` finds what needs a human's judgment and
says so; `stigmergy-digest` posts what happened to Slack.
Design record: [ADR 024](../decisions/024-gardener-digest.md) — it holds the decisions this
document only shows the results of.
What a finding's path to zero looks like is [`repair.md`](./repair.md); view staleness is covered
in [`views.md`](./views.md). This document describes what these two commands add on top of them.
Code maps, one per command:
[`src/stigmergy/gardener/index.md`](../../src/stigmergy/gardener/index.md) and
[`src/stigmergy/digest/index.md`](../../src/stigmergy/digest/index.md).

```
stigmergy-gardener                                  stigmergy-digest
  ├─ 10 deterministic checks  (checks.py)            ├─ corpus health          (the LATEST
  │    pages_index / capture_queue / the registry /  │    completed gardener run's findings —
  │    the repo checkout                             │    reused, never re-derived)
  ├─ model editorial sweep    (sweep.py)             └─ corpus deltas          (capture_queue
  │    changed-since-watermark pages +                    + repairs: pages filed, the
  │    a rotating sample of unchanged ones,               identities those filings introduced,
  │    PydanticAI, no checkout, no tools                  and the repairs that landed)
  ├─ model empty-body pass    (sweep.py)
  │    EVERY entity page in the checkout,
  │    batched, minus the ones already
  │    reported as placeholders — no tools
  ├─ model identity pass      (sweep.py)
  │    EVERY registered entity behind that
  │    same zone, in ONE call — a pair split
  │    across batches is invisible; no tools
  ├─ persist: gardener_findings + job_runs          every page NAMED is ACL-scoped to the posting
  └─ print: severity-grouped report, or --json      channel (server.acl.visible, slack.channels.
        │                                           channel_audiences) — the digest broadcasts
        │                                           --dry-run: byte-identical preview, posts nothing
        │                                                  │
        └──────────────────────── gardener.yml (daily cron; gardener only) ─────────────────────────┘
```

Both commands are **findings-only**: neither fixes, writes, opens a PR or issue, or edits the
registry. `stigmergy-gardener` reports; `stigmergy-digest` broadcasts what was already reported
plus the corpus's own deltas. Every other lane — the worker's repair pass, its view sweep, a
person's own `brain_delete`, an ordinary correction filed through the 🧠 gesture — is how anything
a finding names actually gets fixed.

**A finding's road to zero starts in a different package.** The librarian worker reads the latest
completed gardener run on its idle branch and, for the six findings this vocabulary can answer,
derives a concrete change, validates it against a real checkout, proves it through the nine gates
and pushes it — nobody is asked, and the reading happens afterwards, from the diff the ledger
stored. None of that reaches back here: this package still detects and fixes nothing, and it
neither imports nor calls the one that repairs.
The narrative is [repair.md](./repair.md), decided in
[ADR 039](../decisions/039-governed-repair-loop.md) and amended by
[ADR 044](../decisions/044-the-capture-is-the-approval.md) D2.

## The ten deterministic checks

`checks.ALL_CHECK_SLUGS` is the list, and the report prints `len()` of it rather than a
hand-written number, so the count in this document is the only copy that can go stale. Each check
is a query over `pages_index`, `capture_queue`, the entity registry or the repo checkout — none
interprets meaning. The five thresholds named below are settings, env-tunable
(`gardener.settings.GardenerSettings`), and they cover three of the ten checks; the other seven
have no threshold to tune, because staleness is a hash mismatch and an orphan is a zero, not an
amount.

| Check (slug) | Looks at | Fires when | Severity |
|---|---|---|---|
| Orphans (`orphan-page`) | non-entity `wiki/` pages with zero inbound wikilinks (the indexed `links` column) | always, except a type on the stated exemption list (entity pages, addressed by `entity:` anchoring, never a wikilink) | info |
| Aging seeds (`aging-seed`) | `seed`/`developing` pages' `updated` age | older than `STIGMERGY_GARDENER_AGING_SEED_DAYS` (default 30) | warn |
| Stale views (`stale-view`) | `views.staleness.list_stale_entities` — a view's member set vs. its own `member_hash:`, AND the backlinks it would render vs. its own `backlink_hash:` | either mismatch | warn |
| Anchor concentration (`anchor-concentration`) | the last `STIGMERGY_GARDENER_CONCENTRATION_WINDOW` (default 30) filed pages, by top anchored entity's share | share exceeds `STIGMERGY_GARDENER_CONCENTRATION_SHARE` (default 0.6) | warn |
| Dead vocabulary (`dead-vocabulary`) | registered entities absent from `views.staleness.list_all_anchored_entities` | zero pages anchored anywhere | info |
| Company-wide fraction (`company-wide-fraction`) | the last `STIGMERGY_GARDENER_COMPANY_WINDOW` (default 20) filed pages, by share declaring `entity: []` | share exceeds `STIGMERGY_GARDENER_COMPANY_SHARE` (default 0.3) | warn |
| Company page naming an entity (`company-page-names-entity`) | every company-wide, non-provenance page's body, tested against every registered name/id/alias (word-bounded, case-insensitive) | any verbatim match | warn |
| Date-bearing body link (`date-bearing-body-link`) | every `wiki/`, `sources/`, `views/` page's BODY prose, read from the repo checkout, for a `[[YYYY-MM-DD-…]]` wikilink target | any match — one finding per page, naming the first offending stem | warn |
| Entity placeholder body (`entity-placeholder-body`) | every `wiki/entities/` page's BODY, read from the repo checkout, for a line that is wholly angle-marked (`<…>`) — the entity template's unwritten spans | any such line survives — the identity exists and says nothing about itself | info |
| Anchored to a superseded entity (`anchored-to-superseded-entity`) | knowledge pages (`wiki/`, minus the entity zone) whose `entity:` names an id whose own entity page declares `superseded_by:` | any such anchor — the page's history sits on the retired side of an applied merge | info |

**`stale-view` is the one check with an actor outside this package, and that is a division of
labour rather than a gap.** The librarian worker's periodic view sweep converges `views/` to the
corpus on its own interval, over a population that is a SUPERSET of this check's (see
[`views.md`](./views.md)): this check names entities whose EXISTING view has drifted, the sweep
also creates views that were never written and removes orphaned ones. So a `stale-view` finding is
a report of something already scheduled to be fixed, and its `action:` command is what an operator
runs when they do not want to wait for the interval. The gardener itself still writes nothing but
findings — it holds no git plumbing, by construction and by architecture test.

**The date-bearing check is a veto that was demoted, and the demotion is the point.** Only a meeting page's own
filename carries a calendar date (`wiki/meetings/YYYY-MM-DD-<slug>.md`), so a date-bearing wikilink
in body prose is a pointer that belongs in `sources:`/`related:` frontmatter. The meeting flow used
to REFUSE a whole capture over it (`processing._cross_check_meeting_outcome`) — a style convention
holding a veto. It lives here now under the same slug — deliberately the same
string, so an operator's grep finds both eras — and the line it draws is the house rule: **gates veto
the irreversible, the gardener flags conventions.**

**`anchored-to-superseded-entity` is the residual of an applied merge, counted where it accrues.** A merge moves the absorbed entity's aliases and never its name, so the absorbed id stays registered and a capture filed later spelling that name anchors to the retired identity — and the repair loop can never re-propose the pair (its `content_key` is a permanent decision). The population excludes the entity zone and the machine zones on purpose: the absorbed page's own self-anchor and its member-set-of-one view are BY DESIGN and would otherwise be two permanent, unfixable findings per merge. The count is exactly zero the moment a merge lands; what it measures afterwards is the accumulation the filing-time fix (issue #77's other half) exists to end.

**`entity-placeholder-body` is one of the two checks with a repair kind of its own.** An entity
page written today is born with a body — `entities.birth.render_page` refuses one that says nothing
about the entity and strips the template's stubs
([ADR 042](../decisions/042-an-entity-is-born-written.md)) — so what this check finds is a page
born under the older contract, carrying `ops/templates/entity.md`'s angle-marked placeholders
verbatim, or one whose body is blank below its title. Nothing counted those pages before this
check — the orphan check exempts entity pages by type, and no other check reads a body — so an
identity with no content was invisible to every health pass. The finding is `info` and its repair
is `entity-body` ([repair.md](./repair.md)): the worker's repair pass drafts that page's body from
the pages anchored to the entity, proves the diff through the nine gates and pushes it, and the
draft is read afterwards on the ledger. The rule
is deliberately literal — a body line that is wholly wrapped in angle brackets — so a one-line HTML
element (`<details>`) reads as a placeholder. That false positive is accepted: the finding is
`info`, and the repair it invites is bounded by the same gates every other diff passes.

Its literal-ness is also its gap, and the gap is wide: a body somebody WROTE that says nothing —
`Cofers is a company we work with.` — carries no angle markers and passes this check, and every
other deterministic one. That half is the model pass's, `model-empty-entity-body`, described under
"The model passes" below; the two are disjoint by construction and share one repair.

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

**Dead vocabulary can never be silenced by running anything.** Nothing in this platform retires an
identity: `wiki/entities/` is outside what a deletion may touch, by construction, and an
`entity-alias` merge keeps the absorbed page (marked `superseded_by:`) and therefore its registry
entry. So this finding recurs on every run until either a page anchors to the entity, or its page
leaves `wiki/entities/` in the knowledge repo by hand. That is a real property of the check, not a
gap in its copy.

## The model passes — "only what the tool can't see"

The ten checks stay exact and model-free; the model half is the judgment one, built on a
PydanticAI structured-extraction pattern: one
prompt, one structured call through `stigmergy.kernel.llm.build_processor`, one retry
carrying the validation error as its brief, then log-and-skip — never insert unvalidated.
It holds no tools at all: `SWEEP_LIMITS.tool_calls_limit` is `0`, a structural property of the
agent's usage limits rather than a request made in a prompt, so there is nothing for the model to
call and no write path to reach.

**There are THREE passes, and they share that discipline and nothing else.** They differ in their
prompt, their population and the check slugs each may emit — and that last difference is enforced
rather than promised: `_validate` takes its allowed slug set as a parameter, so no pass can emit
another's vocabulary however a page's text argues for it. It takes the SHAPE the same way: only the
identity pass sets a floor on how many pages a finding names, because only that check is a claim
about a pair.

**The editorial sweep** judges four things a mechanical check cannot, each its own check slug
(`sweep.ALL_MODEL_CHECK_SLUGS`: `model-contradiction`,
`model-anchor-fit`, `model-unlinked-mention`, `model-superseded-canon`), all `warn`. Its input is
bounded
on purpose, on every axis: the NEWEST `STIGMERGY_GARDENER_SWEEP_CHANGED_CEILING` (default 30)
pages filed since the last run's watermark, plus a rotating sample of
`STIGMERGY_GARDENER_SWEEP_SAMPLE` (default 10) unchanged pages, each body clamped to
`sweep.MAX_SWEEP_PAGE_CHARS` — so the prompt is settings-shaped, never corpus-shaped, even on a
first run or after a cron outage, and a failed night re-presents a BOUNDED population rather than
one its own frozen watermark keeps growing. A changed page past the ceiling is deferred, counted
(`changed_deferred`, plus a skip reason naming the knob) and never lost: it joins the unchanged
pool, where the rotation reaches it. The pass is deliberately NOT batched instead — its checks
are about PAIRS, and a batch boundary would silently decide which contradictions are ever
visible.

**The empty-body pass** judges one thing, `model-empty-entity-body` (`info`): an entity page whose
body is WRITTEN and says nothing about that entity in particular — no specific facts, nothing named,
no links to the pages that would state them. It is the judgment twin of the deterministic
`entity-placeholder-body`, which only ever sees a body still carrying the template's literal angle
markers; a body somebody typed in thirty seconds passes every deterministic check there is.

Its population is COVERAGE, not sampling, and that is a decision rather than an inherited default:
entity pages are a bounded set (a few dozen), and a sampled judgment check would leave pages
unjudged while the report read as "nothing wrong". It reads the entity zone of the repo CHECKOUT
from the run's single walk of it — the same list `entity-placeholder-body` judged moments earlier,
so the two talk about one page set at one instant — batches it
`STIGMERGY_GARDENER_EMPTY_BODY_BATCH` (default 8) pages per call, and judges every page up to
`STIGMERGY_GARDENER_EMPTY_BODY_CEILING` (default 150). The ceiling is a spend bound for a corpus
that grew hundreds of entity pages, and when it binds the run RECORDS what it deferred in
`job_runs.stats`, as a skip reason and as a log warning: a ceiling that truncated in silence would
read as a clean bill of health for the pages it never opened.

**A page still carrying literal placeholders is removed from that population before the model is
asked**, so one page produces one finding across the two checks by construction — not by a
downstream de-duplication a later re-ordering could defeat, and not two repair proposals for one
page on one night.

**The identity pass** judges one thing, `model-duplicate-entity` (`warn`): two of the brain's
registered entities are the SAME real-world entity, registered twice. A legal-form or qualifier
variant of one name, a former name beside a current one, a regional or transliterated spelling, an
abbreviation and what it abbreviates. `warn` rather than the empty-body pass's `info`, and the
difference is what the finding costs to ignore: an empty body is a page that says nothing, while
two identities for one company SPLIT the anchoring — each timeline is a fraction of the truth and
entity-first retrieval degrades with nothing anywhere reporting that it has.

Its population is the same zone walk, read as REGISTRY ENTRIES: each page is placed onto the id the
registry knows it by (the `slugify(title)` id contract first, the alias matcher as the fallback),
and a page the registry does not register is excluded and counted — an unregistered page is not an
entry, and this check compares entries. It carries `STIGMERGY_GARDENER_DUPLICATE_ENTITY_CEILING`
(default 120) and no batch size at all, and the absence is the decision: **the question is about a
PAIR, and a pair whose two halves fell in different batches is invisible to every batch.** So the
whole population rides ONE call, each entry contributing a bounded number of characters, and below
two registered entities no model is asked at all — a registry that cannot hold a pair is recorded as
such rather than reported as clean.

**Sharing a word is not the finding, and that is the half that keeps this from becoming noise.**
`Cofers` and `Cofers Legal` may well be a parent and its law firm; merging them would silently
rewrite what somebody's pages are about. What makes a pair a finding is agreement in what the two
pages SAY — the same activity, the same people, the same products — not resemblance between two
strings, and the prompt says so and says to flag nothing when the pages do not say enough to tell.
A missed duplicate costs a search some recall; a wrong one moves a page's whole history onto the
wrong company.

**That walk is a confinement boundary.** What it reads leaves the machine, so it refuses a
symlinked page and every symlinked path component above one, refuses to open a file above a fixed
byte cap, and counts each refusal into `job_runs.stats` rather than dropping it — a page missing
from both checks AND from the population count would let the run report full coverage of a
population it silently excluded. Each body also contributes a bounded number of characters to the
prompt: the entity zone is written by hand, so nothing else bounds what one page can cost, and a
body that says nothing about its entity says so in its opening lines or nowhere.

Every page body reaches the
model only inside `stigmergy.text.fence` — page content, including verbatim `sources/` material, is
untrusted input to a prompt, and each pass's system prompt tell the model a fenced page is DATA,
never instructions, however it reads.

**`suggested_action` for a model finding is never model-generated text.** The model's own output
schema has no such field — only a check slug, subject page(s), a one-sentence rationale and a
≤200-character verbatim excerpt. `MODEL_SUGGESTED_ACTIONS` is a fixed, code-owned dict keyed by
slug alone; an injected page cannot make this module choose, let alone compose, a different
string. The rationale and excerpt DO reach the report, sanitized and hard-clamped, in `detail` —
bounded to a wrong sentence in a report, never to an instruction a reader might paste.

All three passes run on the same model. It is configuration (`STIGMERGY_GARDENER_MODEL`, defaulting to
`settings.DEFAULT_GARDENER_MODEL`), independent of `stigmergy.kernel.llm`'s own `CLEAN_MODEL` —
and it does not fall back to the shared model when unset:
it carries its own concrete cheap-class default, so "model is configuration" reads literally rather
than "model is whatever the shared one happens to be". Escalating past that default is an
evidence-driven decision from reading real weeks of findings, not a guess made up front.

An outage of ANY pass (a hard model-call failure, or nothing surviving even the one retry) never
takes the deterministic findings from the same run down with it, and never takes another pass down
either — they fail independently and are reported independently. Any failure commits `partial`
rather than `ok`, because a run's status is the one place an operator learns a whole model pass did
not happen. The identity pass loses its WHOLE population when it fails, not a remainder: it is one
call, so there is no half of it that survived.

That makes the status an **aggregate**, and no watermark is derived from it: the editorial sweep's
`since` and sample rotation continue from the most recent run whose OWN `stats.sweep.error` is
empty, `ok` or `partial` alike. Reading `status = 'ok'` alone would pin the sweep's window at the
last flawless run every night one of the OTHER passes failed, growing its single unbatched prompt
until it took the sweep down too — and re-judging the same rotating sample forever. See "Reading a
gardener report" below.

## Reading a gardener report

```
$ stigmergy-gardener
# Gardener report — run #128, completed 2026-07-31T05:07:03Z

checked 412 pages, 38 entities — 10 deterministic checks, plus a model sweep over 6 changed page(s)
and 10 sampled unchanged page(s), and a body sweep over 24 entity page(s), and an identity sweep
over 38 registered entity(ies)

19 finding(s): 5 warn, 14 info

nothing below is a one-paste fix: every `action:` names what to go look at, or says who is already
taking care of it. This report runs no command and suggests none.

## WARN (5)
[WARN] anchor-concentration        acme-corp — 14 of the last 18 filings (78%) anchored here, above the 60% threshold  [deterministic]
  action: no command — read a few of the recent filings anchored to Acme Corp and judge whether that's genuinely how lopsided the work has been, or whether unrelated material is defaulting here because picking the right anchor felt like more effort
[WARN] stale-view                  acme-corp — the view no longer matches the corpus — its member set or the backlinks it cites have changed since it was last generated  [deterministic]
  action: no command — the librarian worker regenerates it on its next idle pass; a view still listed here after several is worth checking the worker's job runs for
...
## INFO (14)
...
```

Sections print **WARN first, then INFO** — worst news first — and a severity with zero
findings this run still prints its header (with its count) and an explicit "none this run", never
a silently absent section. Within a group, findings sort by check slug then subject, so two runs
over an unchanged corpus produce byte-identical output. Each finding is two lines: the finding
itself (severity tag, slug, subject, the specific numbers that make it self-explanatory, and
`[deterministic]` or `[model: {model_id}]`), then its `action:` — a plain sentence, always. **No
finding names a command any more.** The last one that did was `stale-view`, and what replaced its
command is the truth about it: the librarian worker's view sweep converges that zone and there is
no second road, so telling an operator to run something would be an executable promise nothing
keeps. Nothing here is a repair tool; the preamble says so once, up front, rather than leaving a
reader to discover it finding by finding. The worker's repair pass reading these same findings
afterwards does not soften that: an `action:` line still names what to go look at, never a fix this
report performed. `--json` emits one object per finding
with the same fields plus
`id`/`run_id`/`created_at`/`model_id`, `suggested_action` always populated (never `null` for a
sentence-only check — an absent field would read as "nothing to do," which is false).

**The corpus line names every pass that did not happen, one clause each.** A model pass that failed
says so there rather than simply omitting its numbers ("the model sweep did NOT complete this
run", "the entity-body sweep did NOT complete this run"), the two are named separately because
they fail separately, and the run ceiling's deferred count appears there too. A report that read
like a normal run while a whole model pass never happened — or while a bound quietly skipped half
the entity zone — would be the same silent miss these checks exist to end, one layer up.

`#`/`##` markdown headers are correct here: the reader is a terminal, never Slack. That is the
opposite convention from the digest, below.

## Two severities, and nothing that pages anybody

The vocabulary is `info` and `warn` (`gardener.schema.SEVERITIES`), printed in
`SEVERITY_ORDER` and spelled off those two names by every reader — the terminal report, the
digest's corpus-health section, the console's severity chips. Every deterministic check and every
model slug picks one of the two explicitly, at its own definition, rather than inheriting a default.

**`stigmergy-gardener` notifies nobody, and holds nothing to notify with.** There is no severity
that pages a person and no Slack credential in a gardener process: the package imports no Slack
gateway, no channels file and no ACL predicate, and `tests/test_architecture.py` pins that as its
import edge. A finding reaches a person two ways — `stigmergy-digest`'s corpus-health section, and
the console's Gardener page — and what answers one is the worker's repair pass
([ADR 044](../decisions/044-the-capture-is-the-approval.md)), not a human woken up by a message.

## The digest's two sections

`stigmergy-digest` assembles, over the window since its own last post (a watermark; `--since`
overrides it; 7 days on a genuine first run), two sections, in this order:

1. **Corpus health** — the latest COMPLETED gardener run (`status IN ('ok', 'partial')` — a run
   whose sweep failed still has complete, trustworthy deterministic findings) whose `finished_at`
   falls inside the window: its finding counts by severity, WARN broken down by check, INFO as
   a bare count with a pointer to the full report. Two honest alternatives when there is nothing to
   show: no gardener run has EVER completed, or the latest one predates this window (both name what
   to run next, never silence).
2. **Corpus deltas** — three facts: pages filed in-window (count + titles), entities born
   in-window, and repairs applied in-window. The last two are COUNTS only.

   A birth is a filing: the librarian's report names the identities its own capture introduced
   (`report.entities_born`), so the number is the sum of
   `jsonb_array_length(report -> 'entities_born')` over the window's FILED captures
   ([ADR 044](../decisions/044-the-capture-is-the-approval.md)). There is no ledger to read and no
   second table that could disagree with the commits, and the line reads `N entities born` because
   that is now exact — every counted birth is a page in the repo.

   A repair is counted off the ledger the worker writes, `applied` rows ONLY, grouped by KIND
   (`edits`, `entity-body`, `delete`, `entity-alias`, sorted so two weeks can be compared). A
   `failed` row belongs on the console beside the sentence that refused it, where somebody can act
   on it — not in a weekly post to a channel that cannot.

   Neither the identities nor the repaired pages are NAMED, and that is the same decision twice: a
   page a channel may not see would be named through its entity, or through the repair that edited
   it, and the count is the honest half.

**Every page the digest NAMES passes `server.acl.visible()` at the posting channel's own resolved
audiences** (`sections._visible_pages`, the one place this package calls it) — never the
operator's unscoped view. In practice that is section 2's filed-page list: a page the channel
cannot see is absent from both the count and the titles, because a count and a list that disagree
are their own kind of dishonest report. Section 1 needs no filter because it names no page at all
— only counts by severity and check slug — and neither does the rest of section 2: the
entities-born number is a count taken off the filings' own reports, and the repairs number a count
off the ledger, by kind. Which is exactly why they are counts. The digest is the one gardener-adjacent surface that
broadcasts, so it is the one that needed this from birth (ADR 024 D5); until the first labelled page exists the filter is
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

## How the two commands relate to each other, and to the crons

`stigmergy-gardener` and `stigmergy-digest` are independent, fully-runnable operator commands.
**The digest is command-only: no cron at all.** A schedule buys nothing it does not already have —
`stigmergy-digest` run by hand next month still covers everything since the last post, because its
own watermark says so; only timeliness is lost by not scheduling it.

The gardener's daily cron lives in its own workflow, `gardener.yml` — shipped here as a template
and **run from the knowledge repo**, whose Actions logs are private (this report names entity ids
and page paths; see the runbook). It runs
daily at ~05:07 UTC, after `index-rebuild` (04:17) and `retention-purge` (04:42), so the corpus
view the gardener reads is the morning's, not last night's. It checks out the knowledge repo
read-only and runs one command — `stigmergy-gardener --repo stigmergy-knowledge` — which persists
findings and prints the report, and needs no Slack credential to do it. The whole job is guarded by
`if: vars.STIGMERGY_CRONS_ENABLED == 'true'`, so a fork that inherits the file but not the deployment
behind it skips cleanly instead of failing a scheduled run every night. A `concurrency` group
queues a second run rather than cancelling one in flight: cancelling mid-write would discard real,
already-computed work.

**What answers these findings is not a fourth workflow.** The repair pass runs inside the librarian
worker, on its own interval and only when its queue is idle — because it PUSHES, and a scheduled
Actions run holding the App's private key is a write credential in a public runner's environment
([repair.md](./repair.md), [ADR 044](../decisions/044-the-capture-is-the-approval.md) D2). Its
coupling to this report is a watermark rather than an offset: it answers the latest COMPLETED
gardener run, once, and a pass that finds no completed run — or nothing answerable in one — records
that it did nothing and carries on.

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
