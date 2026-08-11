# librarian — the fast lane's back half

Narrative doc: [`docs/reference/librarian.md`](../../../docs/reference/librarian.md) (the how and why
for an operator and a submitter); the meeting flow's own how and why (the page set, per-page
anchoring, the brief↔gates contract, the vetoes it can produce) is
[`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md); the
view-regeneration trigger this package calls into (best-effort, after a meeting files) is covered
from this package's OWN side here and end to end in
[`docs/reference/views.md`](../../../docs/reference/views.md). Design
records: [ADR 015](../../../docs/decisions/015-librarian.md), and
[ADR 026](../../../docs/decisions/026-the-purge.md) (**read this one first**: D2 is why the trust
layer and `gate_trace` are gone, and D4 is why this package's inputs come from
`stigmergy.kernel` rather than `stigmergy.pipeline`). [ADR 020](../../../docs/decisions/020-meeting-distiller.md) (the
meeting flow as a sibling entry point, atomic page-set filing, per-page anchoring, the second
stamp for the provenance group, and flow policy passed into `GateContext` closed by default).
This file is the code map — for whoever is about
to edit this package, not run it.

## Purpose

Drains `capture_queue` one row at a time and turns each into either a committed page in the
knowledge repo or an honest, actionable refusal. The organizing idea behind every module below is a
division of labour: **the agent judges** (placement, wikilinks, anchoring, duplication —
meaning problems, where a deterministic resolver fails silently); **code vetoes** (zone, secrets, PII,
the contract linter, anchoring — properties an independent checker can settle over a diff, after
the fact). Gates check; they never interpret. If a check needs judgment, it belongs in the skill
(`.claude/skills/librarian/SKILL.md`, versioned in the knowledge repo, not here), never in
a new gate.

Layering (`tests/test_architecture.py` enforces it): `librarian` may import `capture` (the queue
primitives, the evidence plane) and `stigmergy.kernel` (the page contract's constants, the frontmatter
parser, the ACL resolver, the entity registry, the document converters the Drive door's own
kernel-hands conversion runs on — a dependency-free library, never a layer). It must
**never** import `server` or `answer` — this is a worker beside the API, not a layer above or below
it. The two talk only through the durable queue row, so a slow agent run can never happen inside an
HTTP request.

**One further edge is DECLARED rather than implied: `stigmergy.index.corpus`.** `edits.py` reaches
it for `ZONES` and `gather.py` for `load_pages`, and both are reaches for a PURE REPO PARSER —
frontmatter, the wikilink graph, the zone list, over a directory, with no database connection and
no ACL surface anywhere in it. That is the same shape `views/skeleton.py` and `views/staleness.py`
already declare for the same module. It is a LIBRARY reach, not a layer: nothing in this package
touches `pages_index`, which is the table the CLAUDE.md invariant is about ("every reader of
`pages_index` names an ACL predicate"), and a change that made this import need one would be a
design change rather than a wider import. `stigmergy.index.store` — the connection — is reached by
`cli.py` alone, exactly as `capture.cli` and `views.cli` reach it.

**The purge removed this package's whole trust layer and its second caller.** `gates.py` once ran
NINE gates, including a `gate_trace` that verified a page's own figures at filing time, and it had
a SECOND caller — `stigmergy.server.canon`, which ran the same gates synchronously over a canon-lane
proposal or promotion. Both are gone. Ingest-time figure checking went because it taxes the model's
own prose with false positives and cannot catch the dangerous class anyway — an invented CLAIM
passes every figure check; what protects the reader is the verbatim source one click away, plus
**answer-time verification** (`answer.verify_answer`, cites-or-refuses, pure code) as the whole of
it. The second caller went because the canon lane it served (`server/canon.py`) is gone whole
([ADR 026](../../../docs/decisions/026-the-purge.md) D1) — `gates.py` has exactly one caller left,
`processing.py`, and `GateContext` carries no `canon_lane`/`promotion` fields at all: there is
nothing left to declare a promotion for. What that restores is the simpler statement of the same
rule — an edit to an already-filed page may only GROW its `related:` list or ADD a callout, with no
carve-out, ever. **Eight gates run**: `gate_zone`, `gate_binary_page`, `gate_body_rewrite`,
`gate_secrets`, `gate_pii`, `gate_frontmatter`, `gate_contract`, `gate_anchoring` — the whole of
`gates.ALL_GATES`.

**`processing.py` has a SECOND top-level entry point**: `process_meeting_item`,
`process_item`'s sibling for `capture_queue` rows with `kind == "meeting"`. It is not a branch
inside `process_item` — the two flows disagree about the one invariant `process_item`'s own
machinery is built on, exactly one new page per capture — so it is a parallel function that reuses
what genuinely is shared (`_pre_agent`: dedup levels 1-2, the material-level secrets/PII scan) and
owns its own agent call (`deps.agent.run_meeting`, the meeting brief instead of the librarian
skill), its own retry policy wrapper (`_run_meeting_in_worktree`), its own atomicity cross-check
(`_cross_check_meeting_outcome` — a page SET's contract, not `_cross_check_outcome`'s "exactly one
page" rule, which stays unchanged and still governs every ordinary capture), and its own commit/
report (`_file_meeting`, `report.filed_meeting`). See
[`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md) for the full
account; the rest of this file's "one item's journey" diagram describes `process_item` only. This
flow was unaffected by the purge.

