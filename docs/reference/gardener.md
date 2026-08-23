# Corpus health — `stigmergy.gardener`

One operator command over the corpus: `stigmergy-gardener` finds what needs a human's judgment and
says so.
What a finding's path to zero looks like is [`repair.md`](./repair.md). This document describes
what this command adds on top of it.
Code map:
[`src/stigmergy/gardener/index.md`](../../src/stigmergy/gardener/index.md).

```
stigmergy-gardener
  ├─ 10 deterministic checks  (checks.py)
  │    pages_index / capture_queue / the registry /
  │    the repo checkout — no model, no tools,
  │    no provider key
  ├─ persist: gardener_findings + job_runs
  └─ print: severity-grouped report, or --json
        │
        └────────── the worker's night shift (daily, idle branch) ──────────────────
```

The command is **findings-only**: it does not fix, write, open a PR or issue, or edit the
registry. It reports, and NOTHING acts on what it reports: a finding is a list for a person to
read. Every other lane — a person's own `brain_delete`, an ordinary correction filed through the 🧠
gesture, a capture that brings a page up to date — is how anything a finding names actually gets
fixed.

**A finding's road to zero starts in a different package.** The librarian worker reads the latest
completed gardener run on its idle branch and, for the findings this vocabulary can answer,
derives a concrete change, validates it against a real checkout, proves it through the nine gates
and pushes it — nobody is asked, and the reading happens afterwards, from the diff the ledger
stored. None of that reaches back here: this package still detects and fixes nothing, and it
neither imports nor calls the one that repairs.
The narrative is [repair.md](./repair.md), decided in
[`repair.md`](./repair.md).

## The ten deterministic checks

`checks.ALL_CHECK_SLUGS` is the list, and the report prints `len()` of it rather than a
hand-written number, so the count in this document is the only copy that can go stale. Each check
is a query over `pages_index`, `capture_queue`, the entity registry or the repo checkout — none
interprets meaning. The five thresholds named below are settings, env-tunable
(`gardener.settings.GardenerSettings`), and they cover three of the ten checks; the other seven
have no threshold to tune, because an orphan is a zero, not an amount.

| Check (slug) | Looks at | Fires when | Severity |
|---|---|---|---|
| Orphans (`orphan-page`) | non-entity `wiki/` pages with zero inbound wikilinks (the indexed `links` column) | always, except a type on the stated exemption list (entity pages, addressed by `entity:` anchoring, never a wikilink) | info |
| Aging seeds (`aging-seed`) | `seed`/`developing` pages' `updated` age | older than `STIGMERGY_GARDENER_AGING_SEED_DAYS` (default 30) | warn |
| Anchor concentration (`anchor-concentration`) | the last `STIGMERGY_GARDENER_CONCENTRATION_WINDOW` (default 30) filed pages, by top anchored entity's share | share exceeds `STIGMERGY_GARDENER_CONCENTRATION_SHARE` (default 0.6) | warn |
| Dead vocabulary (`dead-vocabulary`) | registered entities anchored by no page in `index.corpus.load_pages` | zero pages anchored anywhere | info |
| Company-wide fraction (`company-wide-fraction`) | the last `STIGMERGY_GARDENER_COMPANY_WINDOW` (default 20) filed pages, by share declaring `entity: []` | share exceeds `STIGMERGY_GARDENER_COMPANY_SHARE` (default 0.3) | warn |
| Company page naming an entity (`company-page-names-entity`) | every company-wide, non-provenance page's body, tested against every registered name/id/alias (word-bounded, case-insensitive) | any verbatim match | warn |
| Date-bearing body link (`date-bearing-body-link`) | every `wiki/` and `sources/` page's BODY prose, read from the repo checkout, for a `[[YYYY-MM-DD-…]]` wikilink target | any match — one finding per page, naming the first offending stem | warn |
| Entity placeholder body (`entity-placeholder-body`) | every `wiki/entities/` page's BODY, read from the repo checkout, for a line that is wholly angle-marked (`<…>`) — the entity template's unwritten spans | any such line survives — the identity exists and says nothing about itself | info |
| Anchored to a superseded entity (`anchored-to-superseded-entity`) | knowledge pages (`wiki/`, minus the entity zone) whose `entity:` names an id whose own entity page declares `superseded_by:` | any such anchor — the page's history sits on the retired side of an applied merge | info |
| Link to a narrower page (`link-to-narrower-page`) | every resolved outbound link in `pages_index.links` | a link whose TARGET does not `flows_into` its SOURCE — the linking page's readers see a title they cannot open | warn |

**The date-bearing check is a convention, and that is exactly why it is a finding and not a veto.**
A page name that opens with a calendar date is a dated record, so a `[[YYYY-MM-DD-…]]` target in
body prose is a pointer that belongs in `sources:`/`related:` frontmatter rather than in a sentence.
Nothing about it is irreversible, so nothing refuses a capture over it:
`checks.check_date_bearing_body_links` walks every page in the three content zones and raises one
`warn` per offending page, naming the first offending stem. The line it draws is the house rule:
**gates veto the irreversible, the gardener flags conventions.**

