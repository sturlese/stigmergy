# `SKILL.md` here is a FROZEN COPY — the DRIFT-GUARD one

`SKILL.md` beside this file is a byte-for-byte copy of the librarian's operating procedure from
the **knowledge repo** (`stigmergy`), not a second version of it. Same arrangement, and the same
reasoning, as its sibling one directory over (`../meeting-distiller/FROZEN.md`).

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/librarian/SKILL.md` |
| **Copied at commit** | `b4e4438a2ecee8f612ca8c3707c359273b1ffe3a` |
| **Drift guard** | `tests/librarian/test_librarian_brief_contract.py` — the rule table runs against THIS copy in CI, and a separate test asserts the copy is byte-identical to the knowledge repo's own whenever that checkout is present |

> **This copy has moved five times, and the row above always names the CURRENT bytes.** ADR 033
> rewrote the brief tool-neutral and the `sdk` retirement closed that rewrite's last debt (the
> environment paragraph that still described tool-holding runs); both landed in the knowledge repo's
> `c1e0996ed497e70a9df82661c367294b48207a16`. ADR 034 then gave the ordinary run its tools back, so
> the brief's own preamble became environment-CONDITIONAL rather than describing one run style —
> that touch is `0bf3c5462d50e72f5435ce61d61ba5f023e60388`. Most recently, the "Writing the page"
> section's opening was rewritten to state both page-authorship shapes symmetrically instead of
> favoring the container-building one (the imbalance measured 8 of 13 first-pass drafts missing
> frontmatter entirely, against 0 of 12 after) — that touch is
> `03aab8799f9778087ab78cc23fbbf9a809d52d5b`. Most recently again, the park section learned that a
> capture can leave SEVERAL things unregistered and must name every one of them in `triage.names`
> rather than fold them into one string — the brief half of issue #32, whose platform half is the
> `entity_names` port this fixture's suite exercises. That touch is
> `c6068fff232e12f4c452a9999c4b905654a5d07c`. Most recently of all, the same park section made
> `triage.names` its PRIMARY worked example instead of showing the retired singular `name` first,
> and stated `name` as accepted legacy input rather than as the ordinary shape — issue #53's brief
> half, whose platform half gave `pydantic_backend.OrdinaryTriage` the same inbound tolerance
> `agent.parse_outcome` already had, so the two outcome boundaries stop disagreeing about a
> spelling on the day `structured_ordinary` flips. That touch is
> `b4e4438a2ecee8f612ca8c3707c359273b1ffe3a`, the sha the row above records. These five shas are
> not interchangeable: the first four are history, the fifth is what these bytes are now. A future
> resync re-reads the fifth the same way:
>
> ```sh
> git -C "$STIGMERGY_REPO" log -1 --format=%H -- .claude/skills/librarian/SKILL.md
> ```

## Why the suite needs it

Two reasons, and they are different from the eval fixture's.

**The brief↔code contract runs in CI.** `test_librarian_brief_contract.py` greps a hand-maintained
rule table against this file in BOTH directions — the brief phrase must still be in the brief, the
code marker must still be in `processing.py`/`gates.py`/`agent.py` — and reading the brief out of
the knowledge repo would make that table skip on every CI push, which is exactly where the drift it
catches actually lands. Its sibling `test_meeting_brief_contract.py` already had to learn this.

**The structured ordinary flow reads it at run time.** `agent.read_skill` reads
`.claude/skills/librarian/SKILL.md` out of the item's own worktree and injects it as the
instructions, for every backend named in `agent.SKILL_READING_BACKENDS` — the `pydantic` one today.
Any integration test that drives a real backend over this fixture repo needs the file present at
the base commit, the same way the meeting tests need the distiller brief.

## Why it is RESYNCED rather than pinned

This is the **drift guard** copy, not the yardstick: it is resynced whenever the real brief moves,
so a brief edit that breaks the contract table turns this suite red in the same change. The
`evals/filing/repo/` copy is the opposite — pinned, so a golden score can name the brief version it
was measured under. They are byte-identical today and are expected to diverge; declared
duplication, not an oversight.
