# stigmergy — working notes for an AI agent

This repository holds the **code**: the durable capture queue and the doors that fill it, the
librarian service that drains it, the hybrid index builder, the single MCP server (the only API)
and the answering half behind its `ask`, the entity-birth rules the librarian writes through, the
Slack transport, the meeting distiller, the view layer, the gardener, the weekly digest, the admin
console, `docker-compose` for the local test stack, and the evals.

**This repo never stores pages.** Knowledge content lives in a separate git repository — the
knowledge repo — that you point at with `STIGMERGY_REPO`. Its page format is
[`docs/reference/page-contract.md`](./docs/reference/page-contract.md).

Human-facing setup, workflow and PR expectations are in
[`CONTRIBUTING.md`](./CONTRIBUTING.md). What follows is the part that is easy to get wrong.

## Orientation

Read [`README.md`](./README.md) first for the annotated tree, then the `index.md` code map inside
whichever package you are about to touch — each one lists what its modules are for, what to reuse
and what to avoid. [`docs/reference/`](./docs/reference) is what each subsystem does;
[`docs/decisions/`](./docs/decisions) is why it is built that way.

**Two skills live in `.claude/skills/` here and are worth loading before you start.**
`land-a-change` picks the delivery pipeline a change belongs in, says when the auditor stops being
optional, and names which changes also have to land in the knowledge repo. `validate-deployment`
walks a deployed stack through MCP, Slack and the admin console with evidence at each step. Do not
confuse this `.claude/` with the knowledge repo's, which is where the librarian's own operating
procedure and the contract linter live — that one is described in
[`docs/reference/knowledge-repo.md`](./docs/reference/knowledge-repo.md).

**Nothing under `src/` loads a `.env` file.** `make` loads it for its own targets; a binary invoked
directly sees only the environment you exported. A "why is `$STIGMERGY_REPO` empty" debugging
session starts here.

## The invariants a change has to respect

Not style — these are enforced, and the enforcement is where to look when one fails:

| Invariant | Enforced by |
|---|---|
| `stigmergy.kernel` and `stigmergy.text` import nothing from this project — they are the bottom of the stack | `tests/test_architecture.py` |
| The librarian never imports the server; every cross-package reach is a named, exercised exception | same file, per-package |
| Every reader of `pages_index` names an ACL predicate, or is a listed exception | same file |
| No exception list keeps an entry that has stopped being used | same file, the pruning tests |
| The UNTRUSTED-DATA fence is built in `stigmergy.text` only | same file |
| The frozen contract linter and the frozen meeting brief match the knowledge repo's own | `tests/librarian/test_frozen_linter.py`, `test_meeting_brief_contract.py` |
| The diff the nine gates approved is the diff that lands | `gitcmd.commit(gated_entries=…)` + `tests/librarian/test_gitcmd_unit.py` |
| `server.acl.visible()` is the ONE place read access is decided, and the only implementation of it | `server/acl.py`, `tests/test_contract_parity.py` |
| The librarian reaches `stigmergy.entities` from `librarian/identity.py` only, and only for the birth fold, the generator and its errors — the birth writer is the one seam between filing and identity | `tests/test_architecture.py`, a named exception with its own pruning test |
| The shared mint sequence carries no authorization, so its caller set is closed to the surfaces that decide their own | `tests/test_architecture.py` — set equality both ways, so a caller that stops calling it fails too |
| The README's countable claims match the code | `tests/test_readme_claims.py` |
| `docs/reference/` names no command, variable or count the code does not have, and `docs/README.md` lists every document that exists | `tests/test_docs_claims.py` — ADRs are exempt by design: they record a decision, not the present |

## Working rules

- **Tests are the contract.** The suites under `tests/` are the behavioural invariant. Restructure
  freely; change what the code DOES only with a decision behind it.
- **Reproduce before you fix.** A bug gets a failing test that demonstrates it *before* production
  code is touched, and the test's own comment says what the old behaviour was. Several of this
  repo's worst defects were invisible to 3,000 green tests and visible in one deliberate mutation:
  *a rule nothing has tried to break is not a rule you know about.*
- **This repo owns everything it documents.** Documentation here never links to another project —
  not to a predecessor, not to a private checkout, not to anything that could be archived or made
  private. Corollary: never satisfy "remove the external reference" by deleting a link to an
  explanation — that leaves the repo clean and dumber. Port the explanation.
- **Documentation is rewritten, not appended to.** `README.md`, this file and every `index.md`
  describe what EXISTS. A code map that was appended to at every change and rewritten at none
  becomes a changelog whose accuracy is inversely proportional to its age. If a change makes a
  sentence false, correct the sentence in the same commit.
- **Do not write a comment the code will outlive.** A comment states a constraint the code cannot
  show: the failure that motivated a design, the attack a defense exists for, the "if you change
  this you must also change that". Counts, signatures and module names drift — prefer a sentence
  that stays true, or pin the fact in a test.

## Testing doctrine, in four lines

- **A benign twin for every defense.** A test that only proves a gate fires measures its
  sensitivity and never its specificity, and every gate here can bounce someone's real work.
- **A message containing a command is an executable promise.** If a refusal tells a human to run
  something, a test runs it.
- **Never fake what you are claiming to prove.** Real git, real Postgres, real gates — a faked git
  proves nothing about the property being claimed.
- **A check that stops running must be impossible to miss.** Skipped and retired dimensions are
  printed, counted and asserted — never quietly dropped. A permanently-green test is worse than no
  test, because it reads as coverage.

## Before you finish

`make lint` and `make test` both green. The suite is keyless by construction — if something you
wrote needs an API key to pass, it is in the wrong place.
