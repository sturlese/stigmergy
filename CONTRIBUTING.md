# Contributing

Thanks for looking. This file is the whole contract: what to run, what the code will refuse, and
the handful of rules that are enforced by tests rather than by review.

## Getting a working checkout

```bash
make venv        # bootstrap the virtualenv (re-runs when pyproject.toml changes)
make db-up       # postgres+pgvector + minio + a bare git remote, all on loopback
make test        # the whole suite (coverage gate 75%)
make lint        # ruff over src/ tests/ evals/ scripts/
```

You need Python 3.12+, Docker, and `gitleaks` on `PATH` (`brew install gitleaks`) — the secrets gate
shells out to the real scanner, so anything exercising the librarian worker refuses to start without
it. You do **not** need an API key: `make test` is keyless by construction — an autouse fixture
forces the fake model backend repo-wide, even if your `.env` has real keys in it. A test that
silently reached a real model would be a test nobody could trust.

`make help` lists every target. The ones that cost money or touch a real deployment say so.

To watch the real thing work without a key or a knowledge repo, run the three narrated walks the
README's quick start lists (`scripts/walk_*.py`) — real Postgres, real git, real gates, the offline
double in the agent's place.

Copy `.env.example` to `.env` when you want to run something against a real model or a real
knowledge repository, and **export it** (`set -a && . ./.env && set +a`) — `make` reads the file for
its own targets, but nothing under `src/` does, so a binary you invoke directly sees only what is in
the environment. `.env` is gitignored and never enters a Docker build context.

What that knowledge repository has to contain before anything will index or file is
[`docs/reference/knowledge-repo.md`](./docs/reference/knowledge-repo.md) — read it before you go
looking for why an empty one does nothing.

## What this repo is, in one paragraph

The platform stores no knowledge. It reads and writes a **separate git repository** — the knowledge
repo — whose page format is described in
[`docs/reference/page-contract.md`](./docs/reference/page-contract.md). Captures arrive on a durable
queue, a single writer (the librarian) files them as pages through an agent whose output is judged
by code, and a derived Postgres index serves reads through one MCP server. Start with
[`README.md`](./README.md), then [`docs/reference/`](./docs/reference) for what each subsystem does
and [`docs/decisions/`](./docs/decisions) for why it is built that way.

## The rules that are enforced, not reviewed

`tests/test_architecture.py` parses every module's imports and fails with the offending file and
line. A layering that lives only in a README is decoration; these are tests:

| Rule | Why it exists |
|---|---|
| `stigmergy.kernel` and `stigmergy.text` import nothing from this project | they are the bottom of the stack, so everything above can depend on them without inheriting anyone's git stack |
| the librarian never imports the server | one direction only; every cross-package reach is a named, exercised exception |
| every reader of `pages_index` names an ACL predicate, or is on a declared exception list | `acl.visible()` is the ONE place read access is decided |
| the UNTRUSTED-DATA fence is built only in `stigmergy.text` | six separate copies once existed and one took the weak variant, so hostile content could close the fence early |
| a parked capture's unresolved names have one definition, one writer and one reader | the keys are a JSONB wire format with no schema behind it, so nothing type-checks a module that writes the wrong one; two lanes each deciding for themselves is how a capture naming two entities lost one on its way to a human |
| every declared exception must still be used | an exception list nobody prunes becomes a permission slip |
| the README's countable claims match the code | `tests/test_readme_claims.py` — four of them had already drifted |
| the reference docs name no command, variable or count the code does not have | `tests/test_docs_claims.py` — 60k words nobody can afford to re-read, so the checkable part is checked. `docs/decisions/` is exempt: an ADR records a decision, not the present |

If you need to cross a seam, add the exception **by name with its reason** rather than widening a
rule. The pruning tests will delete it for you when it stops being used.

## Testing doctrine

Five rules, and they are the reason the suite is worth its size:

- **A benign twin for every defense.** A test that only proves a gate fires measures its sensitivity
  and never its specificity — and every gate here can bounce someone's real work. Prove it refuses
  the bad thing *and* accepts the good one.
- **Reproduce before you fix.** A bug gets a failing test that demonstrates it *before* production
  code is touched, and the test's own comment says what the old behaviour was. Several of this
  repo's worst defects were invisible to 3,000 green tests and visible in one deliberate mutation.
- **A message containing a command is an executable promise.** If a refusal tells a human to run
  something, a test runs it. A refusal that names a dead flag or a renamed variable costs the
  person reading it a debugging session, at the exact moment they are already stuck.
- **Never fake what you are claiming to prove.** Real git, real Postgres, real gates. A faked git
  proves nothing about a property that is about git.
- **A check that stops running must be impossible to miss.** Skipped and retired dimensions are
  printed, counted and asserted — never quietly dropped. A permanently-green test is worse than no
  test, because it reads as coverage.

## Writing comments

This codebase comments heavily and deliberately, and there is a specific bar: **a comment states a
constraint the code cannot show.** The failure that motivated a design, the attack a defense exists
for, the "if you change this you must also change that". Not what the next line does, not where the
change came from, not why the author thinks it is correct.

Two things to avoid, both learned the hard way:

- **Do not write a comment the code will outlive.** If it states a count, a signature or a module
  name, it will drift. Prefer a sentence that stays true, or pin the fact in a test.
- **Do not index prose to anything outside this repository** — a ticket, a review, a document that
  can be archived or made private. Every explanation this repo depends on lives in this repo.

## Pull requests

- Branch from `main`. Small and focused beats large and thorough.
- `make test` and `make lint` green. CI runs the same thing plus four Docker end-to-end proofs and
  a full-history secret scan.
- Behaviour changes want a decision record. If you are changing *what the system does* rather than
  how it does it, add or amend an ADR in [`docs/decisions/`](./docs/decisions) in the same PR.
- Documentation is **rewritten, not appended to.** If your change makes a sentence in `README.md`,
  a `docs/reference/` page or a package `index.md` false, correct it in the same commit. A stale
  README is a false premise for everyone who reads it next.

## Writing a decision record

Five rules, which `docs/README.md` records as history and are easier to follow stated as rules:

- **Take the next free number, and never renumber anything.** The sequence has gaps and keeps them.
  A reference to "ADR 015" in a commit message or a code comment has to still resolve years later,
  which is worth more than a tidy run of integers.
- **Open with a `**Status:**` line carrying the date**, and update it in place when a later record
  changes the decision — including a forward pointer to the record that did. An ADR whose reader
  cannot tell it was overturned is worse than no ADR.
- **Amend rather than rewrite.** An ADR is dated evidence of a decision, not a description of the
  present, which is why `tests/test_docs_claims.py` deliberately exempts `docs/decisions/` from the
  checks that hold `docs/reference/` to what the code has today. A record naming a module that was
  since deleted is doing its job, as long as it says so.
- **When a subject is removed from the system whole, delete its record rather than superseding it**
  — and name the deletion in `docs/README.md`'s numbering note, so the gap reads as a decision
  instead of an accident. The exception is a design worth rebuilding from: keep it, and mark it
  historical in its own opening lines, the way ADR 023 does.
- **Add the row to `docs/README.md`'s table in the same commit.** A test fails if the index and the
  directory disagree, in either direction.

## Security

Do not open a public issue for a vulnerability — see [`SECURITY.md`](./SECURITY.md), which also
describes the threat model this system is actually built against.

## License

By contributing you agree that your contributions are licensed under
[Apache-2.0](./LICENSE), the same terms as the project.
