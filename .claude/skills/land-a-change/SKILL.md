---
name: land-a-change
description: >
  Select and complete the delivery workflow for a Stigmergy issue, defect, feature, or refactor.
---

# Land a Stigmergy change

Read `CLAUDE.md` and the active implementation specification before changing behavior. Use code
and tests as the authority when older prose conflicts with them.

## Select the workflow

| Change | Workflow |
|---|---|
| Observable defect or false executable promise | Reproduce it with a failing test, find the root cause, fix it, and keep the test |
| New capability or moved product surface | Write or update the specification's acceptance criteria first, then build against them |
| Behavior-preserving restructuring | Keep the tests green before and after; change no behavior in the same change |
| Documentation-only correction | Correct every sentence the change makes false, in the same commit |

Before changing a public tool contract, schema, persisted format, or event shape, list every caller
and stored format it affects. The deployment uses a clean cut: do not add migration, compatibility,
or rollback machinery unless a specification explicitly requires it.

Use an independent security/architecture auditor when the change touches ACL decisions, identity
or token resolution, queue leasing or retries, Git gates, evidence storage, answer verification,
any read path that can reveal restricted knowledge, code that parses or cuts text a capture brings
in, a loop around a paid model or embedding call, or how the index is written. Give the auditor the
fixes too, and audit again until a pass finds nothing high or medium.

## Keep the contract in one place

Knowledge repositories hold Markdown only; the platform package owns the executable contract. A
filing-contract change updates the live librarian skill, code, tests, eval corpus, and
documentation together. Model-owned knowledge paths are committed by the trusted writer identity
through the librarian GitHub App.

## Completion

1. Run focused tests, Ruff, and the complete keyless suite.
2. Review the diff against the invariants in `CLAUDE.md` and resolve every finding.
3. Commit and push; wait for green CI.
4. For a deployment you operate, deploy every affected process group and follow
   `validate-deployment` for live evidence when the image or a cross-system contract changed.
