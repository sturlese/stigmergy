# ADR 028 — The Drive door: a caller, not a flow

Status: accepted · 2026-08-03
**Superseded by** [ADR 044](./044-the-capture-is-the-approval.md) D4: there is no Drive door. A document reaches the brain as text a client already holds, through `brain_submit(kind="document")` — no Google credential exists server-side. The two standing rules this ADR set (door never mirror; caller not flow) outlived the door itself and still hold.

Two standing rules box this in. **Door, never mirror**: extracted text lands as a `sources/`
page, the ORIGINAL BYTES go to the evidence plane too, and the binary stays in Drive.
**Caller, not flow**: Drive enters as a CALLER of existing machinery, and the
meeting↔drive extraction question is decided here, with the duplication measured rather
than assumed.

## The gesture

Get a deck from Drive into the brain as anchored pages: one operator command, and a
real PDF (or a native Google file) ends as verbatim source parts under `sources/drive/`
plus a synthesis page anchored to a registry entity, the original bytes archived in
evidence, the Drive link one click away on the source page.

## D1 — The measurement: the extraction already happened

The central question — "do the meeting flow and the drive path merge into one generic
document flow?" — dissolves under measurement:

- **The document-flow output contract Drive needs is the fast lane's source-attachment
  parameter, already live.** It was built for the Slack 🧠 gesture — the extracted piece,
  the source-page writer, was born there and Drive reuses it: the ordinary fast-lane
  agent files ONE synthesis page, and CODE attaches the verbatim material as
  `sources/<door>/` part pages through `_build_source_parts` — parametrized with
  `source_kind`/`tags`/`url` explicitly for this second caller — stamped by
  `_stamp_attached_sources`, cited by `_stamp`'s `cite_stem`, all in one atomic commit.
- **The meeting flow's ~2.4k lines are meeting-shaped, not document-shaped**: attendee
  lists, action items, N decision pages with per-page anchoring, the meeting page as
  provenance hub. A deck produces none of that. Merging the two flows would touch every
  line of the meeting flow to extract a generality with exactly one hypothetical
  consumer — the speculation the caller-not-flow rule exists to forbid.

**Ruling: no fusion, no third flow, no new flow at all.** Drive rides `process_item`
with the source attachment ON — the parameter's second caller, exactly the seam that
rule froze. New code is confined to: a door CLI, a conversion step at the worker, a
`drive` branch in `_source_attachment`, and schema plumbing (kind + hints + a second
blob ref).

## D2 — The door: an operator CLI over the operator's own Google auth

`stigmergy-drive drop <file-id-or-url>` — an operator CLI in the `stigmergy-meeting` mold
(direct DB + bucket access, no MCP transport, run from the operator's own terminal). It
resolves the file through the `capture.drive_client` seam, whose production
implementation (`GogDriveClient`) shells out to the operator's locally-authenticated
`gog` CLI (`gog drive get` for metadata, `gog drive download` for bytes — the same
command exports native Google formats). No
Google credential ever reaches the server or the worker: the worker converts from the
evidence blob and never talks to Drive. Webhook/watcher stays future work.

The door runs **no model and no conversion**. Validate → fetch →
upload → insert: every refusal (unsupported format, over-cap file, empty file, an
office format while its wake condition sleeps) happens BEFORE the first byte is
uploaded, so "no row and no object" holds — `stigmergy-meeting drop`'s own discipline.

## D3 — What the row carries: manifest as material, bytes as a second blob

`prepare_submission`/dedup/`_material` are text-shaped and stay that way. A drive
submission's `material` is a small deterministic **manifest** (name, Drive file id,
webViewLink, mime, modifiedTime, sha256 + byte size of the fetched bytes); the
ORIGINAL BYTES go to evidence as a **second blob ref** (`queue.submit` gains an
additive `extra_blob_refs` parameter; MCP callers never pass it).

Consequences, each deliberate:

- **Dedup works by content**: the manifest embeds the bytes' sha256, so re-dropping an
  unchanged file collapses/rejects on the existing levels 1–2 unchanged, and a file
  EDITED in Drive (new bytes) is honestly a new capture.
- **Door-never-mirror lands literally**: evidence holds the pre-gate verbatim (the
  bytes); the reader-facing raw layer is the `sources/drive/` page set; the binary stays
  in Drive, linked as `url:` on every source part.
- This is the codebase's **first multi-blob capture** — the standing open question about
  `_material` reading `blob_refs[0]` only is settled by design here: blob 0
  stays the text material (the manifest), blob 1 is the bytes, and the drive path
  reads its bytes explicitly by index 1, stated in code.

## D4 — Conversion at the worker: deterministic hands, one bounded vision fallback

`process_drive_item` (a thin sibling in `processing.py`) fetches the bytes, converts,
then delegates to the SAME `process_item` path with the extracted text as material —
`_pre_agent`'s dedup and secrets/PII scan run over exactly what could reach a page.

- **Text layer first** (kernel hands, unchanged contract): `converters.method_for_ext`
  → `extract` — pdf (pdftotext), sheet (xlsx/xls/csv/tsv), docx, text (txt/md/json).
- **`vision_extract` as the bounded fallback**, decided by CODE, not by an agent: a
  PDF whose text layer yields fewer than a threshold of characters per page is treated
  as scanned and gets ONE Gemini OCR call. No `GEMINI_API_KEY` → the honest refusal
  names the missing capability. One call per document, ever.
- **Conversion failure is a named stage** (`conversion`), a `failed` row with an
  honest sentence — never an exception loop, never a submitter-blaming report.
