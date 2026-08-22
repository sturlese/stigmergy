# Cron templates — copy these into your knowledge repo

Three scheduled jobs a deployment wants, and **they do not live in `.github/workflows/` on
purpose**: they are not this repository's CI, they are yours, and they belong in the repository
that holds your knowledge.

| File | Schedule (UTC) | Runs |
|---|---|---|
| `index-rebuild.yml` | ~04:17 nightly | `stigmergy-index --rebuild` |
| `retention-purge.yml` | ~04:42 nightly | `stigmergy-queue purge` |
| `gardener.yml` | ~05:07 daily | `stigmergy-gardener` |

**There is no repair cron.** The gardener writes findings, and the librarian WORKER answers them on
its own idle branch — deriving each repair, running the nine gates over the diff it produces and
pushing it as one commit, with nobody asked in between (ADR 044,
[repair.md](../../docs/reference/repair.md)). That job needs the librarian's App credential, which
is exactly why it belongs in the worker and not in a scheduled Actions run: a push credential in a
public runner's environment is the thing this file's own header is about.

## Why not here

**Actions logs on a public repository are readable by anyone, with no login**, and these jobs
narrate the corpus out loud — `stigmergy-gardener` prints its whole report, entity ids and page
paths included.
Repository *variables* are not masked either (only secrets are), so your knowledge-repo slug and
your model names would be in the clear on every run.

Your knowledge repo is private and is where the data already is. Run them there.

Two things get *better* by doing so, which is why this is not merely damage control:

- **No cross-repo credential.** The knowledge repo is then the workflow's own repository, so the
  job's read-only `GITHUB_TOKEN` covers the checkout. An earlier layout needed a fine-grained PAT
  for exactly this and it now has no reader.
- **No code is copied.** The CLI arrives by `pip install git+https://github.com/…@<ref>`, so
  nothing here has to be kept in sync with anything there. Pin `STIGMERGY_PLATFORM_REF` to a
  release tag to control when it moves.

## Installing them

1. Copy all three into `.github/workflows/` **in your knowledge repo**.
2. Set the secrets there: `INDEX_DSN` and `OPENAI_API_KEY`. No job here needs a Slack token —
   none of them posts anything; `stigmergy-digest` is the command that broadcasts, and it is not
   on a schedule.
3. Set the variables there: optionally `STIGMERGY_GARDENER_MODEL`, `STIGMERGY_PLATFORM_REF`
   (a release tag) and `STIGMERGY_PLATFORM_REPO` (if you run a fork). The repair pass's own
   model is the WORKER's environment, not this repo's — it does not run here.
4. Set `STIGMERGY_CRONS_ENABLED` to `true`. **Every job is gated on it**, so until you do, all
   three skip cleanly rather than failing a scheduled run every night.

A skipped job is GREEN, which is the failure mode to check first when the crons "stopped running":
read `job_runs` in the database, not the Actions tab. The operator runbook's Actions section is the
long version of all of this.

If you also run the admin console, point `STIGMERGY_ADMIN_GITHUB_REPO` at the repository these end
up in — the console drives *these* workflows — and scope its PAT to that repository.
