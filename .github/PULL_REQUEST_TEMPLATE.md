<!--
The rules below are the ones CONTRIBUTING.md states; this is the checklist, not the explanation.
Read CONTRIBUTING.md if any line here is surprising.
-->

I have read CLA.md and I agree to it for this and all my future contributions to Stigmergy.

## What this changes, and why

<!-- One paragraph. If it fixes a defect, say what the old behaviour was — that sentence usually
     belongs in the regression test's own comment too. -->

## Checklist

- [ ] `make lint` and `make test` are green locally.
- [ ] A defect fix has a test that **failed before the fix**, and whose comment says what the old
      behaviour was.
- [ ] A new defense has a **benign twin**: a test proving it does *not* fire on legitimate work.
- [ ] Documentation is rewritten, not appended to — every sentence this change makes false is
      corrected in the same commit (`README.md`, `CLAUDE.md`, `docs/`, `specs/`).
- [ ] No prose here indexes anything outside this repository: no ticket number, no private
      checkout, no document that can be archived or made private. Port the explanation instead.
- [ ] Behaviour changes are consistent with `specs/karpathy-team-wiki.md` and `docs/ARCHITECTURE.md`,
      and correct any sentence there they make false.
