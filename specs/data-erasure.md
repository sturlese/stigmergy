# Data erasure and EU residency

A workspace administrator can erase one layer completely, a platform operator a whole workspace,
and an operator the whole deployment in an emergency. Every original stays in the EU. Processors
and where data lives: [`docs/DATA-PROTECTION.md`](../docs/DATA-PROTECTION.md).

## 1. Constraints

- **Complete or not claimed.** After an erase no store the platform controls holds the erased
  content: current tree, Git history, evidence objects, Postgres rows, index, embedding cache,
  measures. What it cannot erase at once is named in the receipt with when it goes.
- **Everything else untouched.** Other layers' pages, sources, originals, history content, and
  answers stay; only commit SHAs change.
- **One writer.** Each erasure is a job of the single writer (main specification, invariant 4).

## 2. Evidence in the EU

The evidence bucket is an R2 bucket with the EU jurisdiction (endpoint
`<account>.eu.r2.cloudflarestorage.com`, checked by `validate-deployment`). Jurisdiction is fixed at
creation, so a bucket without it is replaced: create an EU bucket, copy every object (keys are
content hashes), point `STIGMERGY_EVIDENCE_*` at it, deploy, validate, then delete the old one.

## 3. Erasing a layer

### 3.1 Behaviour

Backoffice → Access → Layers → a layer → **Erase content**, for workspace administrators only.
The dialog shows what will go (pages by type, captures, originals, changes) and the last erasure's
state, and asks for the layer's name typed in full. The request (`layer_erasures`, one open per
layer, never `public`) is audited as `layer.erase`; the writer runs it before its next capture:

1. **Close the layer.** New captures to it are refused (`layer_erasing`) and its queued captures are
   not claimed. Other open work that names it fails with `layer_erased`.
2. **Rewrite history** in a scratch mirror: `git-filter-repo` removes `layers/<layer>/` from every
   branch and tag; commits left empty disappear (pull-request refs are dropped, any other ref kind
   stops the job). The commit map is stored, then the refs are pushed atomically with a lease on
   each, so a ref another writer moved fails the push instead of being overwritten.
3. **Checkout.** The writer resets its checkout to the rewritten branch plus anything filed since,
   and expires every object of the old history.
4. **Postgres**, in one transaction: delete the layer's captures with their artifacts, uploads, and
   Slack submissions; delete the change rows whose paths are all in the layer, rebuild the patch of
   a change that also touched other folders, and remap every other stored SHA (changes, captures,
   job runs, measure snapshots) through the commit map; delete the layer's measure series; redact
   queue, job-run, audit, and admin-action fields that name its folder, its captures, or its pages'
   virtual paths, titles, or aliases.
5. **Evidence.** Delete the originals, readable texts, uploads, and replaced patches that no row of
   any tenant still references.
6. **Index.** Full rebuild at the new head; drop embedding-cache entries no fragment uses; measure
   the head again.
7. **Receipt.** A `layer.erased` admin action with the counts, the old and new heads, and what
   remains outside the platform: GitHub's cached copies of old commits until GitHub Support removes
   them (with the first changed commits that request needs and a link to GitHub's guide), Supabase
   backups until their retention ends, and clones made before the erasure. No erased content.

Before the push a failed attempt is retried with backoff; after three, the request fails only if
its rewrite verifiably never reached the remote, which reopens the layer with nothing changed. Once
the push is recorded it never fails: steps 3 to 7 are idempotent and retried until done. A second
erasure in the workspace waits while another's rewrite may be pushed but unfinished. Deleting the
emptied layer stays the roster action, which refuses while anything still holds it.

The GitHub App must be allowed to force-push every branch and tag of the knowledge repository, and
the image ships `git-filter-repo`.

### 3.2 Not provided

Erasing one person across layers or everything one user submitted (done by hand on request), and
messages already posted in Slack.

### 3.3 Acceptance criteria

1. **ER-01** On a fixture with a marker planted in layer L's sources, notes, entity pages, and
   capture titles, after erasing L: no blob or commit reachable in the repository, no Postgres
   row, and no evidence object contains the marker or any of L's capture IDs.
2. **ER-02** Layer M's pages, sources, originals (including one byte-identical to an L original),
   history content, search results, and answers are unchanged.
3. **ER-03** During the erase a capture to L is refused, one queued for L is not claimed and is
   erased, and one for M waits and then lands.
4. **ER-04** A job stopped after the push completes when run again; a failed push changes nothing
   and a ref another writer moved is never overwritten.
5. **ER-05** Only a workspace administrator can start it, with the layer's name typed; the action
   is audited.
6. **ER-06** The index converges at the new head and Index health is healthy; every change row's
   SHA resolves.

## 4. Deleting a workspace

A platform operator deletes a suspended workspace from the master console (`specs/multi-tenancy.md`
§7): repository, rows, HNSW indexes, the evidence only it referenced, and checkout.
`workspace_erasures` keeps the request and its receipt; `platform_actions` records it.

## 5. Emergency erase

`docs/ERASE.md` erases every workspace of a deployment at once, each step with its exact command:

1. **Stop:** scale every Fly process group to zero.
2. **Revoke:** the OpenRouter key, the Slack bot token and app, and the GitHub App.
3. **Knowledge:** delete every repository in the GitHub organization and ask GitHub Support to
   remove them (a deleted repository stays restorable for 90 days); destroy the Fly app, whose
   machine holds the writer's checkouts; delete local clones.
4. **Evidence:** delete every object and the bucket, then the R2 tokens.
5. **Database:** delete the Supabase project, which deletes its backups.
6. **Check:** the repositories, Fly app, project, bucket, keys, and apps are gone.
7. **Record**, outside the system: when, who, what was erased, the Support ticket, and what remains.

## 6. Ready for outside data

Before a workspace files outside data: the DPAs are accepted; the people whose calls or messages
are captured are told; the outside data has its own layer and its users only that layer; a note
records purpose, readers, retention, and how erasure works.
