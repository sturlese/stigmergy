# The meeting distiller — `stigmergy-meeting` + the librarian's meeting flow

A transcript dropped by hand becomes, unattended, one atomic commit of pages: the source page(s)
holding the transcript verbatim (permanent evidence), a `meeting` page (provenance), and N
`decision` pages (knowledge, anchored, dated). The ordinary fast lane files exactly one page per
capture; the meeting flow is a SECOND flow through the same queue and the same worker, filing a
page **set** instead. Design record:
[ADR 020](../decisions/020-meeting-distiller.md) — it holds the
decisions this document only shows the results of (a second flow rather than a branch, atomic
page-set filing, per-page anchoring, the provenance stamp the fast lane's own stamp omits, and flow
policy passed into `GateContext` closed by default).

The front half — [`capture.md`](./capture.md) and
[ADR 014](../decisions/014-capture-queue-and-attribution.md) — and
the back half's ordinary path — [`librarian.md`](./librarian.md) and
[ADR 015](../decisions/015-librarian.md) — are unchanged and are not repeated here.

```
stigmergy-meeting drop <transcript> --title <t> --date <YYYY-MM-DD>
                       --submitted-by <email>            (or $STIGMERGY_MEETING_OPERATOR_EMAIL;
                       [--attendees a,b]                  refused by name without one — attribution
                                                          is never guessed)
  │  validates locally -> uploads to the SAME evidence store every capture uses
  │  -> queue.submit, exactly ONE capture_queue row, kind="meeting"
  │  (no MCP door: schema.MCP_SUBMIT_KINDS = ("raw", "page") only)
  ▼
capture_queue row, kind="meeting" (claimed by the SAME librarian worker, same fenced claiming)
  │
  ├─ _pre_agent (SHARED with the ordinary flow): dedup levels 1-2, secrets/PII over the material
  │
  └─ librarian.processing.process_meeting_item  (process_item's sibling, not a branch inside it)
       │  ephemeral worktree, agent fed the transcript AND the drop metadata as fenced
       │  UNTRUSTED DATA (the metadata labelled HINTS, never instructions; the resolved entity
       │  registry stays outside the fence — server-derived, from governed birth) — the
       │  meeting-distiller brief instead of the librarian skill, read from the SAME worktree
       │  at base.sha
       │
       │  agent returns ONE structured outcome (no page-writing tool at all);
       │  _write_meeting_pages then has CODE write the SET, all paths checked before the first byte:
       │    N >= 1 source-page parts   sources/meetings/<slug>[-p<n>].md  (verbatim transcript — see below)
       │    exactly 1 meeting page     wiki/meetings/YYYY-MM-DD-<slug>.md  (provenance only)
       │    N >= 0 decision pages      wiki/decisions/<slug>.md   (each its OWN anchor)
       │
       ├─ _stamp_meeting: PER-PAGE server stamp (source parts get the provenance group;
       │     meeting page gets no entity/acl; each decision page gets its OWN entity:/acl)
       ├─ gates.run_gates(ctx) over the WHOLE diff, ctx scoped to the meeting flow's own lane
       │     (GateContext.write_prefixes/creatable_types/extra_folder_types/page_declared/
       │     stamped_by_path/provenance_pages/edits_allowed=False — every DEFAULT reproduces the
       │     unattached fast lane byte-for-byte; a caller widens them on the ctx, never globally)
       ├─ _cross_check_meeting_outcome: the SET's own atomicity contract (below)
       │
       │   any veto survives one corrective retry?  →  refuse the WHOLE capture, zero pages
       │   gates pass?  →  ONE commit, all pages, App-bot identity, Submitted-by: trailer
       ▼
filed: report.filed_meeting names every page path and every decision's anchor outcome
```