- **Office formats stay behind the Gotenberg flag**: the `office` converter path exists
  and works wherever `GOTENBERG_URL` points at a live container (compose dev), but no
  Gotenberg service ships on staging and the DOOR refuses `.pptx`/`.doc`/`.odt`/… with
  a sentence naming the wake condition — the first real `.pptx` that matters. Native
  Google files never need it:
  the door exports them to PDF at fetch time (`gog drive download --format pdf`), so
  the evidence blob is a faithful PDF and the worker sees `.pdf`.
- **Extraction over the material cap refuses honestly** (mirror of
  `MAX_MATERIAL_BYTES`): the brain files documents, not databases; sheets are already
  profiled (`SHEET_MAX_ROWS`), and the parts-split bounds pages, not prompts.

## D5 — kernel `doctools.py` is deleted

ADR 026 kept `converters` + `doctools` by name for this door. This decision takes the
converters and rejects doctools' shape: it exists for an agent that ORCHESTRATES its
own extraction (`read_more`/`ocr` as agent tools over a `DocContext`), and D4 decided
extraction is code's, deterministically, before the agent ever runs. Zero callers — its
unit tests exercised an organ nothing calls, the dormant-organ class ADR 027 named — and
the standing rule that "kept for the shape" is speculation dressed as design points the
same way; the tests go with it. `vision_extract` — the engine doctools wrapped —
survives in `converters` as a live, called, tested function. This ADR preserves the
shape if an agent-orchestrated extraction is ever earned.

## D6 — chain identity is stamped by the producer

Homed here because this change touches the producer anyway: `_build_source_parts` now
writes an explicit frontmatter `id:` on every part — `id: <stem>` for part 1,
`id: <stem>#p<n>` for the rest — the Karpathy `#p<n>` sub-identity convention, declared
instead of inferred. The provenance stamp (`stamp_source_fields`) owns the field;
`gate_frontmatter`'s provenance-page group admits it exactly like
`content_hash`/`extracted_at` (told, never inferred). The index reads `id:` as the chain
key where present; the earlier filename inference (`-pN` regex + directory key) steps
down to belt-and-braces for historical pages.
Consumers: `index.corpus` (chain key), `index.rank` (chain collapse),
`answer`'s directory-gated propagation — each keyed on the explicit id first.

## D7 — What the agent sees, and what it may not

The fast-lane agent keeps its skill, its outcome schema, its gates and its one
corrective retry. A drive capture reaches it as material (the extracted text) plus
hints — **plus one server-composed flow fact**: the first REAL drive capture parked as
"this reads like a source page" — the brief's genre rules, correct about a bare
document and wrong about one whose source half code had already taken (the attachment
was designed invisible to the agent, and for document-shaped material that invisibility
was the bug). `build_prompt` now carries a `flow_note` on attachment-ON captures —
instruction-side, the corrective brief's own standing: the verbatim source set is the
system's; the agent's whole job is the one synthesis page. Told, never inferred; an
ordinary capture's prompt is byte-identical to before. The flow key is the ROW'S OWN
`kind == "drive"` — server-asserted by the door, unreachable from MCP
(`MCP_SUBMIT_KINDS` stays `("raw", "page")`) — never a client-forgeable hint. The new
`DRIVE_HINT_KEYS` (file id, name, url, mime, modifiedTime) follow `SOURCE_HINT_KEYS`'
additive allowlist pattern; `drive_url` lands on a reader-facing page (`url:`), so the
pair that decides/decorates the attachment is refused at the client seam for every
door, `reject_source_provenance_hints`-style — the third application of that pattern.

## D8 — "the same capture resumes" is a qualified promise, not a universal one

Resuming the SAME capture by reusing its stored distillation is the document flow's
property (`_with_park_outcome`); the fast lane re-runs the agent after a requeue — and
a parked drive capture additionally re-converts (deterministic, cheap; the one vision
call recurs only for scanned decks). Ruled: state the qualification rather than grow
the fast lane speculative outcome storage nothing has asked for.

## Deliberately absent

A Drive mirror or sync · a server-side Google credential · a webhook (future, named) ·
Gotenberg on staging (flag + wake condition) · a new librarian flow or brief ·
agent-side extraction tools (D5) · any change to gates, budgets, ranking, or the answer
path beyond D6's explicit-id reading.

## Acceptance

1. `stigmergy-drive drop` refuses — before any upload — an unsupported format, an
   office format (naming the wake condition), an over-cap file, an empty file; each
   with no row and no object.
2. A real PDF drops end to end on staging: manifest + bytes in evidence, one queue
   row, worker converts, agent files ONE synthesis page anchored to a registry
   entity + N `sources/drive/` parts, one atomic commit, `url:` = the Drive link.
3. A native Google file (Slides) exports to PDF at the door and files the same way.
4. A scanned-PDF fixture takes the vision fallback (or refuses honestly keyless);
   a conversion fault is a `failed` row naming the `conversion` stage.
5. Every source part written by ANY caller (meeting, Slack, drive) carries its
   explicit `id:`; the index collapses chains by declared id; golden holds (gates
   green, `make gates`).
6. `kind="drive"` is unreachable through `brain_submit`; asserting drive provenance
   hints through MCP is refused loudly.
7. Suite green; the kernel converters gain their first real CALLER (their offline
   unit suite predates it — `tests/kernel/test_converters.py`); `doctools.py` is gone,
   its tests with it, and a grep for it comes back empty.
8. The end-to-end demo: a real deck from Drive, dropped, filed, and asked about with
   one real `ask` — answer `verified`, citing the new pages.
