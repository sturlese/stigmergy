# `SKILL.md` here is a FROZEN COPY

`SKILL.md` beside this file is a byte-for-byte copy of the meeting-distiller brief from the
**knowledge repo** (`stigmergy`), not a second version of it. Same arrangement, and the same
reasoning, as its sibling one directory over (`../librarian/FROZEN.md`).

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/meeting-distiller/SKILL.md` |
| **Copied at commit** | `03aab8799f9778087ab78cc23fbbf9a809d52d5b` |
| **Drift guard** | none, on purpose — see `../../tools/FROZEN.md` |

## Why the filing golden needs it

`agent.read_meeting_brief` reads this path out of the item's own worktree on the first
`kind="meeting"` row a run claims, and fails closed without it. Two of this golden set's captures
are transcripts, so a mini repo missing this file would score those two as config failures rather
than as filing quality.

## Why it is frozen rather than resynced

The same argument `../librarian/FROZEN.md` makes about the ordinary brief, and one addition
specific to this one: the meeting brief and `librarian/gates.py` are a **two-sided contract**
(`tests/librarian/test_meeting_brief_contract.py`). Pinning the eval's copy is what lets a future
change to that contract be measured — the run before it and the run after it each name the brief
version they were judged under, so "did the new brief file better meetings?" is a question the
series can actually answer.

The sha above is the one every frozen copy in this fixture matches, not necessarily the commit
this file was physically taken at: these bytes are identical at its predecessor too, so the row
was aligned to the sha that describes the whole tree rather than re-copied — a record-only change,
recorded the same way in `../../../PROVENANCE.json`.

Note that this is a DIFFERENT copy, at a different freeze cadence, from
`tests/librarian/fixtures/repo/.claude/skills/meeting-distiller/SKILL.md`. That one is a drift
guard and is resynced whenever the real brief moves; this one is a yardstick and is not. They are
byte-identical today and are expected to diverge — declared duplication, not an oversight.