**Read the diagram's second box twice: the agent does not write pages.** Its allow-list is
`agent.MEETING_ALLOWED_TOOLS` — exactly one tool, `Write` — with a `PreToolUse` hook
(`confine_writes`) refusing every target but its own outcome file, and it returns the decisions,
their anchors and the drafted prose as DATA. `processing._write_meeting_pages` builds and writes
every page of the set from that one structured object. Everything downstream — the stamp, all eight
gates, the cross-check, the commit — is unchanged and unaware. The earlier shape, where the agent
made Write/Edit/Read/Glob/Grep calls inside the worktree and separately DECLARED what it had
written, carried two independent claims that could disagree; collapsing them removed the
disagreement rather than policing it. `Read`/`Glob`/`Grep`/`Edit` are not merely unused: they are on
`MEETING_DISALLOWED_TOOLS` beside the ordinary agent's own denials.

## Why filing is atomic

One capture, one commit, one page **set**, or nothing. A meeting page that links a decision page
which does not exist, or a decision page with no provenance record behind it, are both worse than
parking the whole capture. `process_meeting_item` never commits
until every page in the set has passed every gate; a terminal veto on any single page (or on the
set's own shape — a page outside the set, a missing meeting page, a mismatched decision count)
refuses the capture with **no page committed at all**. This is why `_cross_check_meeting_outcome` exists
separately from the ordinary flow's `_cross_check_outcome` ("exactly one page created"): the ordinary
rule stays exactly what it was, unchanged, and still governs every non-meeting capture; the meeting
flow gets its own, wider contract instead of a conditional bolted onto the old one.

`_cross_check_meeting_outcome` checks, over the diff the gates are about to judge:

- no page outside the set — source-page parts, one meeting page, any number of decision pages
  (`unexpected-page`);
- at least one source page (`source-page-count`) and exactly one meeting page
  (`meeting-page-count`);
- the outcome's decision count and the decision pages actually written agree
  (`decision-count-mismatch`).

And one more, raised by `_write_meeting_pages` before it writes anything at all:

- a computed page path that already exists in the repo (`existing-page-collision`) — the whole set's
  paths are checked against `gitcmd.tracked_paths` first, so a collision refuses with nothing
  written rather than being discovered mid-write. The repair is a different meeting or decision
  title, which is why it takes the ordinary corrective-retry road.

**This list used to be twice as long, and code-as-sole-author is why it is not.** `duplicate-decision-
declared`, `decision-set-mismatch`, `source-path-mismatch` and `meeting-path-mismatch` all existed
to catch an ADVERSARIAL author: an agent that declared three decisions and wrote two, or claimed a
`source_page_path` that did not match the file on disk. With code as the sole author, building every
page from the SAME structured outcome this function reads, those disagreements are not merely
unlikely — they are unconstructible. `meeting-links-mismatch` survived a while longer as
double-entry bookkeeping and went out on the same argument: `_build_meeting_page` writes
the `## Decisions` section from the identical `decision_stems` list it names the decision pages
with, so the two cannot diverge without `decision-count-mismatch` catching the construction bug
first. What is left is a self-check on code's own construction, and `source-page-count`'s `< 1` arm
is explicitly kept as one — it cannot fire by construction either.

**`date-bearing-body-link` left this list too, and did not die.** The convention is real — only the
meeting page's filename carries a calendar date, so a `[[YYYY-MM-DD-…]]` target in body prose is a
pointer that belongs in `sources:`/`related:` frontmatter. But refusing a whole
capture over a style convention is the wrong side of the line this codebase draws: gates veto the
irreversible, the gardener flags conventions. It is now a gardener check under the same
slug — see [`gardener-digest.md`](./gardener-digest.md#the-eight-deterministic-checks). The
`_build_decision_page` builder still avoids producing one, by citing the transcript through
`sources:` rather than a body wikilink.

## Per-page anchoring

A meeting about two customers yields two `decision` pages belonging to different entities — one
outcome-wide anchoring declaration (the ordinary flow's single `anchoring.kind`/`anchoring.entities`)
cannot express that. The meeting flow's outcome instead declares anchoring **per decision**
(`outcome.decisions[i].anchoring`), and `gates.GateContext.page_declared` carries it forward:
`{page path: {"page_type": ..., "anchoring": {...}}}`, one entry per new page. `gate_anchoring`
detects a populated `ctx.page_declared` and switches to `_per_page_anchoring`, which asks the SAME
anchoring question `gate_anchoring` always asked — `entity` (with `entities`, each resolving through
the registry) or `company` (with a written, non-empty reason) — but **once per page that declares
one**. The source and meeting pages carry no `"anchoring"` key in their `page_declared` entry at all
(they are provenance, never a knowledge destination — see below), so they are never asked the
question; only decision pages are.

`processing._stamp_meeting` stamps each decision page's `entity:`/`acl:` independently, from that
page's own declared anchoring — the same `gates.resolve_entity_ids` call and the same
defence-in-depth ("stamp `[]` rather than a partial resolution when the anchoring gate is about to
veto this pass anyway") the ordinary flow's `_stamp` applies, run once per decision page instead of
once per capture. The map from a written page's path back to its anchoring comes from
`_write_meeting_pages`' own plan (`decisions_by_path`), not from a field the outcome carries: paths
are code-computed here, so the lookup is built from what code itself just wrote.

**One field is stamped differently from every other page in this system:** `as_of` is the MEETING's
own date (the operator's `--date`), never today's. A decision taken in a meeting is `as_of` that
meeting, and time-sensitive ranking would otherwise read a transcript dropped a month late as
current.

Every ordinary (non-meeting) capture's `ctx.page_declared` stays empty, which is what keeps
`gate_anchoring` asking its original, single-outcome question for every non-meeting run.

## Provenance stamping on the source page, and why the ordinary stamp does not write it

The source page under `sources/meetings/` is the meeting flow's only `sources/` write, and it is
validated under a different frontmatter group from every fast-lane page: the machine/provenance
group (`content_hash`, `extracted_at`, `tier`) — those fields belong to a provenance zone, not to a
fast-lane capture. `page.stamp_server_fields` (the function every OTHER new page in this system is
stamped with) does not write them, on purpose: a fast-lane page is never itself a piece of
machine-extracted evidence, so giving it a `content_hash`/`extracted_at` would claim a provenance
chain that page does not have. The source page IS exactly that evidence, so it gets a sibling
function instead, `page.stamp_source_fields`, which writes `content_hash` (`sha256:<hex>` of the
same archived material bytes `capture.schema.material_digest` hashed at drop time — recomputed here
from the bytes this run verified against, so the page's own claim and the evidence-store key can
never disagree), `extracted_at` (this run's timestamp), `tier` (`"1"`, always — a meeting
transcript is a direct recording, not second-hand or AI-generated; the tier is about provenance, not
about how noisy the speech-to-text is) and the part's own `id:` (see the split contract below).
`status`/`as_of`/`submitted_by` are stamped here too, for the same accountability reason every
fast-lane page carries them. `entity`/`acl` are deliberately absent from the signature: a source
page under `sources/meetings/` is provenance for the whole capture, not itself anchored — it is the
DECISION pages that must anchor, not the transcript.

**Code writes that page, verbatim, and splits it when it is long.**
`processing._build_source_parts` takes the archived material and emits it byte for byte — the
transcript is already in the agent's prompt, so having a model copy 863 lines back out was the
largest cost and latency item the first real walk found, and a correctness risk besides: a model
copying a transcript can drop, reorder or normalise a line, and the "ground truth" page would then
be a lossy copy of the ground truth. A body over `kernel.page.MAX_BODY_LINES` is split into N
cross-linked parts (`Continues in [[…]]` / `Continued from [[…]]`) under the same page-as-chunk
contract described in [brain-page-contract.md](./brain-page-contract.md), with one adaptation: a
wikilink target must be a filename, so the FILENAME stem carries the `-p<n>` suffix. The part
identity is **declared, not inferred from that filename**: the builder computes `page_id` itself
(`<stem>` for part 1, `<stem>#p<n>` after) and `stamp_source_fields` writes it as `id:`, quoted
because an unquoted `#` starts a YAML comment. `index.corpus` prefers the declared `id:` over the
stem, so the chain collapse keys on a fact; the older `-p<n>` filename inference survives only as
belt-and-braces for pages filed before the field existed. Every part is then stamped by
`_stamp_meeting` exactly as the one-part case always was, so what the builder drafts for
`content_hash`/`tier`/`status`/`as_of`/`submitted_by` is overwritten and never trusted (a drafted
`id` is stripped the same way — `page.SERVER_OWNED_KEYS` names it). This is THE source-page writer
in the codebase: `source_kind`/`tags`/`url` are parameters with no caller-favouring defaults, and
the fast lane's Slack and Drive attachments call the same function rather than growing a second one.

`gates.gate_frontmatter`'s `FORBIDDEN_PAGE_KEYS` (`owner`, `id`, `content_hash`, `tier`,
`extracted_at`) still refuses every one of these on every OTHER page — a decision or meeting page
declaring `content_hash:` is forged, exactly like a fast-lane page declaring `owner:` always was. The
one exemption is `ctx.provenance_pages`, a `frozenset` of paths the gate is TOLD carry legitimate
provenance fields (never inferred from the diff's own shape — a gate is told a fact, it never
interprets one). `processing._stamp_meeting` populates it with the capture's own `sources/meetings/`
source-page parts; the fast lane's own source attachment populates it the same told-not-inferred
way for its `sources/slack/`/`sources/drive/` parts
([librarian.md](./librarian.md#the-source-attachment-a-parameter-never-a-third-flow)), and a
capture with no attachment at all leaves it empty, so `FORBIDDEN_PAGE_KEYS`'s check is unchanged
there. A duplicate declaration of `content_hash`/`tier`/`extracted_at` (the capture's own forged
line beside the server's stamped one) is refused by the same duplicate-key backstop `owner`/`entity`
already had (`page.duplicate_top_level_keys`, checked against `PROVENANCE_PAGE_KEYS` too).

## The meeting page is provenance, never a knowledge destination

The meeting page carries no anchor of its own (`gates._per_page_anchoring` never asks it the
anchoring question — see above) and `processing._stamp_meeting` stamps it with an empty `entity:`
and no ACL. Four sections, and code writes three of them from the structured outcome:
`## Attendees`, `## Action Items` (a checklist on the page, per the existing template — never
standalone pages), and `## Decisions`, linking every decision page this capture filed, 1:1. The
fourth, `## Notes`, is the agent's own drafted prose and the only place on this page it writes.
**"Content arguing for or explaining a decision belongs on a decision page, not the meeting page"
is stated in the brief and enforced nowhere mechanically**, deliberately:
"is this sentence provenance or is it knowledge" is a judgment
about MEANING, not a structural property a gate can decide without becoming an LLM content
reviewer. Unlike the date-in-wikilink convention (a MECHANICAL property — a wikilink
target's own spelling, which is why the gardener can still test it deterministically), there is no
shape here for any check to test. The mitigation is the
same one every figure in this system leans on, since nothing verifies content at write time: the
submitter reads the report and the filed page.

## The brief↔gates two-sided contract

The distiller's brief (`.claude/skills/meeting-distiller/SKILL.md`) lives in the **knowledge repo**
— the sibling checkout this repository's own tooling assumes at `../stigmergy-brain` relative to this
repo's root (`librarian.config.REPO_DEFAULT`). It is read from the
SAME ephemeral worktree the ordinary flow's `SKILL.md` is (`agent.read_meeting_brief`, checked out
`--detach` at `base.sha` by `gitcmd.ephemeral_worktree`), never by a second `base_inputs` reader —
`base_inputs.MEETING_BRIEF_RELPATH` exists only to name the path for the contract test. Wiring a
second reader would mean the agent's PROMPT was built from one read while the file it is TOLD to
open was a different one: two sources of truth for one brief. The worktree mechanism is what makes
"read at the base commit, injected by the platform, never loaded by the agent from a live checkout"
hold here by the same means the ordinary skill already uses, not by a second one.

**The brief and the gates are one contract, edited on both sides.** Dropping a requirement from a
gate while the brief still promises it enforced routes an ordinary capture to "the librarian broke".
`tests/librarian/test_meeting_brief_contract.py` is the build's own check: `RULE_TABLE`, a
hand-maintained table of (a verbatim phrase from the brief) ↔ (a marker proving the code actually
implements that rule — a finding code, a field, a behaviour), asserted in BOTH directions: the brief
phrase must still be present in the brief text, and the code marker must still be present in
`gates.py`/`processing.py`. It is not a formal proof (English prose is not parsed into rules), but
it catches the failure mode directly: a rule silently dropped from either side while the other still
claims it.

**It runs against a FROZEN copy of the brief, and that split is the point.** Reading the live brief
out of the sibling checkout meant the whole table SKIPPED wherever that checkout is absent — CI,
a fresh clone, every push that gates a branch, i.e. exactly where guarding matters. So it is two
halves now, each named for what it proves: the table reads the vendored copy at
`tests/librarian/fixtures/repo/.claude/skills/meeting-distiller/SKILL.md`, so every code-side marker
is checked on every run everywhere; and a separate drift test asserts that copy is byte-identical to
the knowledge repo's own, skipping only when the sibling repo is missing. Neither is sufficient
alone — a vendored copy proves "the copy and the code agree", which is weaker than the claim — but
together they say the code is checked against a pinned contract on every push and the pin is checked
against reality on every local run. `FROZEN.md` beside the copy records the 40-character commit sha
it was taken at, without which "is this copy behind or ahead?" has no answer; and an anti-vacuity
test asserts the copy is non-trivial and the table has not gone thin, because the mechanism that
replaced a skip has to be as hard to silently disable as the skip was. Retired rows stay in the
table as `REMOVED:` records, in place, each naming why it went and where the code-side rule is still
covered.

## The vetoes this flow can produce

Beyond the ordinary flow's gates (zone, binary-page, body-rewrite, secrets, pii, frontmatter,
contract, anchoring — all eight, run over the whole diff, per `gates.ALL_GATES`), the meeting flow's
own `outcome`-gate findings, all from `processing._cross_check_meeting_outcome` and its helpers,
are:

| Finding | What it catches |
|---|---|
| `unexpected-page` | a page created outside the set's three destinations |
| `source-page-count` | zero pages under `sources/meetings/` — a self-check on code's own construction, unreachable in practice |
| `meeting-page-count` | not exactly one page under `wiki/meetings/` |
| `decision-count-mismatch` | the outcome describes N decisions and a different number of decision pages was written — again, code disagreeing with itself |
| `existing-page-collision` | a computed path for this set already exists in the repo; raised by `_write_meeting_pages` before anything is written |

There is also **one gate finding only this flow can produce**: `zone/meeting-edit-refused`. This
flow builds its `GateContext` with `edits_allowed=False`, because it has no additive-edit mechanism
at all (`edits.apply_declared` is never invoked here — the meeting page's own `## Decisions` section
is what links the set together). A status-`M` entry in this diff therefore has no producer inside
the flow, so `gate_zone` refuses it outright rather than composing a corrective brief for work
nothing here could have done. It is `repairable=False` for the reason every member of that class is:
the refused diff is preserved only on the terminal path, and a retry's `reset --hard` would erase
the only evidence of an unexplained write into the worktree.

Routing a refused capture: `_refuse_meeting` mirrors the ordinary flow's cause-based routing
(`rejected` for a secret, a PII match, or steering with a traceable injection category;
`rejected_malformed_frontmatter`/`rejected_forged_field` for a frontmatter-only veto;
`triage` via `_uncreatable_type` for a governed type the fast lane cannot mint) and adds one
meeting-specific park: when EVERY surviving veto is a per-decision anchoring-unresolved finding (one
per decision page that could not anchor), the whole capture parks in `triage`, naming every
unresolved name at once — never one page filed and another silently dropped. Anything else mixed in
(a binary-page veto, an unrelated dead link, an unexplained zone veto) is not provably the whole
story and falls through to `failed`, exactly like the ordinary flow's own `_unanchorable` posture.

**One correction to that routing.** The steering branch tests
`f.repairable`, so an UNREPAIRABLE zone finding is excluded from it even when the agent declared an
injection category alongside. `meeting-edit-refused`, `body-rewrite` and `unreadable-edit` all mean
"a modification with no producer this flow's agent could have been" — a system fault. Routing one of
them to `rejected_steering` on the coincidence of a declared category would name the submitter's
possibly unrelated capture as the cause, and bury the real signal under a steering report an
operator then investigates for the wrong reason. A *repairable* zone finding — `outside-lane`,
`type-not-creatable` — still routes to steering exactly as before, because there the diff really
could be the agent acting on injected text.

## Ask-back: several names, one question

A transcript can name more than one unresolved entity in a single capture — a call naming two
customers and an unregistered project code. `processing._triage_meeting` / `_ask_or_park_multi`
still spend the SAME one-ask-per-capture budget (`capture_queue.asked_at`) the ordinary flow's
`_ask_or_park` does, but name every unresolved name in one question rather than the first one found.
`capture.schema.SITUATION_NAMES_KEY` (a JSON list, additive beside the unchanged
singular `SITUATION_NAME_KEY`) carries the plural case on the parked row; `entities.situations.
subjects_of` is the per-name reader `stigmergy-entities show` uses to print one `stigmergy-entities
approve` command PER unresolved name, each checked and runnable independently. See
[`../../src/stigmergy/entities/index.md`](../../src/stigmergy/entities/index.md) and
[`operator-runbook.md`](./operator-runbook.md#draining-parked-rows) for the steward-facing and
operator-facing halves of this, respectively.

### …and the re-file after that park does not re-read the transcript

The park→approve→requeue loop does something expensive here: a real
walk parked a meeting on one unregistered name, and the re-file — a fresh agent read of the same
transcript — produced a *different, thinner* distillation, judged "faithful, and incomplete" by the
person who had attended. The system had discarded a good distillation because of an anchoring
failure that had nothing to do with its content, and the problem gets worse with transcript length:
more names, more chances one is unregistered, more that a second read can drop.

So a park STORES the agent's structured outcome on `capture_queue.outcome`, and a re-file re-runs
the existing pipeline — `_write_meeting_pages` plus every gate — over the stored outcome against the
FRESH registry loaded at this item's base commit. This is not a cache with a correctness exemption:
nothing new decides anchoring, the gates do, exactly as always, over content that no longer changes
underneath them. If the steward's mint resolved the name, the same decisions file.

Reuse is refused whenever the model genuinely has new information: the material AND the submitter's
reply must be byte-identical to what produced the stored outcome, and a `brain_reply` is precisely
the case where they are not. When a real re-distillation does happen, the report DIFFS the two
outcomes — which is the only reason the original loss was ever noticed, since a fresh distillation
reads perfectly plausibly on its own.

## Where the code lives

- `capture.meeting_cli` — `stigmergy-meeting drop`, the only door onto this flow. Validates locally,
  uploads to the evidence store, enqueues exactly one `kind="meeting"` row. See
  [`../../src/stigmergy/capture/index.md`](../../src/stigmergy/capture/index.md).
- `librarian.processing.process_meeting_item` and its private helpers — the flow itself. See
  [`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).
- `librarian.agent.SdkAgent.run_meeting` / `build_meeting_prompt` / `read_meeting_outcome` — the
  agent side: a different system prompt (the brief, not the librarian skill), ONE tool instead of
  the ordinary agent's five (`ALLOWED_TOOLS`), and a different outcome parse
  (`parse_meeting_outcome`, a page SET rather than one page). There is no page-writing lane left to
  name: `_MEETING_NO_PAGE_WRITES_RE` is literally `re.compile(r"(?!)")`, a pattern that matches
  nothing, and the single write the agent is permitted is allowed by `confined_write`'s
  unconditional outcome-file exception. There is no read-confining hook either, because
  Read/Glob/Grep are not in the allow-list for one to scope. What did NOT go: `setting_sources=[]`,
  `mcp_servers={}`, `strict_mcp_config=True` and the environment allow-list — they guard the model
  PROCESS regardless of which tools it holds, so removing them would be a real loss of defence in
  depth rather than a tidy-up.
- `librarian.processing.MEETING_WRITE_PREFIXES` — the same three folders, the FLOW's own
  placement contract (where CODE may create a page for this capture) rather than the agent's lane.
  `gate_zone` still judges the diff against them: a defence against a bug in code's construction
  where it used to be a defence against a steered agent.
- `librarian.gates.GateContext`'s seven flow-scoped fields (`write_prefixes`, `creatable_types`,
  `extra_folder_types`, `page_declared`, `stamped_by_path`, `provenance_pages`, `edits_allowed`) —
  the mechanism that lets one gate suite serve this flow, the plain fast lane and the fast lane's
  own source attachment without a conditional inside any individual gate. `edits_allowed=False` is
  the one this flow alone sets; the other six are also widened, differently, by an attached
  fast-lane capture ([librarian.md](./librarian.md#the-source-attachment-a-parameter-never-a-third-flow)).
- `librarian.double.DoubleAgent.run_meeting` — the offline double's meeting-specific directives,
  planted in the transcript itself: `DOUBLE:decisions=<n>`, the four
  `DOUBLE:meeting-hallucinate*` variants (first decision, first pass only, LAST decision, the
  meeting page's own notes), `DOUBLE:meeting-triage=a,b,c`, `DOUBLE:meeting-anchor=<name>`,
  `DOUBLE:meeting-company[=n]`, `DOUBLE:meeting-body-date-link` and `DOUBLE:meeting-collide`. Every
  sabotage this document names above has a directive that reproduces it offline, keylessly.
  **There are deliberately no declared-vs-written mismatch directives**: with the agent holding no
  page-writing tool and code building every page from the same structured outcome, a disagreement
  between what was declared and what was written is unconstructible, and a directive staging one
  would be testing a mechanism that does not exist. A planted secret needs no directive either —
  the transcript reaches the source page verbatim through code, so a fixture that plants a
  gitleaks-detectable string in the MATERIAL is enough.

## Tests

| Suite | Covers |
|---|---|
| `tests/capture/test_meeting_cli.py` | `stigmergy-meeting drop`'s own refusals, ordering (validate → upload → insert) |
| `tests/librarian/test_meeting_processing_pg.py` | the whole flow over real Postgres + real git, including the long-transcript oversize case |
| `tests/librarian/test_meeting_queue_fencing_pg.py` | the meeting kind claimed through the same fenced claiming as every capture |
| `tests/librarian/test_meeting_brief_contract.py` | the brief↔gates two-sided contract, in both directions |
| `tests/librarian/test_meeting_outcome_reuse_unit.py` | the reuse predicate — when a stored outcome is re-filed without an agent call, and when it must not be |
| `tests/librarian/test_gates_unit.py` | the flow-scoped `GateContext` fields, per-page anchoring, the provenance-group exemption |
| `tests/librarian/test_report.py` | `report.filed_meeting` / `triage_entity_multi` / `needs_input_multi` |
| `tests/entities/test_situations.py` | `subjects_of`'s multi-name fallback |

No test needs an API key: `double.DoubleAgent.run_meeting` drives every sabotage in the table above,
offline, against `--backend double`.

## Known limits, stated rather than assumed away

- **Long transcripts are handled.** `_build_source_parts` splits an oversize transcript and the
  set's arity is N ≥ 1 source parts. What remains is not a gap but a consequence: a very long
  transcript files more pages, and the contract linter judges each part on its own.
- **"Provenance, never knowledge" on the meeting page is agent-judgment, not a mechanical check** —
  see above.
- **The transcript is ground truth, not truth.** A misheard figure in the speech-to-text stays
  wrong-with-provenance; nothing checks a transcript's accuracy against the world, and nothing
  checks a figure at write time at all. The source page remains the permanent, unedited evidence a
  reader can open, and the mitigation is the submitter reading the report — the only person who
  attended.