**`link-to-narrower-page` is the one link a model can no longer write and a person still can.**
Every page a model reads while
writing is scoped to what that page may cite, so the librarian cannot LEARN of a page it may not
link to. What remains is a name the capture's own material supplies — a human writing a restricted
page's title into open material — the same act
as posting it in a public channel — and the brain reports it rather than policing it. Nothing is
repaired: narrowing the linking page would punish one person's capture for what somebody else
restricted, demoting the link to plain text leaves the title (which is the whole of what a link
leaks), and deleting it edits somebody's words. The finding names the pair and stops.

**`anchored-to-superseded-entity` is the residual of a merge that was applied while the elective repair loop existed, counted where it accrues.** A merge moved the absorbed entity's aliases and never its name, so the absorbed id stays registered and a capture filed later spelling that name anchors to the retired identity. Nothing merges identities any more, so this count only shrinks. The population excludes the entity zone and the machine zone on purpose: the absorbed page's own self-anchor is BY DESIGN and would otherwise be a permanent, unfixable finding per merge. The count is exactly zero the moment a merge lands; what it measures afterwards is the accumulation the filing-time fix (issue #77's other half) exists to end.

**`entity-placeholder-body` names a page nobody has written yet.** An entity
page written today is born with a body — `entities.birth.render_page` refuses one that says nothing
about the entity and strips the template's stubs
 — so what this check finds is a page
born under the older contract, carrying `ops/templates/entity.md`'s angle-marked placeholders
verbatim, or one whose body is blank below its title. Nothing counted those pages before this
check — the orphan check exempts entity pages by type, and no other check reads a body — so an
identity with no content was invisible to every health pass. The finding is `info` and nothing acts
on it: a capture about the entity grows the page, which is what `entity_updates` is for. The rule
is deliberately literal — a body line that is wholly wrapped in angle brackets — so a one-line HTML
element (`<details>`) reads as a placeholder. That false positive is accepted: the finding is
`info`, and the repair it invites is bounded by the same gates every other diff passes.

Its literal-ness is also its gap, and the gap is wide and currently unclosed: a body somebody
WROTE that says nothing — `Cofers is a company we work with.` — carries no angle markers, is not
blank, and passes this check and every other one. A model pass used to judge exactly that
(`model-empty-entity-body`) and produced zero findings in three weeks; it went with the rest of the
model half ("Why there is no model half" below). Nothing reports that page today.

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
identity: `wiki/entities/` is outside what a deletion may touch, by construction. So this finding
recurs on every run until either a page anchors to the entity, or its page
leaves `wiki/entities/` in the knowledge repo by hand. That is a real property of the check, not a
gap in its copy.

## Why there is no model half

There was one, and it was measured rather than argued about. Three passes ran nightly against the
author's own deployment for three weeks: an editorial sweep over changed-plus-sampled pages, an
empty-body pass over every entity page, and an identity pass over the registry behind that zone.
Between them they produced **nine findings** — seven `model-unlinked-mention`, two
`model-anchor-fit`. `model-contradiction`, `model-empty-entity-body` and `model-duplicate-entity`
produced **zero, ever**.

Three of those four zero-producing slugs fed a repair road, so the shape of the spend was: a model
pass reads pages, emits a finding, persists it, and a LATER pass hands a second model the same
pages to propose a fix. Two model calls a night, for a finding class nothing had ever produced.
Over the same period the deterministic checks produced 168 findings, including 20 distinct
orphans and 14 distinct empty entity bodies.

[`docs/DESIGN.md`](../DESIGN.md) states the criterion: anything not forced by multi-user has to
demonstrate USE, and passing tests is not evidence of use. So the passes went, and with them the
`model-*` check vocabulary, the gardener's own model setting and the per-pass ceilings, the
`partial` run status, and the worker's preflight refusal for a gardener provider key.

What it cost, stated plainly rather than left to be discovered:

- **`entity-placeholder-body`** keeps its deterministic form (a body still carrying the template's
  angle markers, or blank below its title) and loses the judged one. A body somebody wrote that
  says nothing specific about the entity is no longer reported.
- The **merge road** lost its only detector, and then the road itself: the elective repair loop it
  belonged to was removed. See [`repair.md`](./repair.md).
- The **additive road** keeps `orphan-page` and loses `model-unlinked-mention` and
  `model-contradiction` — the two that produced findings.

A `gardener_findings` row a retired pass wrote is still readable and still says what it was: the
`source` column keeps `'model'` in its vocabulary and the `model_id` column keeps its value, so
such a row reads back labelled rather than silently relabelled `deterministic`.

## Reading a gardener report

```
$ stigmergy-gardener
# Gardener report — run #128, completed 2026-07-31T05:07:03Z

checked 412 pages, 38 entities — 10 deterministic checks

19 finding(s): 5 warn, 14 info

nothing below is a one-paste fix: every `action:` names what to go look at, or says who is already
taking care of it. This report runs no command and suggests none.

## WARN (5)
[WARN] anchor-concentration        acme-corp — 14 of the last 18 filings (78%) anchored here, above the 60% threshold
  action: no command — read a few of the recent filings anchored to Acme Corp and judge whether that's genuinely how lopsided the work has been, or whether unrelated material is defaulting here because picking the right anchor felt like more effort
...
## INFO (14)
...
```

Sections print **WARN first, then INFO** — worst news first — and a severity with zero
findings this run still prints its header (with its count) and an explicit "none this run", never
a silently absent section. Within a group, findings sort by check slug then subject, so two runs
over an unchanged corpus produce byte-identical output. Each finding is two lines: the finding
itself (severity tag, slug, subject and the specific numbers that make it self-explanatory —
there is no source tag, because every check that runs is deterministic and a label with one
possible value is noise on every line), then its `action:` — a plain sentence, always. **No
finding names a command**, because a message containing a command is an executable promise and
none of these has one to keep. Nothing here is a repair tool; the preamble says so once, up front, rather than leaving a
reader to discover it finding by finding: an `action:` line names what to go look at, never a fix
this report performed and never one anything else will. `--json` emits one object per finding
with the same fields plus
`id`/`run_id`/`created_at`/`source`/`model_id`, `suggested_action` always populated (never `null`
for a sentence-only check — an absent field would read as "nothing to do," which is false).
`source` is `"deterministic"` and `model_id` `""` on everything a run produces now; both keys stay
in the payload because dropping a key breaks a consumer parsing it, and because a row a retired
model pass wrote still reads back through the same shape.

**The corpus line has no second half.** It used to carry a clause per model pass, and a clause
for each one that did NOT complete, because a report that read like a normal run while a whole
pass never happened would be the same silent miss these checks exist to end, one layer up. Nothing
in a run is optional now — it completes or it raises — so there is no such clause to write.

`#`/`##` markdown headers are correct here: the reader is a terminal.

## Two severities, and nothing that pages anybody

The vocabulary is `info` and `warn` (`gardener.schema.SEVERITIES`), printed in
`SEVERITY_ORDER` and spelled off those two names by every reader — the terminal report and the
console's severity chips. Every check picks one of the two explicitly, at its own definition,
rather than inheriting a default.

**`stigmergy-gardener` notifies nobody, and holds nothing to notify with.** There is no severity
that pages a person and no Slack credential in a gardener process: the package imports no Slack
gateway, no channels file and no ACL predicate, and `tests/test_architecture.py` pins that as its
import edge. A finding reaches a person one way — the console's Gardener page — where they read
it and decide, rather than being woken up by a message.

## How the command relates to the night shift

`stigmergy-gardener` is a fully-runnable operator command, and
**the gardener's daily run happens inside the librarian worker**, on its idle branch, at
`STIGMERGY_LIBRARIAN_GARDEN_AT` (default 05:07 UTC) — see
the night shift and the
[operator runbook](./operator-runbook.md). Three properties come from living there rather than in
a scheduled GitHub Actions run:

- **It cannot delay a filing.** A pass never starts while a capture is waiting in the queue, and
  yields between units. A cron had no way to know.
- **It cannot silently stop.** Due-ness is read from the pass's own last `job_runs` row, so a
  worker that restarts at 05:08 does not garden twice and one that was down all night does not
  garden at 23:00. The failure mode this replaced was a job guarded by a repository variable: unset
  meant every run was green-and-skipped, and "the crons stopped running" looked exactly like "the
  crons are fine".
- **The report stays private without an arrangement.** This report names entity ids and page paths.
  Running it inside the deployment means there is no Actions log to keep private in the first
  place, which is what the "run it from the knowledge repo" rule used to buy.

It needs no Slack credential, and it holds no push credential either: it persists findings and
returns them. NOTHING answers them automatically: the elective repair loop that used to
([repair.md](./repair.md)) was removed after applying five repairs in three weeks of daily use, and
what a finding gets now is a person reading it on the console.

## Findings-only, provably

The package holds no route to a page, and that is asserted rather than asserted-about. Over every
module in it, `tests/test_architecture.py` pins three things: the import edge list
(`test_gardener_library_modules_stay_within_the_documented_edge`), the absence of git plumbing
(`test_gardener_never_touches_git_plumbing`), and the absence of any literal path fragment under
the knowledge repo (`test_gardener_holds_no_literal_path_under_knowledge`). One of those edges
is a named exception rather than a blanket grant, because a transitive import would otherwise smuggle
the capability back in: `test_gardener_transitive_librarian_reach_is_a_named_declared_exception`
names, module by module, exactly what loads when `gardener.checks` does. Its heaviest edge today is
`index.corpus`, a parser — but the pin stays, because a chain reaching a commit-and-push module is
precisely what it caught once already.

Behaviorally, `tests/gardener/test_run_pg.py::test_run_gardener_writes_nothing_but_its_own_findings_
and_job_runs_row` is the direct proof. The only write the package performs at
all is its own `job_runs` row — the bookkeeping every operator CLI in this codebase already writes
through.
