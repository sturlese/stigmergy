# `stigmergy_lint.py` here is a FROZEN COPY

`stigmergy_lint.py` beside this file is a byte-for-byte copy of the contract linter from the
**knowledge repo** (`stigmergy`), not a second implementation of it.

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/tools/stigmergy_lint.py` |
| **Copied at commit** | `febe7b951d6e2e4e8d2eeb3e57474b041151c1c0` |
| **Drift guard** | `tests/librarian/test_frozen_linter.py` |

M6c's linter changes are **committed and pushed** in the knowledge repo (2026-07-27, the sha
above): the `registry` consistency rule, the quoted-frontmatter-key fix that the `notes-forward`
entry had owned since M6b, and — from the M6c findings loop — the two generator-refusal cases that
rule used to pass over (an entity page with no title, and two pages whose titles slug to one
registry id). Both make `stigmergy-entities regenerate` refuse to run, so a linter silent about them
left CI green on a repo where the fix command cannot execute.

M8a (2026-07-28) adds the `entity:` contract: bare-string-or-list acceptance, resolution against
`ops/entity-registry.json` (an unresolvable value, or a display name/alias where an id belongs, is
now an error naming the fix), the duplicate-match-key mirror of
`entities.generator._duplicate_match_keys`, the thin-page exemption for `type: entity`, and the
removal of the `submitted_by`-keyed zone-ownership rule (superseded by the knowledge repo's own
CI author check, which reads commit history this stateless scan cannot).

M8a findings cycle 1 (2026-07-28, the sha above) fixed two defects the auditor found in the M8a
work above: `parse_frontmatter` now folds a scalar the same way PyYAML (the entity generator's own
parser) does — matching its actual comment rule (`#` needs whitespace only on its LEFT, not both
sides) and quote-aware inline-list splitting (`["Borealis Dynamics, S.L."]` no longer breaks into
two elements) — and refuses outright, rather than guessing, on a value opening with a YAML
construct this parser cannot represent (`>`, `|`, `&`, `*`). Separately, the `submitted_by`-keyed
removal above over-read the spec: six verifier-output fields (`unverified_numbers`,
`unanchored_numbers`, `extraction_method`, `blob`, `source_uri`, `extracted_at`) are a SHAPE
question, not an authorship one, and are restored as `INGESTED_ONLY_FIELDS` — checked on any
`wiki/**` page, keyed on nothing but presence, no `submitted_by` exemption reintroduced.

M8a findings cycle 2 (2026-07-28) fixed six more constructs where `parse_frontmatter` and PyYAML
disagreed, both directions: a comment after a quoted scalar, a tab after the colon, YAML 1.1
implicit bool/null/sexagesimal words read unquoted, an explicit tag (`!!str`), an escaped quote
inside a quoted scalar, and a `#` (or an escaped quote) inside a quoted inline-list element — the
last two were false positives that turned a `stigmergy-entities regenerate`-accepted page red with
no way to clear it. `datasets/`/`meta/` also join the `INGESTED_ONLY_FIELDS` zone check (decided
and recorded in the linter's own comment).

DB-27 (2026-07-29, the sha above) adds `view` to `VALID_TYPES` (`COMPANY_TYPES`): M9b's
`views/` pages carry `type: view` — DESIGN's own T6 artifact, not pipeline output awaiting
SI-02's mapping — so every real view used to carry a permanent, misleading "pipeline dialect;
mapped at M1b — SI-02" warning. Because a valid type no longer trips the generic "invalid type"
error, `ZONE_TYPES` gained two entries it was missing (`wiki/notes`, `wiki/postmortems`)
so a hand-authored `wiki/**` page still cannot mint `type: view` and pass — the zone check
is now the only thing standing between "M9b's own artifact" and "anyone can claim this type".

> **Why committing was the load-bearing half.** ST3 (M6c §4.6) made `gate_contract` materialize the
> linter from the **base commit**, so the worker executes what `origin/main` carries and never what
> sits in an operator's working tree. Until this was pushed, the new rules had no effect on a live
> librarian run at all. Whoever changes this linter again inherits the same two-step: commit in
> `stigmergy`, then record the sha here — they are one action, not two.

## Why a copy

`gate_contract` runs the knowledge repo's own linter, so the librarian is held to exactly the
standard a human PR is. The test suite therefore needs a real linter — but it must not depend on
a particular checkout existing on the machine running the tests, or CI and a fresh clone would
both fail for a reason that has nothing to do with the code.

## Why the drift matters more than it looks

`gate_contract` is the **only** contract check the librarian's own commits ever receive. It commits
direct to `main` (D5 fast lane; branch protection is unavailable on the free private repo, SI-01),
so there is no PR and no CI run between a filed page and the graph. If this copy drifts behind the
real linter, the librarian is being held to a standard the repo has moved on from — and nothing
would say so.

## Resyncing

`test_frozen_linter.py` fails with the command when it can see the real checkout. It is:

```sh
cp "$STIGMERGY_REPO/.claude/tools/stigmergy_lint.py" \
   tests/librarian/fixtures/repo/.claude/tools/stigmergy_lint.py
```

Then update **Copied at commit** above with:

```sh
git -C "$STIGMERGY_REPO" log -1 --format=%H -- .claude/tools/stigmergy_lint.py
```

Same declared-duplication posture the repo already applies to
`pipeline.ingest.trust.verify` / `answer.numbers`: mirrored on purpose, written down, and checked.
