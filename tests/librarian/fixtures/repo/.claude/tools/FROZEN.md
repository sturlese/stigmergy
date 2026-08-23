# `stigmergy_lint.py` here is a FROZEN COPY

`stigmergy_lint.py` beside this file is a byte-for-byte copy of the contract linter from the
**knowledge repo** (`stigmergy`), not a second implementation of it.

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/tools/stigmergy_lint.py` |
| **Copied at commit** | `153849293b5876fb39868306f08a57c7e0ff4ade` |
| **Drift guard** | `tests/librarian/test_frozen_linter.py` |


What the copy carries is whatever the knowledge repo's linter carries at that commit; the rule
set is that file's own business and its own tests (`test_stigmergy_lint.py` beside it) describe
it. The rules the librarian's gates lean on hardest: the page contract (frontmatter, zones, size,
dead links, alias collisions), the `entity:` anchor resolved against `ops/entity-registry.json`,
the registry-as-derived-view agreement between every entity page and the registry (name, type,
aliases, and — since File First, Govern After — the identity lifecycle: `approved_by` empty is a
proposal, `proposed_aliases` are spellings waiting on a steward, and the registry must say the
same), and the two generator refusals (an entity page with no title, two titles that slug to one
id) that would stop `stigmergy-entities regenerate` from running at all.

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
