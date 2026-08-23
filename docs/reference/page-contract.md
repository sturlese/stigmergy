# The page contract: `entity:`, the anchor field

The narrative doc for `entity:` — the field that carries a page's *aboutness* — and, at the end, for
the one field that carries an **entity page's own lifecycle** (`approved_by:`).
It sits beside [`brain-page-contract.md`](brain-page-contract.md),
which documents the wider `sources/` frontmatter dialect (`entity: initech # resolved from
the folder path`); this file is the one place the FAST-LANE anchor rule itself is written down.
The capture is the approval, so that lifecycle has exactly one state.

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
  the identity, code writes the page, and it is born CONFIRMED by the person whose capture it was —
  the lifecycle field below is where their name lands.
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

**Hand edits**: `entity` is **not** in the trust-field group
(`capture.schema.ATTRIBUTION_FIELDS`) — a person editing an existing page's own aboutness in the
knowledge repo is doing ordinary editorial work, not making a forged trust claim.

## `approved_by:` — an entity page's lifecycle

This field lives on `type: entity` pages **only**. It exists because an identity is created by the
librarian while it files a capture, which means the page enters the corpus in the same commit as
the note that is about it — and the field records who stands behind it. The capture IS the
approval: the person who captured is
the person named here, so there is no waiting state, no second field, and nothing to confirm
afterwards. `ops/entity-registry.json` is a derived view of the page;
`entities.generator.APPROVED_BY_KEY` is the one spelling of the name.

### `approved_by:` — who introduced this identity

A **scalar string**, with two readings and no third:

| Value | Reading | Written by |
|---|---|---|
| `approved_by: ana@example.com` | **introduced by that person** — the `submitted_by` of the capture the page was written from: the token's email over HTTP, the `--identity` over stdio, the reacting user's resolved email from Slack, the console's actor for a registration | `librarian.identity.write_births`, in the commit that files the capture |
| the key is **absent** | a page written before the field existed. Pages under the older contract are never migrated, and read the same way | nothing — it is what an old page already says |

The empty string is not a third reading. The knowledge repo's contract linter raises a `lifecycle`
error for `approved_by: ""`, as it does for a non-string value: an identity nobody is named for is
an identity nobody stands behind. The fix it states is to name the person, or to drop the field on
a page that predates it.

The value is a **name, not a permission**: nothing downstream authorizes on it. What it answers is
"who stands behind this identity", the same question `git log`'s author line and the filing
commit's `Submitted-by:` trailer answer — three records of one fact, because a database and a
repository can be separated.

`gates.gate_identity` is the veto that holds it. A created entity page must be one this run
introduced, and its `approved_by` must name EXACTLY the submitter the birth code was told
(`not-confirmed-by-its-submitter`); a page that named nobody, or somebody else, would be an
identity nobody stands behind. The gate is told that name by code, per path, and never from the
model's account.

### A spelling is an alias, not a state

There is no `proposed_aliases:` field. A spelling the material uses for a REGISTERED entity is
appended to that entity's own `aliases:` list in the commit that files the capture, byte-proven
like every other planned edit, and it resolves from that moment. A page still carrying
`proposed_aliases` is a `lifecycle` error in the knowledge repo's linter, with the fix being to
move the spellings into `aliases`.

### It is checked against the derived view

`ops/entity-registry.json` is derived from the entity pages, so the two can only disagree if
somebody hand-edited one of them. Both sides check that. The knowledge repo's own contract linter
compares each page's name, type and aliases against the registry and reports a disagreement as a
`warn` — the librarian worker regenerates the file from the pages on its next pass over the
identity zone, and neither side is edited by hand. The platform's own `entities.generator.check`
compares the same view plus who introduced each identity, and the librarian runs it at the base
commit BEFORE it introduces anything: an identity born into a registry that already disagrees with
its pages would be regenerated into a file the commit was not meant to rewrite, so the capture is
refused with that sentence instead.

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