**`_file_meeting` triggers view regeneration, best-effort, right after pushing the
meeting's own page set**: it calls `stigmergy.views.regenerate.run` for
every entity id the meeting's decision pages resolved to (`ctx.stamped_by_path`'s own server-
resolved values, never the agent's declared names), over the SAME already-pushed worktree,
unguarded (`guarded=False`, since that worktree is always fresh and detached). A view fault is
caught, logged, and recorded to its own `job_runs` row — never re-raised into the meeting's own
`Result`, because the meeting's page set is already committed and pushed by the time this runs (an
irreversible, successful outcome that a downstream fault must not retroactively taint).
`processing.py` may import exactly one symbol from `stigmergy.views` —
`stigmergy.views.regenerate` — enforced by
`tests/test_architecture.py::test_librarian_may_only_import_views_regenerate`; the reverse edge
is closed too (`stigmergy.views` must never import `stigmergy.entities`, keeping the unattended
worker's dependency graph one-way — see
[`views/index.md`](../views/index.md)'s own "Avoid" section).

**The agent seam is a PORT, there are TWO backends behind it, and both serve BOTH flows.**
`filing_port.FilingAgent` is what `processing.py` is written against — `run` / `run_meeting`, the
`AgentRun` envelope, the fault contract (`AgentError` carrying `run_cost_usd`), the side-effect
rules (which differ per flow AND per ordinary SHAPE, and must not be averaged), and one declared
capability, `structured_ordinary`. `PydanticFilingAgent` and `DoubleAgent` satisfy it
STRUCTURALLY — no base class, no registration: a backend is a class that answers the two calls and
declares that one attribute. `build_agent` returns the port and is where both are declared to
satisfy it.

**The port has already outlived one implementation, which is the argument for it in one
sentence.** `SdkAgent` — the Claude-Code-harness backend — was retired without `processing.py`
changing a line, because what the worker is written against is `filing_port.py` and not a class.
A worker still CONFIGURED for it (`STIGMERGY_LIBRARIAN_BACKEND=sdk` in a stale `fly.toml` or
`.env`) is refused at startup by name — `agent.RETIRED_BACKENDS` carries the message, and
`agent.ensure_known_backend` is the ONE place either that refusal or the unknown-value one is
worded.

**The ORDINARY flow has TWO shapes behind one entry point, and a backend DECLARES which one it
answers** ([ADR 033](../../../docs/decisions/033-structured-filing-flow.md)). `structured_ordinary`
is `False` for `DoubleAgent` — the EXPLORING shape: the agent goes looking through
the checkout itself, writes the page inside `agent.confined_write`'s allow-list,
and declares the path it wrote in `Outcome.page_path`. That was the retired backend's shape too,
and the double is what keeps the branch exercised: both roads through `processing._one_pass` run
offline on every `make test`. It is `True` for `PydanticFilingAgent` — the
STRUCTURED shape: `processing._one_pass` runs the deterministic gatherer (`gather.py`) first and
hands the result over as rendered prompt text, the agent holds no tool and writes nothing, its
account CARRIES the page's own body in `Outcome.page`, and `processing._write_ordinary_page` builds
and writes the file. **`processing` reads the declared attribute and never `isinstance`**: a third
backend, or a double standing in for one, must take the right branch by declaring the right thing
rather than by being the right class. Everything from the stamp down — all eight gates,
`_cross_check_outcome`'s "exactly one new page", the commit — is shared byte for byte, which is what
keeps two shapes from becoming two flows.

**M1's meeting-only refusal is gone with the limitation it described.** `worker.startup_checks` used
to refuse `backend="pydantic"` for a worker outright (a queue carries ordinary captures too, and a
backend serving one `kind` burns deliveries while looking configured), with one `meeting_only`
escape for the eval rig. Both are removed. What that check still validates for this backend is what
was always about the backend: a provider-prefixed model string, a configured price, and the
provider's own key — plus the librarian skill at the base commit, required of every backend in
`agent.SKILL_READING_BACKENDS` (a named set, not "the ones that are real": the question is who
INJECTS the brief, and the offline double reads none).

**`cost_usd` has two roads, and the report's shape has neither in it.** A backend priced by its own
provider passes that figure straight through (the retired one worked that way, and the double's
honest `0.0` is the same road); a backend that reports only TOKENS multiplies
them by `pricing.py`'s configured table, which is every backend that costs anything today. A model
with no configured price is REFUSED at startup —
never silently `$0.00`, which would read as free. [ADR 032](../../../docs/decisions/032-filing-port-and-pricing-seam.md)
records both halves and the expand–contract plan.

**A THIRD external input `agent.py` briefly grew is also gone.** The agent run once read a fleet
supervisor's approved playbook (`ops/playbook.md`, out of the worktree) and appended it to the
system prompt as advisory context, alongside the skill. The supervisor went with the purge
([ADR 026](../../../docs/decisions/026-the-purge.md) D3), and with it the human gate that made the
playbook *approved text* rather than accumulated machine opinion — `agent.py`'s own docstring
records the removal. The skill, read at the base commit, is once again the ONE thing this agent is
briefed with.

**The fast lane has a SOURCE ATTACHMENT — a parameter, never a third flow.** The door rule:
material with independent documentary existence files a `sources/` page beside the
synthesis; a conversational capture leaves none. `_source_attachment(item)` returns a
`SourceAttachment` in exactly TWO positions, each keyed on a fact a DOOR asserted server-side: the
capture's door asserted `source_client == "slack"` (the 🧠 gesture — `SLACK_SOURCE_PREFIX`), or the
ROW'S OWN `kind` is `drive` (`DRIVE_SOURCE_PREFIX`). `process_item` then widens its own
`GateContext` by exactly that attachment's own folder and one type (`source`). CODE writes the
verbatim thread or document (`_write_attached_sources` → `_build_source_parts`, the meeting flow's
own writer), `page.stamp_source_fields` stamps the provenance group,
`GateContext.provenance_pages` tells the gates which pages carry it, and `page.add_source_citation`
puts the source in the synthesis's own `sources:`. **The agent writes none of it** —
`agent.confined_write` is unchanged, still one new
`.md` page in one of the three ordinary folders. Keying the SLACK position on a hint is sound for
one specific reason: `source_client` / `source_permalink` are refused at the client seam for every
door but Slack's own (`capture.schema.reject_source_provenance_hints`), so the hint stopped being
client-writable the moment it became load-bearing; the drive position consults no hint at all,
because `kind: drive` is unreachable through `brain_submit` (`schema.MCP_SUBMIT_KINDS`). With the
parameter OFF — every MCP capture, and every door until it opts in — the fast lane behaves exactly
as it did before the parameter existed.

**Contract change a future reader must not rediscover**: the branch tip after a meeting filing is
**not** necessarily the meeting's own commit. When the view-regeneration step above succeeds, it
pushes a SECOND commit on top of the meeting's. `result_ref` (`<meeting_page>@<sha>`, captured
BEFORE the view step runs) and the `sha` `_file_meeting` returns still name the meeting's OWN
commit and remain the correct, stable handle for "what this capture filed" — but any code that
reads "the current branch tip" to learn what a capture just filed, rather than reading
`result_ref`/`sha` directly, is wrong. This bit a test once; see
[`views/index.md`](../views/index.md)'s own Notes section for the fuller account and
[`../../../docs/reference/views.md`](../../../docs/reference/views.md) for the narrative.

## One item's journey — claimed row to pushed commit

```
capture.queue.claim_next            — FOR UPDATE SKIP LOCKED, hands back the `attempts` fence
  │
  ├─ dedup.find_retry / find_already_filed        — deterministic, before the agent, cheapest first
  ├─ gates.scan_secrets / gates.scan_pii(material) — over the RAW MATERIAL; bounces the whole capture
  │
  └─ gitcmd.ephemeral_worktree(repo, base.sha, worktree_root)  — a fresh `git worktree --detach`,
       │                                                          reaped by `gitcmd.reap` on crash
       ├─ gather.gather(...)                       — STRUCTURED shape only: the context code builds
       │     from the checkout (entities, top-K candidates + excerpts, the link neighbourhood, the
       │     wikilink vocabulary), rendered by agent.render_gathered and fenced like any material
       ├─ agent.build_agent(settings).run(...)     — a filing_port.FilingAgent. EXPLORING shape:
       │     writes ONE new page plus the outcome file, confined by agent.confined_write /
       │     page.is_inside. STRUCTURED shape: writes NOTHING and returns the page's text
       ├─ agent.read_outcome → parse_outcome        — untrusted input, bounded and frozen (Outcome)
       ├─ agent.discard_outcome_file                — consumed before the diff is ever taken
       ├─ processing._write_ordinary_page           — STRUCTURED shape only: CODE writes the page.
       │     Filename from the title (page.unnameable_reason, collision-checked by page.path_key),
       │     folder from page.FOLDER_BY_TYPE, frontmatter and H1 from the account
       │
       ├─ edits.apply_declared(outcome.edits)       — code performs the DECLARED backlink/overlap/
       │     contradiction edits to EXISTING pages (page.with_related_link / with_callout)
       ├─ gates.verify_against_material + processing._stamp  — server-owned frontmatter written
       │     (page.stamp_server_fields): status, as_of, submitted_by, entity, acl
       │
       ├─ gates.run_gates(ctx)             — zone, binary, body-rewrite, secrets, pii, frontmatter,
       │     contract, anchoring — ALL_GATES, every one, every attempt (eight; `gate_trace`
       │     went with the trust layer, ADR 026 D2)
       ├─ processing._cross_check_outcome  — the outcome's account must match the diff
       │     (no-page-created / multiple-pages / page-path-mismatch)
       │
       │   parked by the agent? → processing._triage routes it: an `unresolved-entity`
       │       declaration on a capture that still has its ONE question becomes `needs_input` with a
       │       code-built question (processing._ask_or_park → report.needs_input, candidates from
       │       gates.registry_candidates); a spent budget, an `unsupported-type` and every surviving
       │       veto go to `triage`. The budget is `capture_queue.asked_at`,
       │       so it survives a reply, a steward requeue AND a lease redelivery
       │
       │   vetoed? → ONE corrective retry (gates.corrective_brief; worktree `reset --hard` + `clean -fdq`)
       │             — UNLESS no veto names a repair the agent can perform (gates.unrepairable):
       │               then it refuses after ONE pass rather than buying the same answer an agent
       │               run later, and the report's agent counter says so
       │             → still vetoed? preserve the refused diff (processing.preserve_refused_diff),
       │               terminal state chosen by CAUSE, not by which gate fired (processing._refuse)
       │               — and the two refusals whose honest destination is the steward's queue reach
       │               `triage` rather than `failed`: "nothing on this page anchors"
       │               (processing._unanchorable) and "the fast lane may not create that type"
       │               (processing._uncreatable_type). Both are destinations the agent reaches by
       │               parking the capture itself, so both roads end in the steward's queue
       │
       │   `processing._one_pass` is one attempt of everything above; `_run_in_worktree` is the POLICY
       │   around it (one pass, one corrective pass, then refuse) — so an `OutcomeShapeError` from
       │   `parse_outcome` reaches the retry by the SAME road a gate veto does, rather than escaping
       │
       └─ gates pass → gitcmd.commit(githubapp.identity(), gated_entries=ctx.entries)
              │                                       — path AND blob hash: a page rewritten in
              │                                         place after the gates ran is refused
              → capture.queue.holds_lease            — re-asserted; the LAST check before anything
              │                                         irreversible
              → gitcmd.push (rebase-and-retry; githubapp.installation_token via push_config)
              → report.filed(page, commit, anchors, links, overlaps, pages_edited,
                             agent_rationale=outcome.summary)
```

