# The page contract: `entity:`, the anchor field

The narrative doc for `entity:` — the field that carries a page's *aboutness* — and, at the end, for
the two fields that carry an **entity page's own lifecycle** (`approved_by:`, `proposed_aliases:`).
It sits beside [`brain-page-contract.md`](brain-page-contract.md),
which documents the wider `sources/` frontmatter dialect (`entity: initech # resolved from
the folder path`); this file is the one place the FAST-LANE anchor rule itself is written down.
Design records: [ADR 008](../decisions/008-entity-registry.md) (the
registry itself), [ADR 016](../decisions/016-human-loop-and-entity-governance.md) (the routing an
unresolved anchor used to take).

## The ruling: aboutness, never mention

**`entity:` declares the entities a page is ABOUT — never that an entity was merely mentioned,
never that someone participated, never that someone wrote the material.** A person appearing in a
thread, a name in a sentence, is not aboutness; `submitted_by:` already carries submission
unforgeably (it is the server's own field, never a capture's to declare) and `related:` carries
see-also. The analogy that settles most cases: a conversation belongs to an epic — not to
everyone quoted in it.

This is deliberate, not an oversight: at pilot scale (tens of pages) a looser rule costs nothing
visible, but the ambient-noise failure re-enters through the anchor field itself the moment it
does not hold — at hundreds of pages, every platform note that happens to *link* `[[Stigmergy]]`
would anchor to it, and the `entity` filter would match half the corpus.

## Shape

- **Value**: a list of **entity registry ids** — the slugs exactly as they appear in
  `ops/entity-registry.json` (`stigmergy`, `borealis-dynamics`), never a display name, never an
  alias. A **bare string is also accepted** and read as a one-element list — an older writer's
  dialect (`entity: initech`); nothing mints one now, but pages already carrying it stay valid.
- **Cardinality**: plural, deliberately bounded by prose rather than by a rule. **1–3 is the
  expected shape** — a page naming more than three entities is a page about a theme, not an
  anchor, and the contract says so without enforcing a numeric ceiling (a hard numeric ceiling is
  a proxy with false positives, and this codebase does not buy those).
- **`entity: []` — its meaning is fixed by ZONE and by page TYPE**, following the precedent that a
  machine zone validates against a different rule than an authored one:
  - On an ordinary `wiki/**` page (the fast lane, where `gate_anchoring` runs on every filed page)
    an empty list is a **checked, explicit company-wide declaration** — the librarian's agent
    judged the material belongs to no single entity and wrote a reason, which lives in the
    submission report, never on the page (see below).
  - On a **provenance page** — `page.PROVENANCE_PAGE_TYPES`, today `source` and `meeting` — it is
    neither a claim nor an omission. A provenance page is a RECORD OF AN EVENT, never a knowledge
    destination, so it HAS no aboutness to declare: `[]` there means "about **nothing**",
    not "about everything".
  - In `sources/` and `views/` generally (no anchoring gate runs there) an empty list means **the
    extractor found none** — an absence of evidence, not a checked judgment.

  Same value, three readings, one rule each, written down here so nobody has to infer it from
  which folder a page happens to sit in.

  **The distinction is load-bearing for views**: views read `entity:` to build member sets, and a
  meeting page reads as "about everything" under the company-wide interpretation and "a member of
  nothing" under the provenance one. A provenance page is a member of nothing BY CONTRACT —
  `page.is_provenance_type` answers the question, and answers `False` for an unknown type, the
  conservative direction: one extra human question rather than a silently skipped governance
  check.

