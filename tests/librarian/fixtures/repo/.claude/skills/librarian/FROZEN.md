# `SKILL.md` here is a FROZEN COPY — the DRIFT-GUARD one

`SKILL.md` beside this file is a byte-for-byte copy of the librarian's operating procedure from
the **knowledge repo** (`stigmergy`), not a second version of it. It is the ONLY brief this fixture
carries: `../meeting-distiller/` was its sibling until the meeting flow was deleted, and a
`kind="meeting"` capture is filed against this brief like every other one.

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/librarian/SKILL.md` |
| **Copied at commit** | `153849293b5876fb39868306f08a57c7e0ff4ade` |
| **Drift guard** | `tests/librarian/test_librarian_brief_contract.py` — the rule table runs against THIS copy in CI, and a separate test asserts the copy is byte-identical to the knowledge repo's own whenever that checkout is present |

> **The `edits` declaration RETIRED, and this row moved with it.** Three additive shapes
> (`backlink`, `overlap`, `contradiction`) an account named against a page that already existed,
> performed by a module of the librarian's own — removed from the code along with the elective
> repair loop that was its only other caller. A page that already exists is now changed only by a
> `rewrites` entry. Knowledge-repo commit `153849293b5876fb39868306f08a57c7e0ff4ade`.

> **The row above always names the CURRENT bytes.** The brief has been rewritten several times
> (tool-neutral under ADR 033, environment-conditional under ADR 034, the symmetric page-authorship
> opening, the plural park names of issue #32, the `names`-first spelling of issue #53) and each
> rewrite moved this row. The current bytes are the File First, Govern After brief: there is no
> park and no question to the submitter — an unknown name is PROPOSED in the account
> (`new_entities`, every field filled), a spelling for a registered entity is a `new_aliases`
> entry, and `file` is the only decision. The current bytes also carry the ONE PIPE rewrite: the
> account declares `pages` — a LIST, one entry per page the material establishes, each with its own
> optional `anchoring` and `links` — and a run that writes its own pages names them in
> `page_paths`. A future resync re-reads the sha the same way:
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
catches actually lands. Its retired sibling `test_meeting_brief_contract.py` learned this first,
and went with the flow it guarded.

**The structured ordinary flow reads it at run time.** `agent.read_skill` reads
`.claude/skills/librarian/SKILL.md` out of the item's own worktree and injects it as the
instructions, for every backend named in `agent.SKILL_READING_BACKENDS` — the `pydantic` one today.
Any integration test that drives a real backend over this fixture repo needs the file present at
the base commit — every capture, whatever its kind.

## Why it is RESYNCED rather than pinned

This is the **drift guard** copy, not the yardstick: it is resynced whenever the real brief moves,
so a brief edit that breaks the contract table turns this suite red in the same change. The
`evals/filing/repo/` copy is the opposite — pinned, so a golden score can name the brief version it
was measured under. They are byte-identical today and are expected to diverge; declared
duplication, not an oversight.