`capture.queue.finish` (fenced by the SAME `attempts` token `claim_next` handed back) closes the row
on every branch. `worker.process_next` is the only caller of the whole path above, and it is what
turns any exception it raises into a `failed` `Result` naming the right stage. `report.filed`'s
shape carries no `verification`/`figures` — nothing computes a verification verdict, so nothing is
reported about one (see Data & contracts, below).

## Key entry points

| Module | Owns |
|---|---|
| `cli.py` | `stigmergy-librarian` — `once` / `run` / `status`; the operator's front door |
| `filing_port.py` | `FilingAgent` — the agent seam as a `Protocol` instead of a convention: the two calls, the `AgentRun` envelope, `priced()` and the fault contract, and the per-flow side-effect rules. Imports `errors` and nothing else, so every backend can depend on it |
| `pricing.py` | model id → $/MTok (`PRICES` + `$STIGMERGY_LIBRARIAN_PRICING`, `AS_OF`), `compute_cost_usd`, `require_priced` — for the backends that report TOKENS rather than dollars |
| `pydantic_backend.py` | `PydanticFilingAgent` — the real backend, one of the two behind the port: one structured pydantic-ai call per flow, no tools, no outcome file, BOTH flows ([ADR 032](../../../docs/decisions/032-filing-port-and-pricing-seam.md) for the meeting half, [ADR 033](../../../docs/decisions/033-structured-filing-flow.md) for the ordinary one). `PydanticMeetingAgent` survives as a deprecated alias until its callers migrate |
| `gather.py` | the deterministic gatherer — a pure function of `(worktree, registry, material)` producing what the STRUCTURED ordinary shape is handed instead of exploring. Reads the CHECKOUT, never `pages_index`, and `_confined` is what makes "the same data the agent read" true rather than intended ([ADR 033](../../../docs/decisions/033-structured-filing-flow.md)) |
| `worker.py` | the loop, `startup_checks` (every fail-closed startup refusal), `sweep`, `Worker` (signal handling) |
| `processing.py` | `process_item` — one capture end to end; `Result`, `Deps`, the refused-diff digest; `process_meeting_item` — its sibling for a `kind="meeting"` row, filing a page SET instead of one page; and `process_drive_item` ([ADR 028](../../../docs/decisions/028-drive-door.md)) — the thin drive sibling: kernel-hands conversion (`_drive_material`, `_with_vision_fallback`) then `process_item` itself over the extracted text, with the source attachment ON via `_source_attachment`'s kind-keyed drive branch |
| `config.py` | `Settings` — every tunable, resolved once (`from_args`); the derived visibility timeout |
| `base_inputs.py` | the three repo-sourced inputs — ACL, entity registry, contract linter — read **at `base.sha`**, never off the working tree; also the doorbell's `ops/stewards.json` reader, shared with `stigmergy.server.review` |
| `bootstrap.py` | `stigmergy-librarian-boot` — what the DEPLOYED worker runs: clone, verify checkout == base ref, exec the loop |
| `gitcredential.py` | `stigmergy-librarian-credential` — git credential helper backed by the GitHub App, so a container's fetch authenticates |

Everything else is reached FROM `processing.py`; read it first when tracing one capture's path.

## Use these

- `capture.queue.claim_next` / `finish` / `holds_lease` — the lease and its `attempts` fence. **Never
  reimplement the fence**: it closed a real defect, a stalled worker silently overwriting the live
  one's row.
- `page.path_key` / `path_keys` / `is_inside` — the ONE way to ask "is this the same page" or "does
  this resolve inside the worktree", correct on a case- and Unicode-normalization-insensitive
  filesystem (macOS/APFS, the primary deployment platform). `agent.confined_write`,
  `edits.validate`, `gather._confined` and `processing._write_new` all go through it; do not add a
  second, `==`-based answer to either question. **`is_inside` RESOLVES**, which is why an
  `os.path.islink` test on the leaf is not a substitute for it (a symlinked directory COMPONENT is
  invisible to one) and why it is not a substitute for that test either (a symlink pointing back
  inside the worktree is contained and still not the bytes git tracks). Both halves, everywhere the
  two are needed.
