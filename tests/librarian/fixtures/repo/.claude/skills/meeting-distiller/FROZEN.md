# `SKILL.md` here is a FROZEN COPY

`SKILL.md` beside this file is a byte-for-byte copy of the meeting-distiller brief from the
**knowledge repo** (`stigmergy`), not a second version of it. Same arrangement, and the same
reasoning, as the frozen contract linter one directory over
(`../../tools/FROZEN.md`).

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/skills/meeting-distiller/SKILL.md` |
| **Copied at commit** | `7c6de3a44e69f87126efcc8b95d2cac5b54d8f1d` |
| **Drift guard** | `tests/librarian/test_meeting_brief_contract.py` |

## Why the copy exists (M9.5, ROADMAP item 7)

`test_meeting_brief_contract.py` is the two-sided check criterion 17 asks for: every rule the
brief states in its own words is paired with a marker proving the code implements it, asserted in
BOTH directions. It read the brief straight out of `../stigmergy` and **skipped whenever that
checkout was absent — which in CI is always**. So the one test written to catch the M8a failure
mode (a gate stops enforcing something while the brief keeps promising it) ran only on a machine
that happened to have both clones, and never on the pushes that gate the branch.

## Why a frozen copy is not a tautology here

Reading a vendored copy proves "the copy and the code agree", which is a weaker claim than "the
brief and the code agree" — and a green test making the weaker claim while looking like the
stronger one is precisely the *"passes for the reason it does not name"* defect this test file has
already fixed in itself twice (findings cycle 1 entries A1 and A2).

The two halves are therefore split, and both are named:

- **In CI** (`../stigmergy` absent) the contract table runs against this copy. Every code-side
  marker is checked against the live source, so a gate that stops enforcing a rule turns CI red —
  which is the half that was missing entirely.
- **On any machine with both clones** — which is every local run — a drift test asserts this copy
  is byte-identical to the real brief. Editing the brief without resyncing turns that test red and
  prints the resync command.

Neither half is sufficient alone. Together they say: the code is checked against a pinned
contract on every push, and the pin is checked against reality on every local run.

## Resyncing

```
cp "$STIGMERGY_REPO/.claude/skills/meeting-distiller/SKILL.md" \
   tests/librarian/fixtures/repo/.claude/skills/meeting-distiller/SKILL.md
git -C "$STIGMERGY_REPO" log -1 --format=%H -- .claude/skills/meeting-distiller/SKILL.md
```

...and record the new sha in the table above. A copy with no recorded provenance cannot be
resynced with confidence: "is this behind or ahead?" has no answer without the sha it was taken at.
