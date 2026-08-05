# The page contract: `entity:`, the anchor field

The narrative doc for `entity:` — the field that carries a page's *aboutness*. It sits beside
[`brain-page-contract.md`](brain-page-contract.md),
which documents the wider `sources/` frontmatter dialect (`entity: initech # resolved from
the folder path`); this file is the one place the FAST-LANE anchor rule itself is written down.
Design records: [ADR 008](../decisions/008-entity-registry.md) (the
registry itself), [ADR 016](../decisions/016-human-loop-and-entity-governance.md) (the human-loop
routing an unresolved anchor takes).

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

  **The provenance distinction had to be written down because it had a consequence**, found by a
  real filing walk rather than by a test — no meeting page had ever reached the situation before.
  Any review step that asks a human to CONFIRM a company-wide claim before acting on a page
  carrying `entity: []` would, under the single reading, put such a claim in front of a person and
  ask them to sign it for a page that never made one. Asking somebody to vouch for an assertion
  nobody wrote is precisely the failure such a confirmation exists to prevent, arrived at from the
  other side. (There is no maturity-promotion lane in this codebase for that step to live on today
  — maturity is a field, not a lane — so the live consequence is the second, quieter one.) That
  one: views read `entity:` to build member sets, where a meeting page reads as "about everything"
  under the company-wide interpretation and "a member of nothing" under the provenance one. They
  agreed by luck of implementation; now a provenance page is a member of nothing BY CONTRACT,
  because `page.is_provenance_type` answers the question and answers `False` for an unknown type —
  the conservative direction, which costs one extra human question rather than silently skipping a
  governance check.

- **The fast lane can only CREATE three of the seven page types.** `librarian/page.py::PAGE_TYPES`
  is the single vocabulary table, and a type carries a `folder` only when the fast lane may mint
  one: `note` (`wiki/notes`), `decision` (`wiki/decisions`) and `concept` (`wiki/concepts`). The
  other four — `entity`, `source`, `meeting`, `view` — may be read, linked and cross-referenced,
  but a capture that wants one lands in `triage` with the reason in the submitter's own language,
  rather than being quietly downgraded to `note`: a per-type exemption is exactly how ambient
  ownerless content accumulates. Seven rows, one per WRITER — see
  [`librarian.md`](./librarian.md#three-types-the-fast-lane-may-create-seven-it-knows).
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

**The strip compares on a normalized key, and that is not decoration.** `_strip_keys` matched bare,
exact, lower-case keys once, so `"entity": [...]` (quoted) survived beside the
server's own line — and `yaml.safe_load`'s last-key-wins made the survivor the value that counted.
The sibling defect is the same shape: `Entity:` and `еntity:` (Cyrillic е, U+0435) are both bypasses
of an `in keys` test. `page.normalize_key` is what both the strip and
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

## How it is read

`index/corpus.entity_list` normalizes both dialects into `pages_index.entity`, a Postgres `text[]`,
and it does so **fail-CLOSED**, not fail-open: it strips every string element, rejects bools
outright (a YAML 1.1 truthy word is never a plausible id — `entity: no` used to become `["False"]`,
a real scoring change for any query containing the token "false"), drops anything that is not a
string/int/float instead of stringifying it, and folds `""` to `[]`, never `[""]`. Each of those
was a real declaration nobody wrote reaching the ranker with a label a human never typed.

`search_brain`'s `entity` filter is membership (`%s = ANY(entity)`); its **public contract is
unchanged** — a caller still passes one id.

**The entity boost is TOLD, not inferred.** It used to fire when any
element of the list matched a query **token**. That form was structurally dead for every
multi-word entity: an id like `northwind-group` can never equal one token of "Northwind
Capital", so the boost had silently narrowed itself to single-word ids, and it took
someone eyeballing a search result to notice. Today `BrainService._search` resolves the query
against the registry and passes the resolved id DOWN as `entity_hint`; `rank.contract_factors`
fires the boost on **membership of that hint** in the page's list, and the factor label names the
hint (`entity:borealis-dynamics`), never the whole list. No hint means no entity factor at all:
resolution belongs to the layer that owns the registry and the identity, and ranking only applies
what it is told. The filter side is
[index.md → Queryable filters vs stored columns](./index.md#queryable-filters-vs-stored-columns).

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
