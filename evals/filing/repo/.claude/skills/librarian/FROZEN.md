# `SKILL.md` here is a FROZEN COPY

`SKILL.md` beside this file is a byte-for-byte copy of the librarian's operating procedure from
the **knowledge repo** (`stigmergy`), not a second version of it. Same arrangement, and the same
reasoning, as the frozen contract linter two directories over (`../../tools/FROZEN.md`).

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/librarian/SKILL.md` |
| **Copied at commit** | `31e49f8c0ce00c0ad4fd9fcf3128b4bbe0b3b4d9` |
| **Drift guard** | `tests/librarian/test_librarian_brief_contract.py` — the rule table, over the copy in `tests/librarian/fixtures/repo/`; this copy stays a yardstick with no guard (see `../../tools/FROZEN.md`) |

## Why the filing golden needs it

`agent.read_skill` reads `.claude/skills/librarian/SKILL.md` **out of the item's own worktree**
and injects it as the system prompt, and `worker.startup_checks` refuses an `sdk` or `pydantic`
run whose base commit does not carry one (ADR 033: both real backends inject this same brief —
only the ENVIRONMENT preamble in front of it differs). A mini knowledge repo without this file
cannot be filed into at all by either backend the instrument exists to measure.

## Why it is frozen rather than resynced

**The brief is the largest single input to filing quality, so a golden run has to say which
version of it produced the score.** That is the whole reason this copy is pinned rather than read
live out of a checkout: when the brief changes, the next baseline is measured against a *named*
predecessor instead of against "whatever the brief said that week", and the two entries in
`evals/history.ndjson` are comparable because the fixture sha in each row says what changed.

A change to the real brief is therefore measured by **re-freezing this file deliberately and
recording a fresh baseline** — a decision with a row in the series behind it — never by a quiet
`cp` that re-grades every entry already recorded.
