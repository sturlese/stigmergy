# Cron templates — copy these into your knowledge repo

Four scheduled jobs a deployment wants, and **they do not live in `.github/workflows/` on
purpose**: they are not this repository's CI, they are yours, and they belong in the repository
that holds your knowledge.

| File | Schedule (UTC) | Runs |
|---|---|---|
| `index-rebuild.yml` | ~04:17 nightly | `stigmergy-index --rebuild` |
| `retention-purge.yml` | ~04:42 nightly | `stigmergy-queue purge` |
| `gardener.yml` | ~05:07 daily | `stigmergy-gardener` |
| `repair-propose.yml` | ~06:07 daily | `stigmergy-repair propose` |

The last two are a pair, an hour apart on purpose: the gardener writes findings, and the proposer
reads that morning's completed run and turns the three findings a link or a callout can answer into
concrete, strictly additive proposals a steward decides one at a time. It **proposes and cannot
apply** — there is no `stigmergy-repair apply` — which is why it needs neither a write permission,
nor a Slack token, nor the librarian GitHub App credential
([repair.md](../../docs/reference/repair.md)).

## Why not here

**Actions logs on a public repository are readable by anyone, with no login**, and these jobs
narrate the corpus out loud — `stigmergy-gardener` prints its whole report, entity ids and page
paths included, and `stigmergy-repair propose` names every page it proposed an edit against.
Repository *variables* are not masked either (only secrets are), so your knowledge-repo slug, your
digest channel id and your model names would be in the clear on every run.

Your knowledge repo is private and is where the data already is. Run them there.

Two things get *better* by doing so, which is why this is not merely damage control:

- **No cross-repo credential.** The knowledge repo is then the workflow's own repository, so the
  job's read-only `GITHUB_TOKEN` covers the checkout. An earlier layout needed a fine-grained PAT
  for exactly this and it now has no reader.
- **No code is copied.** The CLI arrives by `pip install git+https://github.com/…@<ref>`, so
  nothing here has to be kept in sync with anything there. Pin `STIGMERGY_PLATFORM_REF` to a
  release tag to control when it moves.

## Installing them

1. Copy all four into `.github/workflows/` **in your knowledge repo**.
2. Set the secrets there: `INDEX_DSN`, `OPENAI_API_KEY`, and `SLACK_BOT_TOKEN` (the gardener's SLA
   notice, and the only one the fourth job does not want — a proposer that cannot apply has nobody
   to notify).
3. Set the variables there: `STIGMERGY_DIGEST_CHANNEL_ID`, optionally `STIGMERGY_GARDENER_MODEL`,
   `STIGMERGY_REPAIR_MODEL`, `STIGMERGY_PLATFORM_REF` (a release tag) and `STIGMERGY_PLATFORM_REPO`
   (if you run a fork).
4. Set `STIGMERGY_CRONS_ENABLED` to `true`. **Every job is gated on it**, so until you do, all
   four skip cleanly rather than failing a scheduled run every night.

A skipped job is GREEN, which is the failure mode to check first when the crons "stopped running":
read `job_runs` in the database, not the Actions tab. The operator runbook's Actions section is the
long version of all of this.

If you also run the admin console, point `STIGMERGY_ADMIN_GITHUB_REPO` at the repository these end
up in — the console drives *these* workflows — and scope its PAT to that repository.