- **The fast lane can only CREATE three of the seven page types.** `librarian/page.py::PAGE_TYPES`
  is the single vocabulary table, and a type carries a `folder` only when the fast lane may mint
  one: `note` (`wiki/notes`), `decision` (`wiki/decisions`) and `concept` (`wiki/concepts`). The
  other four — `entity`, `source`, `meeting`, `view` — may be read, linked and cross-referenced,
  but a capture that wants one is refused with the reason in the submitter's own language,
  rather than being quietly downgraded to `note`: a per-type exemption is exactly how ambient
  ownerless content accumulates. Seven rows, one per WRITER — see
  [`librarian.md`](./librarian.md#three-types-the-fast-lane-may-create-seven-it-knows). An `entity`
  page is the one a capture's own commit can still contain, and never as a DRAFT: the agent declares
  the identity, code writes the page — unconfirmed, or confirmed by the steward who registered that
  entity through the capture — and the lifecycle fields below say which.
- **An absent `entity` key at all** is a pre-contract page — filed, or extracted, before this
  field existed. Detectable (a linter pass can simply ask whether the key exists), and a one-time
  backfill cleared them from the knowledge repo. That is a claim about the CONTENT repo, which lives
  outside this codebase: `pages_index.entity` is `NOT NULL DEFAULT '{}'`, so the index cannot tell
  an absent key from a checked empty one, and only the page contract needs to.
- **`related:` carries NO anchoring semantics whatsoever.** It stays what it always was — see-also
  and graph decoration. There is therefore no mirrored pair between `entity:` and `related:` to
  drift, and no parity test is owed between them: the two fields answer different questions and
  always did.

## Who writes it, and where

**The fast lane**: `entity` is a **server-owned field** (`librarian/page.py::SERVER_OWNED_KEYS`),
stamped by `stamp_server_fields` from the anchoring outcome `gate_anchoring` already verified — the
same declared-and-protected treatment `submitted_by`/`acl` already get (`verification` is in the
same key set too, but it is STRIPPED there, never stamped — nothing computes a value for
it). A capture's own drafted
`entity: [...]` is deleted and rewritten; the declared value never survives onto the committed
page. `gate_anchoring` requires EVERY value the agent declared to resolve through
`ops/entity-registry.json`, read at the **base commit** the librarian is filing against (never an
uncommitted local edit — `librarian/base_inputs.py`) — an id, a display name or an alias may be
declared, and the page is stamped with the **resolved canonical id**, whichever of the three the
agent actually typed.

**The strip compares on a normalized key, and that is not decoration.** A bare exact-match strip
is bypassed by `"entity": [...]` (quoted — and `yaml.safe_load`'s last-key-wins makes the survivor
the value that counts), by `Entity:`, and by `еntity:` (Cyrillic е, U+0435).
`page.normalize_key` is what both the strip and
`gates.FORBIDDEN_PAGE_KEYS`' presence check compare on now: a small explicit homoglyph fold, then
NFKC, then casefold — NFKC alone does **not** cover a cross-script look-alike, because Cyrillic and
Latin are unrelated scripts to Unicode rather than a compatibility pair.

**Three mechanisms enforce this, not one, and they answer different questions.** Knowing which is
which matters, because the obvious reading of any one of them is wrong:

- the **strip** removes the forged line before the page is committed, comparing on the normalized
  key above;
- the **duplicate backstop** (`gates.gate_frontmatter` reading `page.duplicate_top_level_keys`)
  vetoes a filed page that declares a server-owned key more than once — but by a REAL PARSER's own
  notion of key identity (`yaml.compose`, which builds the tree without constructing a dict, so a
  duplicate is never silently overwritten before it can be seen). That deliberately does **not**
  cover `Entity:` or `еntity:`: those are genuinely different keys to PyYAML too, so they are not
  duplicates, and the strip above is what handles them;
- the **key whitelist** is what actually closes the confusables class. Enumerating spellings never
  converges — a Greek omicron, a Turkish dotless ı, small-caps letterforms, a zero-width joiner
  inside the word, a combining accent instead of a precomposed character all survive the strip and
  produced zero findings before it existed — so the control is inverted: every top-level frontmatter
  key must match `^[a-z_][a-z0-9_.-]*$` or the page is refused outright, whatever it resembles. A
  leading BOM, YAML explicit-key syntax (`? key`) and a top-level merge key (`<<:`) are refused
  outright on the same argument: nothing this repo's page dialect legitimately needs looks like any
  of them.

A gate this contract deliberately does **not** claim: for a company-wide outcome, the only checked
property is that a written reason EXISTS, never that it is true of the material. The librarian
judges and code vetoes — verifying that a capture genuinely belongs to no entity is
the judgment call code cannot make. So a page can reach `entity: []` behind a reason that reads
well and is wrong, and the submitter's own report ("company-wide scope (`<reason>`)") is the only
surface where a human ever sees it. A residual to know about, not a defect to fix here.

A **company-wide** anchoring outcome still needs a written reason — but the reason justifies a
*filing decision*, so it stays in the submission report; the page's `entity: []` states an
*identity* (or the absence of one) and carries no prose.

**Steward edits**: `entity` is **not** in the trust-field group
(`capture.schema.ATTRIBUTION_FIELDS`) — a steward hand-editing an existing page's own aboutness
is legitimate governance, not a forged trust claim.

## `approved_by:` and `proposed_aliases:` — an entity page's lifecycle

These two live on `type: entity` pages **only**. They exist because an identity can now be created
by the librarian while it files a capture, which means a page can be in the corpus — resolving,
anchoring, searchable — before any person has agreed it should be. The page is where that fact is
recorded; `ops/entity-registry.json` is a derived view of it, and the review inbox a derived view of
the registry. `entities.generator.APPROVED_BY_KEY` / `PROPOSED_ALIASES_KEY` are the one spelling of
each name.

### `approved_by:` — who confirmed this identity

A **scalar string**, with three readings and no fourth:

| Value | Reading | Written by |
|---|---|---|
| `approved_by: ""` | **PROPOSED.** The librarian created this page from a capture that was about the thing, and no steward has confirmed the identity. The empty string IS the mark — silence would be indistinguishable from an old page | `librarian.identity.write_proposals`, and only ever this value |
| `approved_by: ana@example.com` | **CONFIRMED**, by that person | a steward's decision (`entities.decide.approve_entity` / `merge_entity`), or — at BIRTH — `librarian.identity.write_proposals` naming the steward who registered this entity through the capture the page was written from ([ADR 042](../decisions/042-an-entity-is-born-written.md)) |
| the key is **absent** | confirmed before the field existed. Pages written under the older contract are never migrated, and read as confirmed | nothing — it is what an old page already says |

Absent-means-confirmed is deliberate and is the only reading that is safe: a repository whose entity
pages predate the field must not wake up one morning with every identity in the review inbox.

The value is a **name, not a permission**: nothing downstream authorizes on it. What it answers is
"who stands behind this identity", the same question `git log`'s author line and the commit's
`Decided-by:` trailer answer — three records of one fact, because a database and a repository can
be separated.

Two vetoes hold the empty-string spelling. `gates.gate_identity` refuses a created entity page whose
`approved_by` is absent or non-empty (`approved-on-arrival`) — an identity that arrived confirmed is
an identity nobody confirmed — and the knowledge repo's own contract linter raises a `lifecycle`
error for a non-string value. The registration road does not weaken that: for a page born confirmed
the gate is told, per path, WHICH steward may appear there, and any other name is
`not-confirmed-by-its-steward`. A confirmed birth is never a page the gate merely let through.

### `proposed_aliases:` — spellings waiting on a steward

A **list of strings**, on a REGISTERED entity's page: the spellings the librarian appended because a
capture used them for this entity, each waiting on Approve (it moves into `aliases:`) or Decline (it
is dropped). They resolve while they wait, exactly as `aliases:` does — that is the point, since the
capture that proposed one is already anchored — and the registry carries them under the same name.

The list is empty or absent on an entity nothing has proposed a spelling for. A spelling that is
already the entity's own title or one of its `aliases:` is a **linter error**, not a duplicate to
tolerate: a spelling the registry already resolves needs no proposal, and leaving it there would put
a decision in the inbox that changes nothing whichever way it goes.

### Both are checked against the derived view

The contract linter compares each page's lifecycle to `ops/entity-registry.json` and errors when
they disagree — a page proposed while the registry registers it confirmed, or a `proposed_aliases`
list the registry does not carry — naming `stigmergy-entities regenerate` as the fix. The reason is
not tidiness: the review inbox is built from the REGISTRY, so a drifted registry shows a steward
proposals that do not exist and hides ones that do.

### The body an entity page is born with

An entity page's frontmatter is not the whole contract. The BODY is written at birth too, by the
only writer there is (`librarian.identity` through `entities.birth.render_page`), and three rules
hold it:

- **It is never empty.** `render_page` REFUSES a page whose body has no **What / Who** paragraph —
  *"would be born with nothing said about it"* — and the entity is not created at all. The rule
  exists because a page that says nothing still resolves, still ranks and still answers nothing:
  twelve of the first brain's nineteen entity pages were born carrying the template's own
  `<One clear paragraph: …>` stubs, which is knowledge-shaped noise, not knowledge.
- **A section with nothing in it is not written.** `Facts` and `Connections` may legitimately be
  empty — a name met once establishes little — and when they are, the heading goes with the stub.
  A page carrying `- <fact…>` reads as a page with a fact, and something has to find it later to
  say it has none.
- **The template's comments never reach a page.** `ops/templates/entity.md` carries HTML comments
  for whoever edits the TEMPLATE; they are stripped from every page rendered from it. The template
  keeps its notes; the page a person reads carries none.

The body is not frozen at birth either. A later filing may APPEND lines to a registered entity's
`## Facts` and `## Connections` (creating the section when the page has none) and move `updated:`
to that day — appends only, byte-proven, and never a line the page already carries. See
[librarian.md](./librarian.md#the-spine-accretes-what-a-filing-adds-to-an-entity-it-already-knows).

## How it is read

`index/corpus.entity_list` normalizes both dialects into `pages_index.entity`, a Postgres `text[]`,
and it does so **fail-CLOSED**, not fail-open: it strips every string element, rejects bools
outright (a YAML 1.1 truthy word is never a plausible id — an unguarded parse turns `entity: no`
into `["False"]`, a real scoring change for any query containing the token "false"), drops anything that is not a
string/int/float instead of stringifying it, and folds `""` to `[]`, never `[""]`. Each of those
was a real declaration nobody wrote reaching the ranker with a label a human never typed.

`search_brain`'s `entity` filter is membership (`%s = ANY(entity)`); its **public contract is
unchanged** — a caller still passes one id.

**The entity boost is TOLD, not inferred.** A boost keyed on query-TOKEN matches is structurally
dead for every multi-word entity — an id like `northwind-group` can never equal one token of
"Northwind Capital" — so `BrainService._search` resolves the query
against the registry and passes the resolved id DOWN as `entity_hint`; `rank.contract_factors`
fires the boost on **membership of that hint** in the page's list, and the factor label names the
hint (`entity:borealis-dynamics`), never the whole list. No hint means no entity factor at all:
resolution belongs to the layer that owns the registry and the identity, and ranking only applies
what it is told. The filter side is
[hybrid-index.md → Queryable filters vs stored columns](./hybrid-index.md#queryable-filters-vs-stored-columns).

## Worked example

```yaml
---
type: decision
title: Borealis Dynamics Renewal
entity: ["borealis-dynamics"]
related: ["[[Northwind Group]]"]     # see-also; carries no anchoring meaning
submitted_by: ana@example.com
status: developing
---
```

The submission report that filed this page names the SAME id, in the SAME vocabulary a human
reads it in: `anchored to Borealis Dynamics (\`borealis-dynamics\`)` — the display name for
legibility, the id backtick-quoted because it is the literal value the page itself carries. A
submitter reading the report and a reader opening the page in git see the same claim, spelled the
same way, without a database in the loop.
