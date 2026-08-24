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
| Observable defect or false executable promise | `squad:fix` |
| New capability or moved product surface | `squad:define` then `squad:build` |
| Behavior-preserving restructuring | `squad:refactor` |
| Documentation-only correction | ordinary change closed by `squad:review` |

Read `squad:breaking-change` before changing a public tool contract, schema, persisted format, or
event shape. The current test deployment uses a clean cut: do not add migration, compatibility, or
rollback machinery unless a later specification explicitly requires it.

Use an independent security/architecture auditor when the change touches ACL decisions, identity
or token resolution, queue leasing or retries, Git gates, evidence storage, answer verification, or
any read path that can reveal restricted knowledge.

## Keep both repositories coherent

Changes to the filing contract normally affect both the platform and the knowledge repository.
Update the live librarian skill, thin linter/authorship launchers, templates, control files, and
workflows with the platform code and fixtures. The platform package owns the executable contract;
the knowledge repository must not carry a second implementation.

Model-owned knowledge paths are committed by the trusted writer identity and pushed through the
librarian GitHub App. Operator-owned controls and documentation use the normal contributor
identity. Both repositories must pass their own CI.

## Completion

1. Run focused tests, Ruff, and the complete keyless suite.
2. Run the proportional final-validation workflow and resolve every finding.
3. Commit and push both repositories with the required identities; wait for green CI.
4. Deploy every affected process group.
5. Follow `validate-deployment` for live evidence when the image or cross-system contract changed.

A cross-repository feature is not landed while either repository, CI run, deployment group, or
scheduled reconciliation path still points at a different contract.
