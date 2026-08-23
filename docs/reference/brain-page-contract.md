# Brain page contract

The frontmatter dialect the `sources/` corpus is written in: Markdown with YAML frontmatter. This
is the interface consumed by the brain server and by MCP clients — treat it as an API. The hybrid
derived index ([`hybrid-index.md`](hybrid-index.md)) is a consumer too, with its own tolerant parser (the packages
share no code); it reads a **subset** of what follows into the queryable columns of `pages_index` —
`type`, `title`, `id`, `status`, `entity`, `owner`, `tier`, `as_of`, `acl`, `tags`, `mentions`,
`supersedes`, `superseded_by`, and `extracted_at` as a fallback for `updated`
(`index.corpus`). Everything else below is read by humans and by clients, not by the index.

**Read this as the READ contract.** The live writers mint a narrower subset, but real pages
already committed under `sources/` carry the full dialect, and `stigmergy.index`'s tolerant
parser and the librarian's stamping code all read it. Treat
every field below as "what this page means," not "what gets written next."

**What actually writes a `sources/` page today** is one function,
`librarian.processing._build_source_parts`, called once per capture from one place: every capture
archives its material verbatim, and the door and the kind choose only the folder —
`sources/slack/`, `sources/documents/`, `sources/meetings/` or `sources/notes/`.
It drafts `type`, `title`, `source_kind`, `url`, `tags`, `related`, `sources`, and
`page.stamp_source_fields` then overwrites `status`, `as_of`, `submitted_by`, `content_hash`,
`extracted_at`, `tier` and `id` on top of whatever the draft said. That fourteen-field shape is the
whole of the live provenance dialect; nothing emits `representation`, `extraction_quality`,
`source_format`, `contextual_retrieval`, `detail_in_source`, `extraction_method`, `source_uri`,
`source_file_id`, `source_name` or the folder-derived `entity_kind`/`entity_aliases`/
`entity_status`/`unit`/`period`/`seq`/`stage` any more. They are documented below because pages
carrying them are still in the corpus and still get read. (`entity_type` is a different field and
is very much live: the librarian writes it on every entity page it creates, and it becomes that
entity's `type` in `ops/entity-registry.json`.)

**`entity:`'s own rule — what it means, who may write it, and the fast lane's use of it — is
documented in full at [`page-contract.md`](page-contract.md).** This file keeps the field in
the frontmatter example below and the `pages_index` mechanics table; that one is the ruling.

**`type:` below is a free-form vocabulary, and it is not the live one.** The old writer let the
model choose a kebab-case doc type per document, which is why `meeting-notes` appears
in the example. Today the vocabulary is a closed table of four — `note`, `concept`,
`entity`, `source` (`librarian.page.PAGE_TYPES`, mirrored by the knowledge repo's
contract linter) — of which the fast lane may CREATE only the first two. An older page carrying a
free-form type is read exactly as written; nothing mints one any more. See
[`librarian.md`](./librarian.md#two-types-the-fast-lane-may-create-four-it-knows).

```yaml
---
type: meeting-notes                  # kebab-case doc type (LLM-chosen) — the OLD vocabulary; see above
title: Q1 board minutes
date: 2026-03-14                     # content date (optional, LLM-proposed)
as_of: 2026-03                       # content validity time at the finest PROVABLE granularity
supersedes: "drive:1OldDoc"          # this page is the CURRENT version of that document
superseded_by: "drive:1NewDoc"       # a newer version exists — prefer it for current truth
tags: [board, minutes, q1]
id: "drive:1AbC..."                  # stable id
source_file_id: 1AbC...              # handle for opening the original (Drive MCP, links)
source_uri: "https://drive.google.com/file/d/1AbC.../view"
source_kind: google-drive            # contract enum — see the field notes below
source_name: 2026 Q1 minutes.pdf
content_hash: "sha256:..."           # provenance: sha256 of the mirrored raw source bytes
extracted_at: "2026-04-01T12:00:00Z"
representation: full                 # full | digest | minimal
extraction_quality: usable           # usable | manual_review
source_format: pdf                   # pdf | spreadsheet | document | office | text | other
contextual_retrieval: title          # embedding-context tier: prepend title to each chunk
tier: 1                              # 1 primary, 2 second-hand, 3 AI-generated
acl: [sales, leadership]             # audience GROUPS; absent = open, [] = nobody
# ── the three lines below are HISTORICAL: the trust layer that produced them is deleted,
#    nothing writes them, and nothing reads them. Pages written before it went still carry
#    them; a value is a frozen fact about that extraction run, never a live guarantee.
verification: verified               # (historical) verified | partial | failed
unverified_numbers: ["9.9M"]         # (historical) figures that couldn't be traced to the source
unanchored_numbers: ["512000"]       # (historical) figures tied to a period the source contradicts
unverified_mentions: [Ghost Corp]    # advisory: mentions not literally found in the source
detail_in_source: true               # spreadsheets only: exact figures live in the source
extraction_method: vision            # the agent escalated to its ocr tool (+ ocr_model: <model>)
entity: initech                      # resolved from the folder path, NOT by the LLM — a bare
#                                       string OR a list (page-contract.md's dialect);
#                                       normalized to a one-element list either way
entity_kind: tracked                 # tracked | prospect
entity_aliases: [Initech, S.L.]      # when the folder name differs from the slug
seq: 3                               # entity's folder number, when present
entity_status: won                   # entity PIPELINE status from the folder name
stage: Evaluating                    # prospects only
unit: Sales                          # org unit (top-level folder)
period: 2026-Q1                      # year/quarter detected in the path
mentions:                            # unresolved entities — the graph stage links them
  - { name: Initech, type: company }
  - { name: Jane Doe, type: person }
---

# Q1 board minutes

...body...
```

And what a `sources/` page written **today** looks like — every field either drafted by
`_build_source_parts` or stamped over it by `page.stamp_source_fields`, nothing else:

```yaml
---
type: source
title: "Q1 board call — transcript"
source_kind: meeting                 # or `slack`, or `upload` for a submitted document
url: ""                              # the thread permalink / the `source_url` hint; "" when none came
tags: [source, meeting-transcript]
related: []
sources: []
status: developing                   # ── everything below this line is server-stamped
as_of: 2026-03-14                    # the day this capture was filed
submitted_by: ana@example.com
content_hash: "sha256:..."           # of the archived material bytes, recomputed from what this run read
extracted_at: "2026-04-01T12:00:00Z"
tier: 1
id: "q1-board-call-transcript"       # `<stem>` for part 1, `<stem>#p<n>` after
---
```

Note `url:`, not `source_uri:`: the live writer emits the shorter spelling, and both are in the
corpus.

## How a client should read it

`as_of` and `acl` are stamped by the librarian on every page it files — `acl` from the audience
the DOOR decided for that capture, so every page one capture writes and the verbatim `sources/`
page beside them carry the same one. An entity page is the exception and carries none: the
registry is the brain's shared vocabulary. `supersedes`/
`superseded_by` are written by a human editing a page (the gardener flags a candidate pair and
names no command, deliberately). The rest of the table is how to read a page that already carries
the field — no writer produces one now.

| Field | Client behavior |
|---|---|
| `detail_in_source: true` + `source_format: spreadsheet` | page is a summary of a *live* sheet — open the source (via `source_file_id`) for exact/current figures |
| `representation: digest` / `minimal` | summary/pointer; detail is in the source |
| `extraction_quality: manual_review` | page carries a warning banner; extraction was lossy — offer to open the original |
| ~~`verification: failed` / `partial`~~ | **HISTORICAL.** Produced by nothing and read by nothing — not indexed, not filterable, not a ranking factor. A value on an old page describes the extraction run that wrote it, nothing about the page today. |
| ~~`unverified_numbers` / `unanchored_numbers`~~ | **HISTORICAL**, same layer, same status. |
| `extraction_method: vision` | body came from OCR (`ocr_model` says which); trust accordingly. Nothing sets it today, and nothing extracts anything either: a document reaches the brain as text its CLIENT already extracted, so how it was read is a fact only the submitter has |
| `mentions` | names the page refers to without anchoring to them. Still read: `index.corpus` folds them into the page's searchable text, so a mentioned name finds the page. Nothing writes them any more |
| `as_of` | when the CONTENT is valid (`YYYY[-MM]` or `YYYY-QN`), only as precise as the source proves — rank current truth with it |
| `superseded_by` | a newer version of this document exists — demote for "current" questions; keep for history/"as of" questions |
| `supersedes` | this page is the current version in a chain |
| `acl` | audience labels — a serving layer must show this page only to clients holding one of them. Absent = open to all; an **empty** list = visible to nobody. The hybrid index stores the labels verbatim (malformed shapes fail closed); enforcement lives in the MCP server (`stigmergy.server.acl.visible`) — see [`server.md`](server.md) |

## Body rules

- **A source page's body is the material, byte for byte.** `_build_source_parts` emits it verbatim
  and adds exactly three things: the `# H1` from `title`, and the `Continued from [[…]]` /
  `Continues in [[…]]` lines on a split part. Nothing paraphrases it, and nothing may — a model
  copying a transcript back out can drop, reorder or normalise a line, and the page that is supposed
  to be the ground truth would then be a lossy copy of it.
- **Wikilinks in a body are ordinary and expected.** They are how the graph is built here; there is
  no separate linking stage that owns them. What holds them honest is `gate_contract`, which runs
  the knowledge repo's own contract linter over the diff at the item's base commit and vetoes a dead
  `[[target]]`, and the librarian, which writes the `related:` entries on a page it files from
  a declaration rather than letting the agent edit it.
- **Nothing rewrites a body for safety.** No code strips a second `# H1` and none neutralizes a
  stray `---`; the frontmatter is protected instead by `gate_frontmatter`, which re-parses what was
  actually written and refuses an unparseable or forged block outright. A rewriter that silently
  fixed a page would be changing content nobody reviewed.
- **No ingest-time figure verification exists**: no code computes a `verification` value for a
  NEW page. Deterministic figure checking lives at ANSWER time, cites-or-refuses (see
  [answer.md](./answer.md)). A `verification` value on an old page is a frozen, historical fact
  about that extraction run, not a live guarantee.

## Oversize documents: split parts

A body over 150 content lines violates the page-as-chunk contract, so an oversize document is
split into cross-linked parts rather than emitted as one page — for **any** body over the cap,
whatever its `representation`, and the split preserves all content:

- part 1 keeps the document's slug, `id` and `title`; part *n* gets `id: "<id>#p<n>"`,
  `title: <title> (part <n>)` and the slug suffix `-p<n>`;
- every part carries the document's full provenance frontmatter;
- parts link to their neighbours with `[[wikilinks]]` ("Continued from / Continues in"), so
  the chain is navigable and link linters see no dead ends;
- the extra part paths are recorded, so dedup, deletion propagation and rename cleanup treat the
  chain as one document.

**`stigmergy.kernel.page` owns the two numbers** (`MAX_BODY_LINES` 150, `SPLIT_CHUNK_LINES` 140),
and the librarian imports them rather than re-declaring them, because a splitter and the contract
linter that judges its output must agree or the split is pointless — the two literals had already
been found drifting as independent copies. `librarian.processing._build_source_parts` is the live
splitter: it writes every capture's archived material — a transcript, a Slack thread, a submitted
document, a pasted note — as N ≥ 1
cross-linked parts under exactly this contract, preferring a blank-line boundary up to 30 lines
back and never breaking inside a fenced code block.

On a source page the FILENAME stem carries the `-p<n>` suffix, because a wikilink target must be a
filename — but the part identity is **declared, not inferred from it**: the splitter computes
`<stem>` for part 1 and `<stem>#p<n>` after, and `page.stamp_source_fields` writes that as a quoted
`id:` (an unquoted `#` would start a YAML comment). `index.corpus` prefers the declared `id:` over
the stem, so the chain collapse keys on a fact and the filename inference is only belt-and-braces
for pages filed before the field existed.

The RANK side of the chain matters too: `index.rank.chain_base` groups the parts, and
`index.corpus.load_pages` propagates the primary's `superseded_by` onto every sibling at BUILD
time (see [navigation.md](./navigation.md)), because only part 1 ever carries the field.

## Field notes worth knowing

- `content_hash` is **server-stamped, never drafted**: `page.stamp_source_fields` writes it on a
  source page, and `gates.gate_frontmatter`'s
  `FORBIDDEN_PAGE_KEYS` (`owner`, `id`, `content_hash`, `tier`, `extracted_at`) refuses it on every
  other page. A page declaring its own `content_hash:` is forged.
- The entity pipeline-status field (`won`/`lost`/`active`… from the folder name) is
  `entity_status`, not `status`: the contract reserves `status` for **page maturity**, which the
  index ranks on; the shared name collided. The
  maturity axis is `seed` · `developing` · `mature` · `evergreen` (`VALID_STATUS` in the knowledge
  repo's contract linter), and `evergreen` is the value `index.rank` boosts. Every fast-lane page is
  filed as `developing`, forced by `librarian.page.FILED_STATUS` whatever the material claimed about
  itself — a capture asserting `status: canonical` is stamped back to `developing`, and
  `canonical` is not a value on the axis at all: it is a name the fast lane recognises only in order
  to refuse it (`declare-canonical` is one of the three injection categories the librarian records).
- `source_kind` honors the contract enum — `google-drive`, `slack`, `meeting`, `github`, `upload`,
  `distilled`. The archive emits three of them — `slack` for the Slack door, `meeting` for a
  transcript, `upload` for a document and for every other capture; the rest belong to pages already
  in the corpus.
