# `SKILL.md` here is a FROZEN COPY

`SKILL.md` beside this file is a byte-for-byte copy of the librarian's operating procedure from
the **knowledge repo** (`stigmergy`), not a second version of it. Same arrangement, and the same
reasoning, as the frozen contract linter two directories over (`../../tools/FROZEN.md`).

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/librarian/SKILL.md` |
| **Copied at commit** | `0a988bd153ced3202bbd7e822cb7acb59c403017` |
| **Drift guard** | none, on purpose — see `../../tools/FROZEN.md` |

## Why the filing golden needs it

`agent.read_skill` reads `.claude/skills/librarian/SKILL.md` **out of the item's own worktree**
and injects it as the system prompt, and `worker.startup_checks` refuses an `sdk` run whose base
commit does not carry one. A mini knowledge repo without this file cannot be filed into at all by
the backend the instrument exists to measure.

## Why it is frozen rather than resynced

**The brief is the largest single input to filing quality, so a golden run has to say which
version of it produced the score.** That is the whole reason this copy is pinned rather than read
live out of a checkout: when the brief changes, the next baseline is measured against a *named*
predecessor instead of against "whatever the brief said that week", and the two entries in
`evals/history.ndjson` are comparable because the fixture sha in each row says what changed.

A change to the real brief is therefore measured by **re-freezing this file deliberately and
recording a fresh baseline** — a decision with a row in the series behind it — never by a quiet
`cp` that re-grades every entry already recorded.