- `processing._write_new` — THE write for every page-building flow (`_write_ordinary_page`,
  `_write_meeting_pages`, `_write_attached_sources`). It carries both guards so three call sites
  cannot come to disagree: containment before the write, and `OSError` (an over-long stem's
  `ENAMETOOLONG`, `O_EXCL`'s `EEXIST`, `ENOSPC`) wrapped into `WorktreeError` so a filesystem fault
  is a NAMED stage instead of escaping every handler as `unexpected` with the item's spend already
  banked. Never call `page.open_for_new` from a flow directly.
- `page.unnameable_reason` — the ONE answer to "can this be a filename", read by `gate_zone` over
  the diff and by `_write_ordinary_page` before it writes. It bounds the stem in **UTF-8 BYTES**
  (`MAX_PAGE_STEM_BYTES`), because that is the unit `NAME_MAX` counts: a character bound would pass
  names the filesystem refuses, and this corpus is expected to carry accented and non-Latin
  titles routinely.
- `gitcmd.diff_entries` (`--raw -z`) / `added_lines` — the diff's STRUCTURED form. Every gate reads
  the diff through these, never through a hand-parsed unified diff: git's rendering can be
  impersonated by page content spelled to look like diff metadata — a fact that has cost three
  separate gates on this branch (see `gates.py`'s module docstring). `gitcmd.diff_text` survives only
  for the human-facing refused-diff digest, where a parsing slip is cosmetic rather than a blind gate.
- `report.py` — the only place a sentence a human reads gets composed **about a fast-lane outcome**.
  The CLI (`report.render_prose`) and `brain_submissions` (which reads the queue row's `report`
  column) render the SAME fact set; never compose wording anywhere else. Its SHAPE
  (`base_report`) and the shared `SEARCHABILITY_NOTE` clause live in `capture.schema` and are
  re-exported here — `capture.dispositions` composes the two sentences a STEWARD authors
  (`resolved`, a human `rejected`) and `capture` may not import `librarian`, so the vocabulary two
  packages must agree on sits with the column that stores it.
- `gates.registry_candidates` — the ONE reading of "which entities exist". `anchoring_brief` lists
  them for the agent and `report.needs_input` lists them with aliases for the person being asked;
  a second implementation would let the human's candidate list disagree with the one the gate
  actually asks, which is worse than no list (see `MAX_BRIEF_REGISTRY_NAMES`).
- `stigmergy.text.clamp` / `sanitize` — the word-safe truncation and the control-character strip both
  `report._clean` and `capture.cli._clean` now go through. The truncation was written twice and the
  copies differed; the hard-slicing one cut a `brain_reply(...)` invocation mid-call on the surface
  that tells a steward to run it.
- `filing_port.FilingAgent` / `AgentRun` / `priced` — the agent seam, and the ONE place its contract
  is written down. A new backend is declared against it; a change to what `processing` may assume of
  a backend is a change HERE first, not a convention three modules discover separately.
- `pricing.compute_cost_usd` / `require_priced` — the ONE tokens-to-dollars answer. A second
  multiplication at a call site is a second price table that drifts from the configured one.
- `gates.Finding` / `GateContext` / `run_gates` — the one veto surface. A new check is a new
  `(ctx) -> list[Finding]` function added to `gates.ALL_GATES`, not a special case inside
  `processing.py`.
- `page.PAGE_TYPES` — the single placement table (`known` vs `creatable`, folder, label, refusal
  reason). **SEVEN types are known; exactly THREE are creatable by the fast lane** — `note`
  (`wiki/notes`), `decision` (`wiki/decisions`) and `concept` (`wiki/concepts`). The other four
  each carry their own refusal reason: `entity` (governed birth, `stigmergy-entities`), `source` and
  `meeting` (provenance, written by code from a captured document), `view` (regenerated from an
  entity's members). Every other module asks placement questions through `page.classify_page_type`
  / `ensure_creatable` / `type_for_folder`, and every derived view (`FOLDER_BY_TYPE`,
  `FAST_LANE_TYPES`, `PROVENANCE_PAGE_TYPES`, `FAST_LANE_TYPE_LIST`, `gates.ALLOWED_WRITE_PREFIXES`,
  `agent.LANE_FOLDERS`, `agent._ALLOWED_WRITE_RE`) computes itself from this tuple — nothing
  re-derives a folder list by hand, which is why the runtime strings stayed correct through the
  narrowing even where the prose did not (see Notes).
- `stigmergy.kernel.acl` (via `acl_rules.py`, an adapter — see its module docstring for why one is
  needed) for label resolution, `stigmergy.kernel.registry.load_registry` for entity resolution, and
  the knowledge repo's own `.claude/tools/stigmergy_lint.py` for the contract linter — reused, never
  re-implemented. All three are read through `base_inputs`, never through `stigmergy.kernel` directly
  from `processing.py` or `gates.py` — see the next bullet.
- `base_inputs.load_acl` / `load_registry` / `linter_at` — the ONLY way the fast lane reads those
  three files. They read at `base.sha`, never off the working tree: once the registry is the output
  of a governed steward flow, a working-tree read is a read *around* that gate, and an uncommitted
  edit could anchor captures to an entity nobody approved. `Settings.acl_path` /
  `registry_path` / `linter_path` still answer "where does this live in a checkout" — for steward
  tooling and for messages about a file a human can open — and are **not** how a run reads them.

## Avoid / anti-patterns

- **Do not add a gate that interprets.** Gates check; they never interpret. Judgment belongs in the
  skill (`stigmergy/.claude/skills/librarian/SKILL.md`, in the knowledge repo — not in this package).
- **Do not let a gate infer which lane it is running for, or invent a promotion, from the shape of a
  diff.** There is no lane to infer any more (`GateContext` carries no `canon_lane`/`promotion`
  field) — a `developing → canonical` `status` change is a person editing a page directly,
  never something a gate reasons about. If a governed write lane is ever reintroduced, it must TELL
  a gate the fact it needs rather than have the gate guess from a diff's shape — the
  exact mistake that shipped once, in the old `gate_body_rewrite`'s first version of a promotion
  carve-out, before the purge removed the carve-out along with what it served.
- **Do not let the agent touch an existing page**, and do not widen `agent.confined_write` past "a NEW
  `.md` page in one of the three fast-lane folders, that does not already exist". An edit to a page that
  already exists is DECLARED in the agent's outcome and PERFORMED by `edits.py` — the agent's own
  write lane admits no modification at all ([ADR 015](../../../docs/decisions/015-librarian.md) §3).
  **The rule holds on the STRUCTURED shape by construction rather than by an allow-list**: there is
  no tool and no field an account could name a path with. Do not add one — `processing.
  _write_ordinary_page` derives the folder from `page.FOLDER_BY_TYPE` and the filename from the
  title, and a `page.path` field would hand back exactly the capability ADR 033 removed.
- **Do not compare paths with `==`.** Two real defects came from exactly that: an exact byte
  comparison against `git`'s tracked-path spelling let a re-spelled existing page (a different case,
  or an accented title in its NFD form) pass as "does not exist yet" on macOS/APFS, regaining the
  write-to-an-existing-page capability the amendment above removed. Go through `page.path_key`.
- **Do not log a HANDLED failure with `exc_info=True`.** `worker.process_next`'s
  `except processing.PROCESSING_ERRORS` branch catches a KNOWN family, names a stage for it and composes
  a careful human sentence directly below; a traceback above that sentence makes a handled validation
  read as a crash. That defect class has now recurred five times in this repo — so if you add a
  branch that catches a NAMED exception and reports it to a
  person, log the class, the stage and the message, and leave `exc_info` to the `except Exception`
  branch where the traceback IS the diagnosis. The same rule caught `registry.load_registry` raising a
  bare `ValueError` past `cli.main` (now wrapped by `base_inputs.load_registry`): **a
  non-`LibrarianError` escaping `cli.main` is a stack trace at an operator**, so wrap any new
  external loader.
- **Do not put a filesystem path — or any `str(exception)` — on the wire for a mid-run fault.**
  `worker.process_next`'s `except LibrarianConfigError` branch is a WIRE path (`Result.error` /
  `Result.report` reach MCP clients through `capture_queue`); it logs the real exception and returns a
  FIXED sentence naming only the stage (`"config"`). This already regressed once in this build — see
  `githubapp._private_key`'s docstring — so do not "improve" a message here by splicing in the caught
  exception's text.
- **Do not run two long-running workers on the same repo AND the same worktree root.**
  `gitcmd.reap` / `reapable` scopes the startup reap by repo and by the pid that created each
  worktree, which makes `once` beside `run` on one repo safe (and a second repo entirely safe) — but
  two long-running workers sharing both a repo and a root can still lose an item to each other's reap.
  Give each its own `STIGMERGY_LIBRARIAN_WORKTREE_ROOT`.
- **Do not parse `git diff`'s rendered text to decide what a gate should act on.** Read
  `gitcmd.diff_entries` / `added_lines`, or a base blob (`git show HEAD:<path>`) directly, instead.
  Page content can be spelled to look like diff syntax (a header, a hunk boundary, a frontmatter
  field), and it has, three separate times on this branch.
- **Do not read the environment at import time anywhere in this package.**
  `config.Settings.from_args` is the only place; every helper takes a duck-typed settings object
  instead of importing `config` for a default.
- **Do not import an agent framework at module scope.** `pydantic_ai` is imported inside the
  backend's own methods only —
  `tests/test_architecture.py` asserts the offline double never touches it, which is what keeps CI
  keyless and the whole suite running against `--backend double`. **The same rule applies to
  `pydantic_ai`**, imported inside `PydanticFilingAgent`'s own methods only, for the identical
  reason: a keyless run must load no agent framework at all, and the import graph must not claim
  this package depends on one unconditionally. (`pydantic` itself is module-scope in
  `pydantic_backend.py` — the output schema is plain data, and a test that builds one by hand must
  not have to reach through a backend to do it.)
- **Do not reuse `kernel.llm.build_processor` for a librarian backend.** It is this repo's fake/real
  dispatch for every OTHER agent and it is the wrong seam here: the librarian's offline path is
  `double.DoubleAgent` — a whole adversarial backend the suite is built on — so routing a librarian
  backend through `resolve_backend` would create a SECOND offline path, with different semantics,
  answering to a different variable (`$CLEAN_LLM` rather than `$STIGMERGY_LIBRARIAN_BACKEND`). What
  `pydantic_backend.py` reuses instead is everything the FLOW already owns: `read_meeting_brief`,
  `build_meeting_prompt`, `build_meeting_system_prompt` and `parse_meeting_outcome`.
- **Do not let a backend report `0.0` for a run that cost money.** A backend that is not priced by
  its own provider goes through `pricing.compute_cost_usd`, and a model with no configured price is
  refused at STARTUP (`pricing.require_priced`). A silent zero in `report.cost_usd` reads as free,
  which is the one direction nobody audits.
- **Do not import `stigmergy.server` or `stigmergy.answer`** from anywhere in this package (or the
  reverse) — `tests/test_architecture.py` asserts both edges. There is no second caller into
  `gates.py` from `stigmergy.server` either (the canon lane that was one is gone); `gates.py` has
  exactly one caller, `processing.py`.
- **Do not import `stigmergy.entities`.** The edge runs one way only: `entities` (the steward's CLI)
  imports three modules of this package (`gitcmd`, `config`, `gates` — see `entities.clone`'s
  docstring for why that reach is a reuse and not a rewrite), and this package must never
  import it back. The unattended worker cannot depend on the steward's CLI. Where the two need the
  same fact and the import would run the wrong way (which characters an entity NAME may carry), it is
  stated at both ends with the duplication declared, not resolved by importing across the edge.
- **Do not read `ops/playbook.md`, or reintroduce a second injected text into the agent's system
  prompt, without reopening [ADR 026](../../../docs/decisions/026-the-purge.md) D3 first.** The
  fleet supervisor and the human gate that made its playbook *approved text* are both gone; the
  skill is once again the only briefing this agent reads.

## Data & contracts

- **`agent.Outcome`** (frozen) — the agent's account of one item, parsed and bounded by
  `agent.parse_outcome` at the trust boundary (never handed onward as a raw `dict`): `decision`
  (`"file"` / `"triage"`), `title`, `page_path`, `page_type`, `summary`,
  `anchoring` (`{kind, reason, entities}`),
  `links_created`, `overlaps` (`[{path, note}]`), `edits` (`[{path, kind, link, note}]`, `kind` one of
  `page.EDIT_KINDS`), `findings` (`[{category}]`, filtered to `gates.INJECTION_CATEGORIES`), `triage`
  (`{kind, name, judged_type}`), and — ADDITIVELY, ADR 033 — `page`, an optional frozen
  `OutcomePage` (`{title, page_type, body}`) carrying the page's own TEXT for the shape where code
  writes it. **Both halves are valid and `parse_outcome` accepts both**: the exploring shape
  produces `page=None` and names `page_path`; the structured shape produces `page` and names no
  path at all — the folder is derived from `page_type` through `page.FOLDER_BY_TYPE`, so an account
  cannot name a location. `title`/`page_type` stay SINGLE fields either way (the sub-object fills
  them in when the top level is silent), so every downstream reader keeps reading one field. Which
  half is REQUIRED is not the parser's question — `processing._require_page_content` asks it, keyed
  on the backend's own `structured_ordinary` declaration. The channel is a JSON file
  (`agent.OUTCOME_FILENAME`) at the worktree root for the file-carrying backends, consumed and
  deleted before the diff is ever taken, and the envelope itself for a structured one.
- **`gates.Finding`** — `gate`, `code`, `severity` (`"veto"` / `"note"`), `locator`, `values`, and
  TWO texts
  for two audiences: `message`, the diagnosis a human reads (`report.py` composes its sentence
  around it, and it never names the offending value), and `brief`, the repair instruction the AGENT
  reads on its ONE corrective retry. `brief` is optional and falls back to `message` — right where
  the message already reads as an instruction, a standing debt where it does not (*a gate's message
  is not a brief*; `gates.corrective_brief` carries the measurement that forced
  the split, and `gates.anchoring_brief` is the worked example the next gate should copy).
  `values` (default `()`) carries the VERBATIM identifier(s) the finding is ABOUT, for a reader that
  compares identity rather than displays it: `locator` is a presentation transform (sanitized,
  whitespace-collapsed, clamped to `MAX_BRIEF_NAME_LEN` with an ellipsis), and comparing THAT for
  identity is what silently broke `processing._unanchorable` for a plural anchor, an NFD-composed
  accent and any name over the clamp. Two producers: `gate_anchoring`'s unresolved finding carries
  every declared value that did not resolve, and a secrets hit carries `(line, rule)` so
  `report.py`, `processing._pre_agent` and both refusal routers never re-parse a message they
  wrote — the rejoined hit's line is EMPTY and its locator holds no line at all, so a router that
  recovered one by splitting the locator reported the page path as a line number. Plus
  `repairable` (default `True`): whether there is anything the agent could do differently at all. A
  gate that sets it `False` — SIX findings today: `zone/body-rewrite`, `zone/unparseable` and
  `zone/unreadable-edit` (each judges a MODIFIED page, which only `edits.apply_declared` produces),
  `secrets/unscanned-diff` and `pii/unscanned-diff` (a scanner could not run over one), and
  `zone/meeting-edit-refused` (fires only when `ctx.edits_allowed` is `False`, a caller-level fact
  the agent holds no tool to change) — takes the corrective
  retry away entirely (`gates.unrepairable`), because a pass spent on an unfixable finding is a
  certainty of the same refusal one agent run later. **Any ONE unrepairable veto stops the retry**,
  not only an all-unrepairable set: the retry exists to reach a pass with NO vetoes, and one that
  cannot clear makes that unreachable.
  **`gates.GateContext`** assembles everything a gate reads, built once per attempt by
  `processing.py` — the only caller, since the canon lane that built its own is gone.
  **Seven fields are FLOW-SCOPED** — a gate is TOLD a fact, it never infers one, applied to
  which write lane and which page shapes a run is allowed: `write_prefixes` (default
  `gates.ALLOWED_WRITE_PREFIXES` — the THREE ordinary fast-lane folders), `creatable_types` (default
  `page.FAST_LANE_TYPES`), `extra_folder_types` (default `{}` — a `{folder: type}` map for a type
  with no entry in the global `page.FOLDER_BY_TYPE`), `page_declared` (default `{}` — per-page
  `{page_type, anchoring}` declarations, since a meeting's decision pages each need their OWN
  anchoring outcome), `stamped_by_path` (default `{}` — per-page server-stamped values,
  since each decision page's `entity:` differs from its siblings'), `provenance_pages` (default
  `frozenset()` — paths where `content_hash`/`extracted_at` are a legitimate, server-stamped
  provenance group rather than a forged field), and `edits_allowed` (default `True` — the fast
  lane's additive-edit allowance; `process_meeting_item` alone sets it `False`, which is the ONLY
  thing that makes `gate_zone`'s `meeting-edit-refused` reachable). **Every default reproduces the
  ordinary flow's behaviour byte-for-byte.** There are now **TWO** callers that widen them, not one.
  `processing.process_meeting_item` widens to the meeting lane (`MEETING_WRITE_PREFIXES`,
  `MEETING_CREATABLE_TYPES`, `MEETING_EXTRA_FOLDER_TYPES`, all module-level constants in
  `processing.py`, plus the per-page dicts `_stamp_meeting` populates). **The source attachment is
  the second**: when `_source_attachment` returns a `SourceAttachment` — for a capture whose
  door asserted `source_client == "slack"` (the 🧠 gesture), or for a `kind="drive"` row —
  `process_item` widens `write_prefixes` and `extra_folder_types` by exactly that attachment's own
  folder (`sources/slack/` or `sources/drive/`) and `creatable_types` by exactly `source`, so CODE
  (never the agent) can write the verbatim thread or document as a `sources/` page
  beside the synthesis. With the attachment OFF, which is every other door, the `ctx` an ordinary
  capture builds is byte-identical to the pre-attachment one. Both callers widen on the
  `GateContext` instance they build, never
  by mutating a module constant or `page.FOLDER_BY_TYPE` globally. This is what keeps an ORDINARY
  capture claiming `type: meeting` parking with the existing reason (an out-of-bounds write for a
  lane nobody widened) rather than silently gaining the meeting flow's folders: **a new flow is out
  of bounds by default.**
- **`gates.gate_body_rewrite`** — a MODIFIED page may only gain `related:` links and callouts,
  proven against the base git blob rather than a rendered diff. Rule 0, checked BEFORE every
  span-based comparison: the new frontmatter must parse as real YAML at all (an indented line
  placed right after a flow-style `related:` list is otherwise silently absorbed as a continuation
  rather than read as a new field — closed the same "refuse what you
  cannot represent faithfully" way `gate_frontmatter`'s own `unparseable` finding already does).
  **This finding, and every other one this gate raises, is `repairable=False` unconditionally.**
  The canon lane's own proposals once drafted a modified page directly, so
  a caller who could fix it and propose again made `repairable=True` legitimate THERE; that lane is
  gone, and on the fast lane a modified page in the diff only ever comes from
  `edits.apply_declared`, which the agent cannot see or touch — so a corrective retry would burn the
  agent's ONE pass on a page it cannot write. `failed` after one pass is the honest answer, and the
  preserved diff is where the diagnosis lives. **The promotion carve-out this gate used to open is
  gone with it**: with `ctx.promotion` removed, rule 3 (every frontmatter line must survive byte for
  byte, except the single top-level `related:` block) has no exception at all.
- **`gates.gate_frontmatter`** — the frontmatter veto: `FORBIDDEN_PAGE_KEYS`
  (`owner`/`id`/`content_hash`/`tier`/`extracted_at` — the provenance group, which a fast-lane page
  may never declare) plus `_ALLOWED_KEY_RE`
  (`^[a-z_][a-z0-9_.-]*$`), which refuses **every** top-level key outside a plain lowercase ASCII
  identifier outright, whatever it resembles — closing the
  case/homoglyph/quoted-key spelling games categorically rather than by enumerating confusable
  spellings, which does not converge. `page.SERVER_OWNED_KEYS` (`submitted_by`, `verification`,
  `acl`, `content_hash`, `id`, `owner`, `status`, `as_of`, `entity`) is stripped from a capture's
  draft and re-stamped by `page.stamp_server_fields`; `page.duplicate_top_level_keys` is the
  raw-text, real-parser post-condition that vetoes a filed page carrying a server-owned key
  declared twice. **`verification` stays in `SERVER_OWNED_KEYS` even though nothing computes one
  any more** (the "a field nothing computes is not stamped" rule) — a
  forged `verification:` on a draft is still stripped here, it is simply never re-stamped with a
  real value afterward. Nothing READS the field either (the index column, the
  filter and the ranking demotions all went with it); this strip is not consistency
  housekeeping for a reader, it is the guard that keeps a deleted check from being asserted. This gate extends every check to MODIFIED pages too
  (`ctx.in_lane_modified_pages()`), not only newly-created ones: `owner` stays categorically
  forbidden on a modification, and `gates.PROVENANCE_PAGE_KEYS`
  (`content_hash`/`tier`/`extracted_at`/`id`) is legitimate ONLY on `ctx.provenance_pages` — the
  source-page PART(S) this run wrote, told to the gate by whichever writer produced them
  (`processing._stamp_meeting` for the meeting flow, `_one_pass` / `_stamp_attached_sources` for
  the fast lane's source attachment) and never inferred from the diff's own shape. See
  [`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md) for why
  those pages need a different provenance group than every other fast-lane page.
- **`processing.Result`** — `status`, `result_ref` (`<page path>@<sha>`), `report` (the dict
  `report.py` builds), `findings`, `diagnostics_path` (operator-only — deliberately not part of
  `report`, so it never reaches a submitter), and `outcome` (the agent's structured account, set
  only on a PARK and only when there is something worth re-filing — `None` everywhere else, which
  `queue.finish` reads as "this caller has no outcome and must not blank the column").
- **`report` reason codes** — every `rejected` report carries `reason_code`
  (`capture.schema.REJECTION_REASONS`: `secret`, `pii`, `duplicate`, `steering`, `steward`,
  `malformed-frontmatter` — `untraced-figure` went with ingest-time figure verification, since
  nothing produces that finding any more) beside its sentence, written through `report._rejected` so none can ship
  without one. It is the only signal a READ path may branch on: `capture.queue` withholds the
  excerpt and the client hints of a row refused for `secret`/`pii` (`schema.WITHHELD_REASONS`),
  which is what makes the refusal's own promise true. Do not branch on the summary's prose, and do
  not reach for `stage` — that field is `failed_system`'s alone.
- **`report.base_report`** shape (defined in `capture.schema`, re-exported here) — every terminal state carries: `status`, `summary`, `page_path`,
  `commit`, `anchored_to`, `links_created`, `overlaps_flagged`, `pages_edited` (what `edits.apply`
  actually changed — distinct from `overlaps_flagged`, which is the agent's judgment about what
  overlaps) — there is no `verification` key; it left this shape with everything that computed it —
  `agent_rationale` (the agent's own `Outcome.summary` — the one field
  that says WHY rather than what, and the only account of its judgment anything downstream has),
  `findings`, and `cost_usd` — the passes' real dollar spend summed from each pass's own figure
  (`processing._stamp_cost`, with the fault road carrying the same sum on the exception via
  `at_agent_attempt`). The rule, not an enumeration: present (possibly `0.0` — a park re-file, a
  fault before the first pass) on every outcome that passed through an agent loop or the failure
  road; absent only on `_pre_agent`'s own terminals — a duplicate, a `filed_retry`, a
  material-level secrets/PII rejection;
  plus per-state extras (`retry_of`, `open_question`,
  `stage` / `deliveries` / `agent_attempts`, `reply_invocation` / `unresolved_name` on
  `needs_input`, `asked` on `triage_entity`, `resolved_by` / `rejected_by` / `steward_note` on a
  steward's disposition).
- **`dedup.Match`** — a prior filed submission with the same content hash (`page_path`, `commit`
  derived from `result_ref`).
- **`config.Settings`** (frozen) — every tunable; see `docs/reference/librarian.md`'s configuration
  table for the full var/flag/default list. Resolved once by `from_args`, precedence CLI flag → env
  var → class default, `is None` (not falsiness) so an explicit `0` is never silently discarded.
- **`errors.LibrarianError`** hierarchy — `LibrarianConfigError` (the WORKER cannot run, raised at
  startup before any claim), `StaleBaseError` (a `LibrarianConfigError` subclass: the DEPLOYED
  worker resolved a base that did not come from the remote on a PER-ITEM fetch, after startup already
  passed — it keeps the config-error CONSEQUENCE, stopping the loop, rather than the ordinary mid-run
  softening into one `failed` row, because the fault applies identically to every row behind this one),
  `WorktreeError`, `GitError`, `LeaseLostError`, `AgentError`, and `OutcomeShapeError` (an `AgentError`
  subclass carrying `gates.Finding`s — the outcome file parsed and does not describe something the
  worker can act on). All carry `agent_attempts` and `agent_cost_usd` via
  `.at_agent_attempt(n, cost_usd=…)`, set on the way out of `processing.py` so a `failed` report
  can say how many agent passes ran and what they cost.
- **The outcome's THREE bounds, and the third one does not behave like the other two.**
  `agent.MAX_IDENTIFIER_LEN` (400, **refused** over it) for fields that NAME something the worker
  resolves; `agent.MAX_PROSE_LEN` (2000, **truncated**, never refused) for `summary` /
  `anchoring.reason` / a `note`. One bound for both refused a whole capture over the 401st character
  of a `summary`, on a field nothing downstream reads. Prose bounds truncate, the way
  `report._clean` already did. `agent.MAX_PAGE_BODY_LEN` (20000) is the third, and it is applied
  TWO ways on purpose: the meeting flow's page bodies TRUNCATE (`_prose(..., limit=…)`), while
  `Outcome.page.body` is **REFUSED** (`_page_body`). Prose truncates because nothing re-reads it; a
  page body IS the product, and a page cut off mid-sentence would pass every gate and stay that way
  in the repo forever. The asymmetry is declared rather than accidental — changing the meeting
  flow's is a behaviour change to a shipped flow and belongs there, deliberately.

## Tests

| Suite | Covers |
|---|---|
| `test_gates_unit.py` | every gate, pure — including the base-blob proof and the `related:`-must-grow rule |
| `test_edits_unit.py` | `edits.validate` / `apply` — the declared-edit vocabulary and its refusals |
| `test_gather_unit.py` | the deterministic gatherer, pure: entity resolution, ranked candidates, the link neighbourhood, the wikilink vocabulary, the `_confined` symlink filter, and that two gathers of one capture are equal objects |
| `test_page.py` | placement (`PAGE_TYPES`), path identity (`path_key`), frontmatter surgery |
| `test_report.py` | every terminal-state sentence, pure |
| `test_acl_rules.py` | the on-disk ACL dialect adapter |
| `test_config.py` | `Settings.from_args` resolution, the `is None` fix, `check_domains` |
| `test_githubapp.py` | JWT / installation-token minting (stubbed), `push_config`, commit identity |
| `test_agent_pure.py` | `parse_outcome`, the confinement helpers — no model, no subprocess |
| `test_structured_outcome_unit.py` | the outcome envelope's ADDITIVE half (ADR 033 D2): both shapes parse, `title`/`page_type` resolve from a single declaration site, `page.body` is REFUSED over `MAX_PAGE_BODY_LEN` rather than truncated |
| `test_filing_prompt_composition.py` | the ORDINARY preamble and the per-item prompt: the shared frame, the required `header`, and the two facts a structured caller declares |
| `test_gitcmd_unit.py` | worktrees, `reap` / `reapable` pid-scoping, the diff's blind spots, rebase-and-retry |
| `test_worker_unit.py` | `startup_checks`, `sweep`, the messages |
| `test_worker_signals.py` | real SIGINT/SIGTERM/SIGKILL against a real worker subprocess |
| `test_cli_once.py` / `test_cli_run.py` / `test_cli_status.py` | the three subcommands, including `status`'s three-verdict lease logic |
| `test_startup_preflight.py` | every `startup_checks` refusal, WITH its benign twin |
| `test_backend_retirement.py` | the retired-backend refusal (`agent.RETIRED_BACKENDS`), worded once and read by both `ensure_known_backend` and `worker.startup_checks` — plus its benign twin, a `pydantic` worker booting clean |
| `test_skill_reading.py` | `agent.read_skill` / `read_meeting_brief`'s refusals and `worker._check_skill_at`'s base-commit read — the coverage `test_agent_sdk_options.py` carried before the retired backend took it with it |
| `test_processing_pg.py` | the acceptance criteria, over real Postgres + real git |
| `test_structured_processing_pg.py` | the STRUCTURED ordinary shape end to end (ADR 033), over real Postgres + real git + a real pydantic-ai `Agent` on an offline model: confinement by construction (six hostile cases, nothing written), code deciding the filename, real dollars |
| `test_adversarial.py` | permanent cat. 1 / cat. 5 / cat. 7 cases, against the double |
| `test_finish_fencing_pg.py` | the `report` column is fenced by the same `attempts` token as `status` / `result_ref` |
| `test_frozen_linter.py` | the fixture copy of `stigmergy_lint.py` has not drifted from the real one |
| `test_testdb_guard.py` | the guard itself: `tests/testdb.py` refuses a non-test DSN before any connection |
| `test_operator_surface.py` | the docs (ADR / reference / runbook) match what the code's refusals actually name |
| `test_base_inputs.py` | `base_inputs.read_at`/`load_acl`/`load_registry`/`linter_at` are a pure function of `(repo, base)` — an uncommitted working-tree edit changes nothing, pure git, no Postgres |
| `test_refusal_routing.py` | `processing._refuse` / `_refuse_meeting`'s routing: the branch the double cannot reach (`zone/type-not-creatable`), proven by patching one table entry rather than waiting for a real double directive; and the secrets locator — both routers read `Finding.values`, never a re-parsed `locator`, over the real scanner's ordinary and REJOINED (no-line) shapes alike |
| `test_human_loop_pg.py` | the ask-back loop end to end: ask → `BrainService.reply` (the real service object, not a stub) → next pass files, identity enforcement, the one-ask budget across a requeue and a redelivery |
| `test_stale_base_pg.py` | `StaleBaseError`: a REAL failed fetch (the remote rewritten to an unreachable path) walks a per-item base back to the local branch, and the worker refuses rather than filing against a stale clone |
| `test_acl_per_item_pg.py` | the ACL config is re-read at EACH item's own base commit, proven on the stamped page at the end of a real run — not only on `base_inputs.load_acl` in isolation |
| `test_bootstrap.py` | `stigmergy-librarian-boot`: `verify_checkout_at_base`'s three real-git refusals, and `worker_env` as a pure `dict -> dict` (assertable with no container, no Fly, no key) |
| `test_entity_full_circle_pg.py` | the governed-birth spine: unregistered entity → ask-back → steward approves via `stigmergy-entities` from a SEPARATE clone → the originating capture re-files anchored to it, no worker restart. Lives here rather than under `tests/entities/` because the fixtures (bare remote, double agent, `capture_queue`) are this directory's |
| `test_meeting_brief_contract.py` / `test_meeting_processing_pg.py` / `test_meeting_queue_fencing_pg.py` / `test_meeting_outcome_reuse_unit.py` | the meeting flow — the brief↔gates contract, real filing, per-item queue fencing, outcome reuse |
| `test_librarian_brief_contract.py` | the ORDINARY brief↔code contract, the same two-sided rule table over the frozen copy in `fixtures/repo/`, plus the drift test against the knowledge repo's own |
| `test_gated_commit_wiring_pg.py` | the gate-run-to-commit wiring, end to end, over real Postgres and real git |
| `test_source_attachment_pg.py` | the 🧠 gesture's own `sources/slack/` page: attached only when the capture crossed the Slack door (`source_client`), carrying the permalink as `url:` — the one fast-lane write outside the three ordinary folders |
| `test_drive_processing_pg.py` | the drive sibling: kernel-hands conversion over the real `pdftotext` binary, then the ordinary fast lane over the extracted text |
| `scripts/e2e_librarian.py` | a HOST worker against a real bare git remote, from empty volumes |
| `scripts/e2e_librarian_container.py` | the DEPLOYED image's worker — clone, file to the remote, SIGTERM, mid-item SIGKILL + lease redelivery |

No test needs an API key; the agent step runs against `double.DoubleAgent`, which misbehaves on
demand (hallucinate, seed a secret, inject, escape, delete, rewrite, write a shape the outcome
boundary refuses) and behaves perfectly on ordinary material. `DOUBLE:bad-shape-once` is what drives
the corrective retry for the outcome's own shape — bad shape on pass 1, good on pass 2, page filed —
and `DOUBLE:long-summary` is its benign twin (a summary far past the prose ceiling must be truncated
and FILED, in one pass). `DOUBLE:triage-entity` is the one directive a REPLY can
override: with an answer naming something the worktree's registry resolves it files, anchored to the
REGISTRY's spelling; with anything else it parks again — which is what makes the whole ask-back loop
(and its one-ask budget) exercisable offline and keyless. The double is routed through `agent.confined_write` — the same rule a tool-holding agent's
`PreToolUse` hook enforces — so the whole offline suite proves something about the production path
rather than about a second, untested implementation of the same rule.

## Common tasks

| Task | Touch |
|---|---|
| Add a new gate | a `(ctx: GateContext) -> list[Finding]` function in `gates.py`, added to `ALL_GATES`. Decide its finding's `brief` (what a repair looks like) — or `repairable=False` if there is none, so it does not spend the retry |
| Add a new declared-edit kind | `page.CALLOUT_STYLES` / `EDIT_KINDS`, `edits.validate` + `apply`, and `double.py`'s directives so the offline suite can stage it |
| Change the outcome JSON shape | `agent.parse_outcome` (bounds + coercion), `pydantic_backend.FilingAccount` (the structured mirror — mirror its REQUIREDNESS in the `model_validator` too, or the framework's own retries never fire and the worker's one corrective pass pays for a shape the framework would have repaired free) AND `double.py` (must keep emitting a shape `parse_outcome` accepts — the double is not exempt from the boundary). Decide which BOUND the field takes — identifier → refused, prose → truncated, a page BODY → refused (`_page_body`, because a clipped page body is the product mutilated) — and whether a missing one is a `missing-field` finding. A field only ONE ordinary shape declares is additive: `parse_outcome` accepts both and `processing._require_page_content` decides which half is owed, from the backend's declaration |
| Add a fourth creatable fast-lane type | `page.PAGE_TYPES` (give it a `folder` + a `label`) — the zone gate, the write-confinement regex, `LANE_FOLDERS` and the triage sentence all derive from it. Adding a NON-creatable type means a `reason` and no folder |
| Tune a bound (turns, tool calls, timeout, poll interval…) | `config.py` (field + `from_args` resolution); document the var in `docs/reference/librarian.md`'s configuration table |
| Change what a person reads about a refusal | `report.py` ONLY — the CLI and `brain_submissions` share it; never compose wording at either call site. A sentence a STEWARD authors (`resolved`, a human `rejected`) belongs in `capture.dispositions` instead, for the layering reason above |
| Change WHEN the submitter is asked vs when the steward is | `processing._triage` / `_ask_or_park` — the routing is code's, from the agent's declared outcome, and the agent's schema does not change. The one-ask budget is `capture_queue.asked_at`, never a counter in this process |
| Add a new refusal | a builder in `report.py` going through `_rejected`, with a code in `capture.schema.REJECTION_REASONS`. Decide whether the capture's own material may still be read back: if not, the code joins `schema.WITHHELD_REASONS` and `capture.queue` does the rest |
| Add a fail-closed startup check | `worker.startup_checks`, raise `LibrarianConfigError`; add its row to the runbook's refusal table |
| Add an agent backend | a class satisfying `filing_port.FilingAgent` in its own module — including the `structured_ordinary` declaration, which decides whether `processing` gathers for it and which half of the outcome envelope it owes — a value in `agent.BACKENDS`, a branch in `build_agent` (lazy import, so the other backends' frameworks stay unloaded), a place in `agent.SKILL_READING_BACKENDS` if it injects the brief, and — if it reports tokens rather than dollars — a row in `pricing.PRICES`. If it cannot serve every flow, refuse it in `worker.startup_checks` rather than letting a worker discover that one capture at a time |
| Change what the STRUCTURED shape is handed | `gather.py` only — it is a pure function, so a change is unit-testable without a model. Tune the two dials in `config.Settings` (`gather_top_k`, `gather_excerpt_lines`) rather than in the gatherer; render it through `agent.render_gathered`, never with a fence built somewhere new. A new FIELD read off the filesystem goes through `_confined` like every other, and a new field that scales with page CONTENT owes `agent.MAX_GATHERED_CHARS` a way to trim it — the per-field bounds multiply, and two of the factors are an operator's to set |
| Change what a model costs | `pricing.PRICES` **and** `pricing.AS_OF` in the same edit, or `$STIGMERGY_LIBRARIAN_PRICING` for one deployment. Never a literal at a call site — the same "model ids are configuration" rule, applied to their prices |
| Read another file out of the knowledge repo | `base_inputs` — a wrapper over `read_at`, never `open(settings.<x>_path)`. Decide what ABSENT means for it and say so in the wrapper's docstring |
| Change what the deployed worker needs | `bootstrap.py` (the checkout, the verification, the env it execs with) **and** `fly.toml`'s `worker` process group **and** `docker-compose.yml`'s `librarian` service — the composition exists so the artifact is exercised before staging is |
| Change worktree / diff mechanics | `gitcmd.py` — `--text` and `core.quotePath=false` are load-bearing on every diff invocation, not cosmetic |
| Change the ORDINARY brief | the knowledge repo's `.claude/skills/librarian/SKILL.md` — never a copy here. Grep it against `tests/librarian/test_librarian_brief_contract.py`'s rule table in BOTH directions before shipping either side alone, and resync BOTH frozen copies: `tests/librarian/fixtures/repo/` (the drift guard, resynced always) and `evals/filing/repo/` (the yardstick, re-frozen deliberately, with `FROZEN_SHA256`, `PROVENANCE.json` and a fresh baseline row in the same landing). The brief is backend-NEUTRAL: a backend that departs from it says so in its own ENVIRONMENT paragraph, on this side (`pydantic_backend.ORDINARY_ENVIRONMENT`, composed through `agent.build_filing_header`) |
| Change the meeting flow | `processing.process_meeting_item` and its private helpers (`_one_meeting_pass`, `_stamp_meeting`, `_cross_check_meeting_outcome`, `_file_meeting`, `_refuse_meeting`); `agent.build_meeting_prompt` and `agent.read_meeting_outcome` for the item's prompt and its account, and `pydantic_backend.MEETING_ENVIRONMENT` for what the agent is told about its own environment (the tool allow-lists went with the backend that held tools); the brief lives in the knowledge repo (`.claude/skills/meeting-distiller/SKILL.md`) — grep it against `tests/librarian/test_meeting_brief_contract.py`'s rule table in BOTH directions before shipping either side alone, because a two-sided contract shipped one side at a time is how the two silently disagree |
| Change the view-regeneration trigger after a meeting files | `processing._file_meeting`'s `views_regenerate` block ONLY — keep it best-effort (never let a view fault taint the meeting's own `Result`) and keep the import pinned to `stigmergy.views.regenerate` (`test_librarian_may_only_import_views_regenerate`); the trigger's own logic (which entities, staleness, the commit) lives in `stigmergy.views.regenerate` — see [`views/index.md`](../views/index.md)'s Common tasks row for that half |

## Notes

- **Agent judges, code vetoes, and the diff is the single unit of veto** — the organizing decision
  behind this whole package. Design record:
  [ADR 015](../../../docs/decisions/015-librarian.md).
- **"Additive edits to an existing page" is split into agent DECLARES / code PERFORMS.** It was one
  thing once — the agent edited directly and a gate refused anything non-additive — until the agent
  rewrote a human page's body twice in a row (the original attempt and its one corrective retry).
  `edits.py`'s module docstring carries the full account.
- **A later security pass closed two collision classes that split did not touch**:
  the worktree reap now scopes by repo AND by the pid that created each worktree
  (`gitcmd.reapable`) — it used to sweep anything under the shared temp root sharing the naming
  prefix, and could delete a live sibling's in-flight worktree; and path identity (`page.path_key`)
  now folds case and Unicode normalization form, because an exact byte comparison against `git`'s
  tracked-path spelling let a re-spelled existing page pass as "new" on macOS/APFS — regaining, from
  a re-spelled name, the very write-to-an-existing-page capability the split above was meant to
  remove.
- **The agent framework is pinned exactly**, the same discipline every framework gets elsewhere in this
  repo: the framework's API drives tool wiring, so a float would let a minor version bump change
  behaviour between CI (which never imports it — see the double) and a live run.
- **[ADR 026](../../../docs/decisions/026-the-purge.md) removed the trust layer and the canon lane's
  reach into this package — read the ADR first, not this file, for the reasoning.** What used to be
  a "second, declared caller" (`stigmergy.server.canon` running these same gates over a canon-lane
  worktree) is simply gone: `gates.py` has one caller, and `GateContext` carries no field a
  removed lane's callers ever set. Where this file narrates something as removed rather than
  silently deleting the paragraph, that is deliberate — a future reader must not wonder whether
  `gate_trace`, the playbook read, or the promotion carve-out were forgotten rather than removed on
  purpose.
- **The fast lane creates three page types**, not more: `note`, `decision` and `concept`.
  `customer` and `product` were once creatable, but they were entity KINDS masquerading as page
  types and the registry already carries `type` per entity. Every list and every regex derives from
  `PAGE_TYPES` — `FAST_LANE_TYPE_LIST` is literally `", ".join(FOLDER_BY_TYPE)` — so narrowing the
  tuple narrowed the operator-facing messages with it, and a fourth type would widen them the same
  way. Trust the tuple, never a sentence that restates it.
- **The skill is not in this package.** `.claude/skills/librarian/SKILL.md` lives in the knowledge
  repo, read by `agent.read_skill` out of the exact commit the worktree branches from
  (`worker._check_skill_at`), and injected into the system prompt rather than loaded through any harness's
  own settings mechanism — loading it as `project` settings once booted the knowledge repo's
  `.mcp.json` servers and hung the run forever (`agent.py`'s module docstring has the full account).
  **It is backend-NEUTRAL since ADR 033**: it describes a worker that hands the agent its context and
  writes the page from one structured account. Tool mechanics, for a backend that has any, would live
  in the per-backend ENVIRONMENT preamble composed by `agent.build_filing_header` — the retired SDK
  backend carried a NAMED override note there, saying its run departed from the brief; no backend
  needs one for the ordinary flow today, since the brief itself now describes the structured shape.
  The direction that note took was the inversion worth noticing while it ran: in ADR 032 the brief
  was the tool-holding text and the structured backend carried the correction; then the brief became
  the structured text and the SDK carried the correction, until it retired.
- **This file's structure matches [`evals/index.md`](../../../evals/index.md)** — the closest
  existing precedent to the per-directory code-map format
  (`docs/reference/hybrid-index.md` is narrative/schema-first instead, for a different audience).
- **The meeting flow's design record is [ADR 020](../../../docs/decisions/020-meeting-distiller.md).**
  It records the decisions this package's comments could only show one at a time: a second flow
  rather than a branch, multi-page atomic filing, per-page anchoring, the provenance stamp the fast
  lane's own stamp deliberately omits, flow-scoped `GateContext` fields defaulting closed. The purge
  left it untouched — the meeting flow does not read the trust layer, the supervisor's playbook, or
  the canon lane.
