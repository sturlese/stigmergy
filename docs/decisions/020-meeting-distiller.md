# ADR 020 — the meeting distiller: one capture, a page set

Status: accepted. Narrative: [`docs/reference/meeting-distiller.md`](../reference/meeting-distiller.md).
Code map: [`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md),
[`src/stigmergy/capture/index.md`](../../src/stigmergy/capture/index.md),
[`src/stigmergy/entities/index.md`](../../src/stigmergy/entities/index.md).

## Context

Meetings are *the richest, most wasted knowledge mine in any company*, and the shape that mines
them is not subtle: a transcript lands in the evidence plane, and the librarian extracts a
`meeting` page plus typed `decision` pages and action items, each traceable back to what was
actually said. None of it was built. A meeting could only arrive as an ordinary capture: one page,
no transcript evidence, its figures unanchored to any source.

The structural obstacle was not the extraction. It was that the write path filed **exactly one
page per capture**, enforced rather than assumed — `processing._cross_check_outcome`'s own
docstring named the way out: *"If multi-page filing is ever wanted, `report.filed` has to carry the
list and `result_ref` has to name the set — a spec change, not a code change."* This record is that
change. Two things had to exist first: a decision about what an anchor MEANS (`entity:` =
aboutness, registry ids, server-stamped), and the queue, the gates, the ask-back and
the governed entity birth this flow reuses wholesale.

## Decisions

**D1 — a second flow, not a branch inside the first.** `processing.process_meeting_item` is a
sibling entry point to `process_item`, dispatched by `worker.process_next` on the queue row's
`kind`. The shared half was extracted (`_pre_agent`: dedup plus the material-level secrets/PII
scan) rather than copied. The alternative — an `if kind == "meeting"` threaded through one
function — would have put the two flows' invariants in one body where the ordinary flow's
"exactly one page" and the meeting flow's "exactly this set" contradict each other line by line.
`MeetingOutcome` is likewise a sibling of `Outcome` instead of an overloaded superset: the fields
differ, and an optional field is a field some caller forgets to check.

**D2 — the drop CLI is the only door, and the enqueue seam is where that is enforced.**
`stigmergy-meeting drop` uploads the transcript to the evidence store and inserts one
`kind="meeting"` row, in that order, so *"no row and no object"* holds by construction for every
refusal. Growing `capture.schema.KINDS` to a third value made `"meeting"` acceptable to **every**
caller of `queue.submit` — including MCP `brain_submit`, where `kind` is a model-chosen argument
and a steered session could have supplied unvalidated meeting metadata that the worker then
stamped into `as_of` on every page. Validation therefore lives at
`prepare_submission`, not in the CLI, and `brain_submit` is restricted to
`MCP_SUBMIT_KINDS = ("raw", "page")`. The CLI keeps its own early copy of the date check for
message quality only. The lesson generalises: a rule enforced
where it is convenient rather than where every caller passes is not enforced.

The CLI **is** the webhook, simulated. A future transcription webhook calls
the same enqueue seam; nothing built here is thrown away when it does, and no transcription tool
is named anywhere in the code — which tool produced the transcript is the operator's coupling, not
the system's.

**D3 — `kind` is the shape of the material and the flow that reads it, never a topic.** The
existing vocabulary (`raw`, `page`) described submission *shape*, and a reader could reasonably
have taken `meeting` for a subject. It is not: it says the material is a transcript and names the
flow that will read it. The page's *topic* remains the agent's judgment, checked by the gates over
the folder the page lands in — the folder is the fact, the declaration is a claim.

**D4 — filing is atomic, and the arity contract is code.** One capture produces one commit
containing N ≥ 1 cross-linked source pages, exactly one meeting page and N ≥ 0 decision pages, or
it produces nothing and parks. `_cross_check_meeting_outcome` refuses a page created but not
declared, a page declared but not created, a decision count that does not match, and any page
outside the three folders. Partial filing was never a candidate: a
meeting page linking a decision page that does not exist, or an orphan decision with no
provenance record, are both worse than parking — the graph is the product, and a graph with
dangling halves is a graph nobody can trust.

The source page's arity is N rather than one because a transcript can exceed the page contract's
line cap; `_build_source_parts` splits it into cross-linked parts rather than letting the whole
capture fail on a linter hard error. The veto is therefore `< 1`, which construction makes
unreachable — kept as a self-check, not as a live path.

**The cross-check shrank once CODE became the sole author of every page in the set.** In the first
cut the agent wrote each page through its own Write/Edit calls and separately DECLARED what it
wrote in its outcome JSON — two independent claims that could disagree, so the cross-check had to
catch a declared decision the diff never created, a meeting page whose "## Decisions" section
linked something else, and a claimed source path that did not match the file on disk. Today
`_write_meeting_pages` builds every page and every link from the SAME structured outcome, so it
cannot declare a decision it did not also write, or link one it did not also file. The four checks
that existed only for an adversarial author (`duplicate-decision-declared`,
`decision-set-mismatch`, `source-path-mismatch`, `meeting-path-mismatch`) were removed rather than
kept as ornaments: a check whose failure mode is structurally unreachable teaches a future reader
that something is still being defended when nothing is.

`result_ref` names the **meeting page** (`<path>@<sha>`), because a human needs one door into the
set and `dedup.Match.page_path`'s existing contract keeps working unchanged. The full list lives
in the report (`report.filed_meeting`).

**D5 — anchoring is per page, because the pages belong to different entities.** `gate_anchoring`
read one anchoring declaration for the whole capture — correct while a capture was one page, and
false the moment a meeting about two customers produces two decisions. Every distilled `decision`
page must anchor to ≥1 entity or declare company-wide scope with a written
reason. The rule is scoped to `decision` pages deliberately: the `meeting` page is provenance and
carries no anchor of its own, and the source page is a machine-zone page. The stamped `entity:`
value is built from the **same call** that verified it, never a second lookup that could diverge,
and `gate_frontmatter` pins the stamped value by equality.

**D6 — the fast lane's stamp deliberately does not write provenance; this flow needs a second
stamp.** `page.stamp_server_fields` omits `content_hash`/`extracted_at` on purpose — that group is
labelled *(sources/source pages)* and a fast-lane capture page is neither. The meeting flow's
source page **is** an ingested page, so `page.stamp_source_fields` writes the provenance group
(`content_hash`, `extracted_at`, `tier`) and `gate_frontmatter` exempts `content_hash` on declared
provenance pages. An exemption is the exact shape of the worst defect class this codebase has: a
carve-out that weakens a gate for a path nothing ever reaches. So it is bounded on every side:
only a `sources/meetings/` path can be a provenance page, decision and meeting
pages cannot claim it, and the stamped values are post-checked by equality like every other
server-owned field. `tier` and `extracted_at` joined `FORBIDDEN_PAGE_KEYS` off the source page —
`extracted_at` was already refused on `wiki/**` by the contract linter, `tier` was refused
nowhere, and this is the first flow in the system that ever writes it.

**D7 — flow policy is passed in, and the default is closed.** `GateContext` grew six fields
(`write_prefixes`, `creatable_types`, `extra_folder_types`, `page_declared`, `stamped_by_path`,
`provenance_pages`) where module-level globals used to be. Every default reproduces the ordinary
flow byte for byte, so a flow that does not set them is out of bounds by default — the direction
`ALLOWED_WRITE_PREFIXES`'s own comment says the list should be wrong in. Creatability is
**flow-scoped, never global**: the `meeting` type is creatable inside this flow only, and an
ordinary capture claiming `type: meeting` still parks with the reason it always had.

The first cut of these constants admitted the ordinary fast lane's folders as well, while the
injected system prompt told the agent its writes were confined to this flow's own three.
Nothing could commit — the terminal cross-check caught it — but the agent was told something false
about its own confinement, and a steering attempt was reported as a **system fault** instead of as
an attempt to write outside the lane. Narrowing the constants to the three real folders restored
three layers at once: the write hook denies at tool time, the zone gate vetoes with the right
finding, and the cross-check went back to being defence in depth rather than the defence.

**D8 — a rule the brief states is a rule the code must check.** This flow's brief lives in the
knowledge repo (`.claude/skills/meeting-distiller/SKILL.md`), read at the base commit and injected
by the platform, never loaded by the agent from the repo it operates on. This repo has already paid
for the other half of it: dropping a requirement from a gate left the skill telling the agent to
do something the gate no longer read. So `tests/librarian/test_meeting_brief_contract.py` asserts
the contract **in both directions** — and the two worst defects found in this flow were both the
same class. The brief promised that the worker checked the meeting page's decision links
"in both directions, exactly", and the worker checked neither, verifying the outcome JSON instead
of the committed page. It now parses the page.

**D9 — the date-in-wikilink convention was a veto, and is now a gardener finding.** A body-prose
link to a date-bearing meeting stem (`[[2026-07-29-…]]`) reads as figures the linking page
asserts to any numeric scanner that does not exempt link syntax — so a page that invented nothing
was refused. The tempting fix (stop scanning wikilink targets) was **rejected** for as long as an
ingest-time figure check existed: laundering a figure through a wikilink is impossible precisely
*because* those digits are scanned, and exempting them would have opened a hole in the trust layer
to remove a false positive. The flow instead refused a body-prose link to a date-bearing stem with
a repair brief naming the alternative (`sources:`/`related:` frontmatter, which the scanner did
not read).

**That veto is gone.** Ingest-time figure verification was removed whole
([ADR 026](./026-the-purge.md) D2), and with its disappearance the only argument for vetoing a
date-bearing body link disappeared too: the convention is style, not safety. It stepped down to
the gardener's `date-bearing-body-link` check ([ADR 027](./027-the-contraction.md)), which flags it
over the committed corpus and blocks nothing. The general rule survives the specific one: gates
veto the irreversible, the gardener flags conventions, and a control kept past the death of its
justification is a tax nobody remembers agreeing to.

## Known limits

- **"Do not park knowledge on the meeting page" has no mechanical check.** There is no structural
  property distinguishing provenance prose from knowledge prose that a gate could test without
  becoming the content reviewer this design retired. It stays agent judgment, stated
  in the brief and visible in the submitter's report.
- **Nothing extracts structured facts from a transcript.** Spoken figures are the lowest-trust
  numbers in the system, and a distilled figure's only guarantee is the transcript sitting one
  click away in the evidence plane. Building a figure store fed by speech was rejected, not
  deferred.
- **The mechanism is proven against synthetic, single-operator traffic.** That someone else's
  meeting — their vocabulary, their entities, their transcription tool — files cleanly is not.
