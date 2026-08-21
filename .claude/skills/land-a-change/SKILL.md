---
name: land-a-change
description: >
  Pick the right delivery pipeline for a change in this repository and close it properly — which
  squad flow fits which kind of change, when the auditor is mandatory rather than optional, which
  changes also have to land in the knowledge repo, and what "done" means when a fix only takes
  effect after a deploy. Use when starting work on a tracker issue, a bug report, or a feature
  here, before writing any code.
---

# land-a-change: from an issue to something actually landed

`CLAUDE.md` holds the doctrine — the invariants, the testing rules, the documentation rule.
**This file does not repeat it**; a second copy of a rule is a copy that goes stale. What
follows is only what that file does not say: which pipeline to run, and what "landed" means
here rather than in a repo with one artifact and no deployment.

## Pick the pipeline

| The change is… | Run | Notes |
|---|---|---|
| A defect with observable wrong behaviour | `squad:fix` | Reproduce-first is this repo's own rule too, so the pipeline and the doctrine agree. |
| A string that promises something untrue (a refusal naming a dead variable, a report claiming an outcome that does not happen, a suggested action the gates forbid) | `squad:fix` | Still a defect: **a message containing a command is an executable promise**. The red test pins the true wording or exercises what the message names. |
| A comment or a doc paragraph with no runtime behaviour behind it | An ordinary PR, closed with `squad:review` | The full pipeline is ceremony here. Batch related ones into a single PR. |
| A new capability, or one that moves between surfaces | `squad:define` → `squad:build` | The spec's acceptance criteria are what the build verifies against. |
| Restructuring with identical behaviour | `squad:refactor` | The existing tests are the frozen invariant. |
| Anything changing a tool's documented contract, a schema, a persisted format, or an event shape | Read `squad:breaking-change` **first**, then the pipeline above | An MCP tool's docstring is a contract: clients rely on it. |

Two calls the flows will not make for you:

- **The auditor is opt-in.** `squad:fix` spawns it only when told the diff touches sensitive
  territory. Here that means: anything under `server/acl.py` or a read path, the answer
  verifier, the librarian's gates, the capture queue's leases and attempts, token or steward
  resolution, and the evidence plane. When in doubt on those, spawn it.
- **The documentator is conditional in the flow and unconditional here.** This repo's rule is
  that a change making a sentence false corrects that sentence in the same commit — so if the
  behaviour, a count, a route or a promise moved, documentation is part of the change, not a
  follow-up.

## The second repository

This platform's changes often have a half that lives in the **knowledge repo** (`$STIGMERGY_REPO`):
the librarian's and the meeting distiller's skills, the contract linter, the entity registry,
the ACL and steward maps. That repo has its own CI — a strict linter, gitleaks, and an
authorship check over zones and trust fields.

Consequences, both of which have bitten:

- A fix is not done until **both** repos are green. A change to how the agent files pages is a
  knowledge-repo change; the platform test suite will not notice it at all.
- An operator commit that touches the machine-owned zones (`sources/`, `views/`) makes that
  repo's authorship check red **on every later push** until the commit is added to its
  reviewed baseline, with its own test updated. Deleting content in bulk is exactly such a
  commit.

## What "landed" means

1. `make lint` and `make test` green (the suite is keyless by construction — if your change
   needs a key to pass, it is in the wrong place).
2. `squad:final-validation` to choose the targets rather than running everything blind.
3. PR merged — this repo's history is merge commits from topic branches.
4. **Deployed, if the change ships in the image.** A fix to a string the worker prints, to a
   gate, or to the console does nothing for anyone until `make deploy-staging` runs. Deploy
   configuration lives entirely outside the repo (`.env` plus Fly secrets — see
   `.env.example`), so a deploy is that command and nothing else.
5. Verified **on the deployment**, not only in CI, when the defect was found there. The
   `validate-deployment` skill is the systematic version of that.

## Pointing the operator CLIs at a deployment

Every `stigmergy-*` binary defaults to the local composition. To exercise one against a live
deployment, export `STIGMERGY_INDEX_DSN` (the deployment's own DSN — `.env` keeps it under a
deliberately different name so a test run cannot reach it) and the four `STIGMERGY_EVIDENCE_*`
variables, which `.env` stores under `R2_*` names for the smoke check. Getting the second half
wrong used to be silent and fatal — the row landed in the deployment's queue while the bytes
went to a local store, and the capture failed seconds later. `stigmergy-entities create`, the one
CLI left that enqueues, refuses that exact combination before anything is uploaded (exit 3,
`--allow-split-stores` to override), so the mistake costs a refusal instead of a capture.
