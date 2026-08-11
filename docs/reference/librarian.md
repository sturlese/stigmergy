# The filing engine — `stigmergy.librarian`

The back half of the fast lane: it drains the capture queue and turns each row into a committed page,
or into an honest refusal. Design record: [ADR 015](../decisions/015-librarian.md) (the agent/gate
split) and [ADR 016](../decisions/016-human-loop-and-entity-governance.md) (reading the three
repo-sourced inputs at the base commit; governed entity birth). The front half — submit, attribution,
the evidence plane — is [`capture.md`](./capture.md) and
[ADR 014](../decisions/014-capture-queue-and-attribution.md).
Code map: [`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).

```
capture_queue row (claimed, fenced by `attempts`)
  │
  ├─ retry collapse ────────────────── same hash + submitter + window  -> filed, at the first page
  ├─ already filed ────────────────── hash matches a page in the repo -> rejected, pointing at it
  ├─ secrets / PII over the MATERIAL  gitleaks + 4 patterns           -> rejected, whole
  │
  └─ ephemeral git worktree of the knowledge repo
       │
       │  ── the agent step has TWO shapes, and a backend DECLARES which one it answers ──
       │     (`filing_port.FilingAgent.structured_ordinary`; the brief is the same for both,
       │      only the ENVIRONMENT preamble in front of it differs — ADR 033)
       │
       │  EXPLORING (`double`)                 STRUCTURED (`pydantic`)
       │  ────────────────────────────         ─────────────────────────────────────────────
       │                                       code gathers the context (gather.py: entities,
       │                                         candidates + excerpts, link neighbourhood,
       │                                         the wikilink vocabulary — from the CHECKOUT)
       │  agent explores with Read/Glob/Grep    agent holds NO tool and explores nothing
       │  agent writes a NEW .md itself         agent returns the page's own TEXT in `page`
       │  outcome names `page_path`             CODE writes the page: filename from the title,
       │                                         folder from the type, frontmatter, the H1
       │
       │  code applies the outcome's DECLARED edits to existing pages
       │  code writes the attached `sources/` page(s), when the door asserted one (see below)
       │  code stamps the server-owned frontmatter
       ├─ gates over the diff: zone · binary-page · body-rewrite · secrets · pii · frontmatter · contract · anchoring
       │     └─ vetoed?  one corrective retry with the findings, then a terminal state
       │                 (no retry at all when no veto names a repair the agent can perform)
       └─ commit (librarian GitHub App) -> push (rebase-and-retry) -> filed, page@sha
```

Everything below the two columns is shared, byte for byte: one stamp, the same eight gates, the
same "exactly one new page per capture" cross-check, one commit path. That is what keeps two shapes
of one flow from becoming two flows.

## Where it sits

`stigmergy.librarian` is a **worker beside the API, not a layer above or below it.** It may import
`stigmergy.capture` (the queue primitives, the evidence plane, the operational spine) and
`stigmergy.kernel` (the ACL resolver, the entity registry, the document converters — a library,
importable from anywhere, [ADR 026](../decisions/026-the-purge.md) D4). It must **never** import
`stigmergy.server` or `stigmergy.answer`, and the server must never import it — they talk through the
queue, a durable row, so a slow agent run can never happen inside an HTTP request. Both edges are
asserted by `tests/test_architecture.py`.

One further edge is **declared**: `stigmergy.index.corpus`, reached by `edits.py` (the zone list)
and by `gather.py` (the corpus parse). It is a pure repo parser — frontmatter and the wikilink
graph over a directory, no database connection and no ACL surface — and the same reach
`stigmergy.views` already declares for the same module. Nothing here touches `pages_index` itself;
`stigmergy.index.store`, the connection, is reached by `cli.py` alone.

| Module | Does |
|---|---|
| `cli.py` | `stigmergy-librarian` — `once`, `run`, `status` |
| `worker.py` | the loop, the fail-closed `startup_checks`, the sweep, signal handling, the per-`kind` routing |
| `bootstrap.py` | `stigmergy-librarian-boot` — the DEPLOYED worker's entry point (clone, verify, exec) |
| `gitcredential.py` | `stigmergy-librarian-credential` — the git credential helper the container fetches with |
| `config.py` | every tunable, resolved once (`Settings.from_args`); the derived lease |
| `processing.py` | one item, end to end: dedup → worktree → agent → edits → stamp → gates → commit. `process_item` is the ordinary flow; `process_meeting_item` is a genuine second flow (a page SET); `process_drive_item` converts the fetched bytes to text and then delegates to `process_item` itself |
| `base_inputs.py` | the three repo-sourced inputs, read at the item's own base commit |
| `filing_port.py` | the PORT — the two calls `processing.py` makes, the `AgentRun` envelope, the fault contract, the per-flow side-effect rules |
| `agent.py` | the shared agent seam: the outcome contract, the fence, the prompts, the write-confinement rule, the system-prompt frame the brief is injected under, and the `backend` dispatch. Drives no model itself |
| `gather.py` | the deterministic gatherer: what the structured ordinary flow is HANDED instead of exploring — a pure function of (worktree, registry, material) ([ADR 033](../decisions/033-structured-filing-flow.md)) |
| `double.py` | the offline double: misbehaves on demand, behaves on ordinary material |
| `pydantic_backend.py` | the pydantic-ai backend: one structured call per flow, no tools, BOTH flows ([ADR 032](../decisions/032-filing-port-and-pricing-seam.md), [ADR 033](../decisions/033-structured-filing-flow.md)) |
| `pricing.py` | model id → $/MTok, for the backends that report tokens instead of dollars |
| `gates.py` | the deterministic vetoes over the diff |
| `edits.py` | code's own additive edits, from the agent's declaration |
| `page.py` | the page vocabulary — SEVEN known types, of which the fast lane may CREATE three — their folders, the server-owned frontmatter stamp, path identity (case/Unicode-fold), and what a filename may be (`unnameable_reason`, bounded in UTF-8 BYTES) |
| `gitcmd.py` | worktrees, the diff, the commit, the push |
| `githubapp.py` | app JWT → installation token → push URL; the commit identity |
| `acl_rules.py` | audience labels from the ordered path rules, fail-closed |
| `dedup.py` | the two deterministic dedup levels |
| `report.py` | what a person is told, one fact set rendered two ways |
| `errors.py` | the domain errors; `LibrarianConfigError` means the WORKER cannot run — and never crosses to the wire once an item is already claimed: a mid-run one becomes a fixed sentence naming only the stage, the detail goes to the operator's log (`worker.process_next`) |

## The command surface

```sh
.venv/bin/stigmergy-librarian once      # claim ONE item, file it, print what happened, exit
.venv/bin/stigmergy-librarian run       # the loop: poll, sweep, drain, until a signal says stop
.venv/bin/stigmergy-librarian status    # depth, the item in flight, the measured p50/p95 — reads only
```

`make librarian-walk` is how a human runs `once` with the real agent; `make librarian-status` is
`status`. Both exist because `make` includes and exports the gitignored root env file and a directly
invoked `.venv/bin/stigmergy-librarian` inherits nothing from it — see the runbook.

Two more entry points exist for the **deployed** worker only, and a person never types either:

```sh
stigmergy-librarian-boot           # clone the repo, verify checkout == base ref, exec `run`
stigmergy-librarian-credential     # git credential helper: a fresh App installation token, per request
```

`boot` is what `fly.toml`'s `worker` process group and the composition's `librarian` service run. A
container starts with no knowledge repo in it, so it clones one, refuses unless `HEAD` is
`origin/<branch>` resolved **from the remote**, strips the read path's
`OPENAI_API_KEY`, and then *execs* the loop so the container's PID 1 is the process SIGTERM has to
reach. `boot --check-only` does everything except the exec, which is what a deploy smoke check
wants. `credential` exists because `base_ref` fetches before every item and a container has no
operator git configuration to authenticate that fetch with — against a private repo an
unauthenticated fetch does not fail loudly, it quietly files against the clone-time snapshot
forever. Operational detail is in the runbook's deployed-worker section.

**Exit codes.** `0` for every terminal state correctly reached, `rejected`/`triage`/`needs_input`/
`failed` included: those are the worker doing its job. `2` when the TOOL cannot run (bad config, unreachable
database, missing gitleaks). `1` for a local error. `130` on Ctrl-C — except for `run`, which
installs its own handlers and exits `0` after stopping cleanly, because a requested stop is not a
failure and every supervisor's restart policy depends on that.

Conventions are `stigmergy-queue`'s, imported rather than re-rendered: `capture.cli.depth_line` for the
`queue: queued=3 · claimed=1` line, `capture.cli.format_ms` for every measured duration,
`capture.cli.RECLAIM_NOW` for the recovery command. The two tools sit side by side in one terminal
and must not speak different dialects.

### `once`, and the preamble it prints first

```
filing into /path/to/stigmergy-brain against origin/main@a1b2c3d4e5f6
  swept 1 stranded claim(s) back to the queue and failed 0 that had burned every delivery
  (claims held longer than 900s (15 min))
#42 filed — wiki/notes/Acme renewal.md@9f8e7d…, anchored to Acme Corp. Becomes searchable…
```

Two lines that look like decoration and are not:

- **`filing into <repo> against <ref>@<sha>`.** The worktree branches from **`origin/<branch>`** when
  the checkout has a remote — correct for a service, and *not* what an operator assumes while looking
  at their own working copy. See "The librarian branches from the remote" below; this line is what
  makes that answerable from the output instead of from a debugging session.
- **the sweep line.** `queue.claim_next` has always released timed-out claims on its own hot path, so
  the recovery was never missing — it was **invisible**, and `once` is exactly the surface where that
  matters: a walk drains by hand, so a row left `claimed` by an interrupted run looks permanently
  stuck until somebody happens to run the next command. The line appears only when a sweep actually
  moved something; a line printed on every invocation is a line nobody reads.

With `--json` the machine-readable object is the FIRST thing on stdout, so a consumer can read it
with `json.JSONDecoder().raw_decode` — which is why the two context lines go *inside* the object
(`base.ref`, `base.commit`, `swept`) rather than being printed in front of it. `once --json` prints
nothing else at all, including the refused-diff line: that path is only on the prose road, on stderr.

### `run`

```
filing into /path/to/stigmergy-brain against origin/main@a1b2c3d4e5f6
  polling every 3s; lease 900s (15 min); Ctrl-C stops after the item in flight
#42 -> filed
^C
finishing the item in flight, then stopping — no further items will be claimed. Press Ctrl-C again
for the same thing without waiting to poll.
stopped after 1 item(s)
```

**Ctrl-C is part of the interface.** What the messages promise is exactly what the code does, which is
less than a cooperative cancel: nothing can abort a `process_item` that is already running — there is
no cancellation point inside an agent turn, a gitleaks run or a push — so the item in flight always
runs to completion and **may well be filed, with a real commit.** The flags affect only whether the
NEXT item is claimed. Earlier wording promised the item "returns to the queue" and that "nothing was
committed"; in a real run the in-flight item finished and was filed, with the commit printed directly
under that promise. The only path where the row does come back is a hard kill, and the messages name
that path specifically.

### `status`

```
queue: queued=2 · claimed=1 · filed=37 · triage=4
in flight: #58 (raw) by ana@example.com attempts=2/3 held 3612.4s of 900s (15 min)
  LEASE EXPIRED — a live worker would have finished or renewed it by now; the next sweep returns it
  to the queue with an attempt burned
  to return it right now, with no librarian running:  stigmergy-queue reclaim --visibility-timeout 0
capture->filed latency: p50=48.2s · p95=91.7s over 37 filed captures
```

- It runs `startup_checks` **deliberately not at all**: an operator reaching for `status` is often
  doing so *because* something is misconfigured, and a status command that refuses to answer until the
  config is valid is useless exactly when it is needed. It needs a database and nothing else.
- It **writes nothing, including no schema.** `_connect` skips `ensure_capture_schema` (DDL) for this
  one subcommand, so `status` against a brand-new database does not create `capture_queue` — it prints
  a sentence naming that ("the capture queue has no schema in this database yet …") instead of the
  generic connection failure, because an operator staring at a database it is demonstrably connected
  to should not be sent hunting for a network fault. It does report a stale lease, but does not repair
  one; the `--visibility-timeout` it compares against is the one on its own command line, because a
  verdict computed against a different lease than the worker runs with would call every healthy agent
  item dead.
- **Three verdicts, not two, for an in-flight row.** Within its lease ("a worker is presumably on
  it"); lease expired with deliveries still left — the example above, where the next sweep RETURNS the
  row to the queue with an attempt burned; and lease expired with EVERY delivery already burned:

  ```
    LEASE EXPIRED and every delivery is burned (3/3) — the next sweep FAILS this row rather than
    returning it to the queue, and records an ingest error
  ```

  which the sweep FAILS outright (`queue.release_expired` splits the expired set) rather than
  requeuing — and the `stigmergy-queue reclaim` advice is withheld in that case, because reclaiming a
  row with no deliveries left fails it too rather than recovering it. `--max-attempts` is compared
  against for the same reason `--visibility-timeout` is: a verdict computed against the class default
  while the worker runs with another one is how the earlier, two-verdict version came to promise a
  requeue for a row the sweep was about to fail.
- The staleness verdict comes from `queue._LEASE_EXPIRED`, the same SQL predicate the sweep acts on,
  so "looks stale" and "will be returned by the next sweep" are one fact rather than two estimates.
  The age is computed **in Postgres**: `claimed_at` was written by the database's `now()`, and
  subtracting a local clock from it would fold this machine's skew into the one number an operator
  uses to decide whether a worker is dead.

### The latency measurement, and when it refuses

capture→filed p50/p95, computed **from the trace alone** — `created_at` and `finished_at` on the queue
row, nothing instrumented, nothing the librarian has to remember to write. It is the instrument the
fast lane's own capture→page target (p50 < 5 min) is settled with, so the one thing it must never do
is produce a confident-looking number nobody should believe:

```
capture->filed latency: not enough data yet — 3 filed captures so far, 10 needed before p50/p95
mean anything
```

Below `latency.MIN_SAMPLES` (10) no percentile is computed at all — not
computed and labelled unreliable, *absent*, so a caller that forgot to check `enough_data` renders
nothing rather than a p95 off three samples. Only `filed` rows count: a `rejected` row's latency is
the latency of a refusal, and a `triage` row has no `finished_at` at all because a row waiting for a
human is not done.

## Configuration

Everything tunable, resolved in exactly one place (`config.Settings.from_args`), precedence
**CLI flag → env var → class default**. No module in this package reads the environment at import
time. Model ids are configuration, never constants — models get deprecated and a hardcoded id is a
landmine.

| Var (flag) | Default | Meaning |
|---|---|---|
| `STIGMERGY_REPO` (`--repo`) | `../stigmergy-brain` | the knowledge-repo checkout the worktrees branch from |
| `STIGMERGY_LIBRARIAN_BRANCH` (`--branch`) | `main` | the branch the fast lane commits to |
| `STIGMERGY_LIBRARIAN_BACKEND` (`--backend`) | `double` | `pydantic` runs both flows STRUCTURED — no tools, a gathered context, code writes the page (see below); `double` is the offline double. A retired third value, `sdk`, is refused at startup by name |
| `STIGMERGY_LIBRARIAN_MODEL` | `anthropic:claude-sonnet-5` | a Sonnet-class model is right for routine filing. PROVIDER-PREFIXED: pydantic-ai reads a bare name as an OpenAI model, so a worker without a prefix is refused at startup |
| `STIGMERGY_LIBRARIAN_PRICING` | — | `{"<model>": [input, cached input, output]}`, dollars per MILLION tokens, merged per id over `librarian/pricing.py`'s own table. Only the backends that report tokens rather than dollars read it |
| `STIGMERGY_LIBRARIAN_MAX_TURNS` | 30 | **DEPRECATED — read by no shipped backend.** It bounded a tool-using conversational loop, and the backend that had one retired; a structured call makes one model call. Still parsed, so it is not silently dropped — but a malformed value fails the boot with a Python error, not a named refusal (`..._TIMEOUT_S` below is the one that is refused by name). Removal is a recorded follow-up |
| `STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS` | 120 | **DEPRECATED — read by no shipped backend**, same reason as the row above: no agent holds a tool to count calls of |
| `STIGMERGY_LIBRARIAN_GATHER_TOP_K` | 12 | the STRUCTURED shape only: how many existing pages the gatherer offers the model as overlap candidates |
| `STIGMERGY_LIBRARIAN_GATHER_EXCERPT_LINES` | 20 | the STRUCTURED shape only: how many lines of each candidate it shows |
| `STIGMERGY_LIBRARIAN_TIMEOUT_S` | 300 | per-item wall clock (enforced by us) |
| `STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S` | 600 | the retry-collapse window |
| (`--poll-interval`) | 3.0 | `run` only; must be > 0 |
| (`--visibility-timeout`) | 900 | derived: `2 × timeout_s + 120s` gates `+ 180s` headroom |
| (`--max-attempts`) | 3 | deliveries before an item is failed; must be ≥ 1 |
| `STIGMERGY_GITLEAKS_BIN` | `gitleaks` | resolved on PATH, existence checked ONCE at startup |
| `STIGMERGY_LIBRARIAN_WORKTREE_ROOT` | system temp | where ephemeral worktrees live |
| `STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR` | `<tmp>/stigmergy-refused-diffs` | where a refused diff's digest is written |
| `STIGMERGY_LIBRARIAN_APP_ID` / `_INSTALLATION_ID` / `_PRIVATE_KEY` (or `_PRIVATE_KEY_FILE`) | — | the GitHub App; all or none |
| `STIGMERGY_LIBRARIAN_APP_LOGIN` | `stigmergy-librarian` | your App's **slug**, which GitHub derives from its name and which in turn derives the identity every commit is authored by (`<id>+<slug>[bot]@users.noreply.github.com`). Deployment-specific: set it unless your App is called exactly the default. **Wrong is silent where it happens and loud one repository over** — the commits push fine and simply stop rendering as the App, while the knowledge repo's `check_trust_authorship.py` rejects all of them, since a check against forged authorship has to pin one identity. For the same reason, renaming an App that already has commits in the repo splits the history across two logins: leave a working App's name alone |
| `STIGMERGY_LIBRARIAN_REPO_URL` (`--url`) | — | inside THIS package, `stigmergy-librarian-boot` only: where a DEPLOYED worker clones `$STIGMERGY_REPO` from, since a container starts with no checkout. The identical env var is also read independently by `stigmergy.server.settings`, for a server-driven entity mint ([ADR 030](../decisions/030-server-side-entity-minting.md)) — see the runbook's [Draining parked rows](./operator-runbook.md#draining-parked-rows) |
| `STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE` | — | set to `1` by `stigmergy-librarian-boot`, never by hand: refuse an item whose base did not come from the remote. See below |

**`STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE` — why the deployed worker refuses what a laptop
accepts.** `gitcmd.base_ref` fetches before resolving the base, and answers a *failed* fetch with a
warning and the local branch. That is correct on a laptop (an offline run files against the branch
you are on) and wrong in a container: `stigmergy-librarian-boot` refuses at startup when the base did
not come from the remote, precisely because a worker whose credential has been revoked would
otherwise file against its own stale clone. The fetch runs again per item, though, so a token that
expires an *hour after boot* walked the worker straight back into the state the startup check
refuses — judging captures against the ACL config, entity registry and contract linter of a commit
the remote moved past, while the steward flow (`approve` → push → requeue) depends on that fetch
working. The boot entry point is the only code that knows the process is containerized, so it
exports this flag and `processing.process_item` enforces it per item.

When it fires, the worker **stops** rather than failing the capture: the fault applies identically
to every row behind this one, so finishing them one at a time would drain the queue into `failed`
for as long as the credential stays broken. The item stays `claimed`, its lease expires, the next
start's sweep returns it to `queued`, and `stigmergy-librarian-boot` refuses at startup with the same
sentence. Nothing is lost and nobody is told their capture failed. The usual cause is the App
installation (revoked, or a token that has expired) or the network.

**An explicitly passed value is never discarded in silence.** Resolution tests `is None`, not
falsiness, so `--visibility-timeout 0` reaches `startup_checks` and is refused *out loud* with the
arithmetic, instead of being silently replaced by 900 and then quoted back at the operator who asked
for something else. A flag a human typed must take effect or be refused; being ignored is the one
outcome that teaches them the tool lies. `--poll-interval 0` and `--max-attempts 0` are refused for
the same reason and with their own sentences (a tight claim loop; every delivery starting exhausted).

### Two backends behind one port

The agent step is a named, typed port — `librarian.filing_port.FilingAgent`, two keyword-only calls
(`run` for an ordinary capture, `run_meeting` for a transcript), one `AgentRun` envelope back, one
fault contract. Two implementations answer it, and `STIGMERGY_LIBRARIAN_BACKEND` picks one:

| Backend | Flows | Ordinary shape | Model string | Cost |
|---|---|---|---|---|
| `pydantic` | every flow | **structured** — no tools, a gathered context, code writes the page | provider-prefixed (`anthropic:claude-sonnet-5`) — pydantic-ai resolves it | computed from tokens through `librarian/pricing.py` |
| `double` | every flow | **exploring** — writes the page through the same confinement rule | none — no model runs | `0.0`, and it says so |

**A third backend, `sdk`, was retired** ([ADR 033](../decisions/033-structured-filing-flow.md)):
it drove the Claude Code harness, exploring the checkout with Read/Glob/Grep and writing the page
itself. Nothing about the port changed when it went, which is what the port is for. A deployment
still configured for it is **refused at startup by name** — the message says the backend was
retired rather than mistyped, names `pydantic` and the provider-prefixed model id as the two
edits it takes, and gives the image rollback (`fly releases` → `fly deploy --image`) for getting a
worker running again meanwhile. Nothing is lost while it is down: the queue is durable.

The image lost the Node runtime and the agent CLI with it, and `claude-agent-sdk` is no longer a
dependency of this project.

**A backend DECLARES its ordinary shape; nothing infers one.** `FilingAgent.structured_ordinary` is
a class attribute `processing._one_pass` reads, and it decides three things: whether the gatherer
runs before the call, whether the account is expected to CARRY the page's text (`Outcome.page`) or
to name a path it wrote (`Outcome.page_path`), and whether code writes the page. A type test would
put the branch inside the worker's knowledge of which classes exist, so a fourth backend would take
the wrong road by being the wrong class rather than by declaring the wrong thing.

**`pydantic` serves a worker, and the refusal that said otherwise is gone.** M1 refused it
outright, because a worker's queue carries ordinary captures too and a backend serving one `kind`
would have burned deliveries one row at a time while looking configured
([ADR 032](../decisions/032-filing-port-and-pricing-seam.md) D3). It serves both flows since
[ADR 033](../decisions/033-structured-filing-flow.md), so what `worker.startup_checks` validates is
what was always about the BACKEND, each refused out loud: a model string with no provider prefix
(pydantic-ai reads a bare name as an OpenAI model, so inheriting it silently would file through a
provider nobody chose), a model with no configured price, and a missing provider key
(`anthropic:`→`ANTHROPIC_API_KEY`, `google-gla:`→`GEMINI_API_KEY`, `openai:`→`OPENAI_API_KEY`; an
unrecognized prefix is a warning, not a refusal — the adapter stays provider-agnostic).

**One of those keys is a dead end on the DEPLOYED worker, by design.** `stigmergy-librarian-boot`
strips `OPENAI_API_KEY` from the container before exec'ing the loop — it is the READ path's embedder
key and Fly secrets are app-wide, so stripping it is the only place the write path can be kept
independent of it. An `openai:` filing model therefore meets the missing-key refusal in the
container whatever the operator exports, while working perfectly on a laptop. The refusal names that
case specifically and offers only models the deployed worker can actually authenticate as; the
intersection of the two tables is pinned by a test, so a second read-path-only key cannot silently
make another provider family undeployable. The
librarian skill is proven at the base commit for every backend that INJECTS it
(`agent.SKILL_READING_BACKENDS`); the offline double reads none.

**A model spelling belongs to a backend.** While two real backends existed the rule ran both ways
— a bare id was refused on the one that wanted a prefix, and a prefixed id on the one that wanted
a bare name — because refusing one direction and silently accepting the other caught exactly half
of the same configuration mistake. One backend is left and the surviving half is the whole of it:
a bare id is refused, and the message says so in the terms a deployment mid-upgrade needs, since
changing the backend and not the model lands exactly there. The `double` reads no model at all and
is silent about it.

**Why an unpriced model is a refusal rather than a zero.** `report.cost_usd` is the row an operator
asks "what did this cost?", and a backend that reports only token counts can answer it only through
a price table. A missing entry that resolved to `$0.00` would read as free — so `pricing.py` refuses
at startup instead, naming the id, the `STIGMERGY_LIBRARIAN_PRICING` line that fixes it, and the
date the table was last set by a human (`AS_OF`). The table is configuration for the same reason
model ids are: prices move, and an introductory rate expires on a date nobody wants to learn from a
bill.

### The gatherer — what the structured shape is handed instead of a search

`librarian/gather.py` is a pure function of `(worktree, registry, material)` plus `gather_top_k` and
`gather_excerpt_lines`. It runs before the model call on the structured shape only, and it produces
four things:

- **the entities the material names**, resolved through the registry's own alias map (never a second
  matching rule — `gates.registry_candidates` is the one reading of "which entities exist"), each
  with its registry id, its aliases and the path of its own page when this brain has one;
- **the top-K candidate pages** it lexically overlaps with, each with a bounded excerpt and its own
  outbound link names. The score is an integer: `3 × title + 2 × its links + 1 × body` term overlap,
  ties broken by path. The corpus decides what a stopword is — a term more than half the pages carry
  is dropped rather than counted, so nobody maintains a word list for a corpus that need not be in
  English;
- **the link neighbourhood**, one hop out from those candidates and the entity pages. This is the
  half a lexical score cannot find: a capture may share no vocabulary with the page it belongs
  beside;
- **the wikilink vocabulary** — every page name in the repo, read through `edits.page_names`, which
  is the SAME function `edits.validate` later answers "does this link resolve" with. One reading, so
  the gatherer cannot offer a name the edit validator would refuse. It is bounded, and it says so
  (`link_names_total`), because a truncated list would read as proof that a name does not exist.

**It reads the checkout, never `pages_index`.** The worktree is the knowledge repo at this item's
base commit — the same data the exploring agent's own `Read`/`Glob` reached — so ADR 033 moved the
reader and not the data's origin. Reading the index would put a write-path worker on the read path's
ACL-governed table and would need an exception it does not need. There is no semantic-similarity
gathering either; reopening that is a design with an ACL question in it, not a patch.

**"The same data" holds because of one filter.** The agent's own reads were resolved before being
allowed (`page.is_inside`); `corpus.load_pages` has no such notion, so `gather._confined` drops
every page that is a symlink or does not resolve inside the worktree, and the wikilink-vocabulary
walk gets the same treatment. Without it the structured shape would read more of the filesystem
than the shape it replaces. A dropped page is logged at WARNING — a symlinked page inside a
knowledge repo has no legitimate producer in this system.

Page excerpts are captured material coming back into a prompt, so they render through the same
UNTRUSTED-DATA fence the material does. The registry half is rendered outside it, and what makes
that safe is the JSON escaping plus `text.sanitize` — not provenance: the entity ids and names are
server-owned, but each entity's page PATH is a filename a person chose.

**The whole block is bounded** (`agent.MAX_GATHERED_CHARS`), not only field by field: `top_k`,
`gather_excerpt_lines` and the per-line clamp multiply, and two of the three are operator-tunable.
Over the ceiling, the lowest-ranked candidates are dropped whole — never a JSON value cut in half —
and the block says so, because a model told "these are the candidates" about a silently shortened
list is being misled about its own context.

### `STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR` — the refused diff, preserved

A veto reaps the worktree, so until this existed the offending diff was gone the moment it was
refused: the report said *that* the agent rewrote a body and never *what* it changed, which for a
defect that will recur is a debugging dead end. When both agent attempts are vetoed, a bounded,
redacted digest of the diff is written to
`$STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR/<submission-id>-<timestamp>.diff`, and `once` prints the **path**
on stderr — never its contents:

```
  the refused diff is preserved for diagnosis at /tmp/stigmergy-refused-diffs/58-20260726T144212Z.diff
```

What is preserved is deliberately asymmetric, and the asymmetry *is* the safety property:

- **removed lines are kept verbatim** — they are content already committed in this repo, the thing
  that was about to be destroyed, and the only thing that answers "what did it change";
- **added lines are withheld entirely**, count only — they are the librarian's draft of untrusted
  captured material, so writing them to a file beside the queue would be the same mistake as putting
  a secret in a log.

Bounded twice (200 lines, 32 KB) because a diff is attacker-influenced in size. The default lives
under the system temp dir and deliberately **not** under `gitcmd.WORKTREE_PREFIX`: startup reaping
deletes anything whose name starts with that prefix, so a diagnostics directory named like a worktree
would be swept away by the next run that needed to read it. The path is an operator-facing value and
never crosses to a submitter — it is not part of the report.

## Three types the fast lane may create, seven it knows

`page.PAGE_TYPES` is the one table every placement question reads, and it answers **two** questions
that are easy to conflate: `known` is the management scope — what may be read, linked and
cross-referenced, so a `decision` page is free to link an `entity` page — and `creatable` is the
operational scope, what this lane may MINT. Only the first three rows carry a folder:

| Type | Folder | Fast lane may create |
|---|---|---|
| `note` | `wiki/notes` | yes |
| `decision` | `wiki/decisions` | yes |
| `concept` | `wiki/concepts` | yes |
| `entity` | — | no — "identity pages are created through a steward's review, not the fast lane" |
| `source` | — | no — written by code from captured material, never drafted |
| `meeting` | — | no — arrives with the meeting distiller |
| `view` | — | no — regenerated from an entity's members, never captured |

A refused type is **parked in `triage` with its own reason**, never quietly downgraded to `note`: a
per-type exemption is exactly how ambient ownerless content accumulates. `gate_zone` derives the type
from the FOLDER the page actually landed in (`_type_for_path`) and checks it against the run's own
`ctx.creatable_types` over the real diff, so the agent's judgment is an *input* to the decision and
never the decision. It asks the CONTEXT rather than the global `page.ensure_creatable` because the
creatable set is per-flow: the meeting flow and the fast lane's source attachment each widen it for
the duration of one item, and neither may widen it for the process.

**Seven rows, one per WRITER — and the table was deliberately cut down to that.** The three
creatable rows are the librarian's only genre choice; each of the other four has exactly one
stamper: `entity` the governed door (`stigmergy-entities`), `meeting` the distiller, `source` the
provenance writer (`processing._build_source_parts`), `view` the regenerator (`stigmergy-views`).
The candidates that were cut, so nobody re-adds one by pattern-matching: a person, a team, a
product, a customer or a project is an ENTITY, and an entity's own kind lives in the registry's
`type` field (`person`, `organization`, `product`, `tool`, `repository`, `place` —
`entities.generator.ENTITY_TYPES`, written on the page as `entity_type`), because two taxonomies
for one spine is duplication; `meta` because index/log/schema are Postgres, git history and
`CLAUDE.md`; `dataset`/`metric` name a store this system does not have;
`playbook`/`postmortem`/`policy` appear in no reference and no code. Adding one back is one row
plus one template plus one linter line; removing one migrates pages — which is why erring small is
the cheap direction.

The table is mirrored by the knowledge repo's own contract linter (`stigmergy_lint.py`'s
`VALID_TYPES`), and the two must agree: a type this table knows and the linter does not is a page
the linter refuses at the gate that judges the diff.

## The outcome contract, and the shape of a declared edit

The agent's only channel back is one JSON file it writes in the worktree, consumed and discarded
before the diff is taken (so it can never reach a commit or trip the zone gate):

```json
{
  "decision": "file",
  "page_path": "wiki/notes/Acme renewal moved.md",
  "page_type": "note",
  "title": "Acme renewal moved",
  "anchoring": {"kind": "entity", "entities": ["Acme Corp"], "reason": ""},
  "links_created": ["Acme Corp"],
  "overlaps": [{"path": "wiki/notes/Renewal pipeline.md", "note": "covers the same ground"}],
  "edits": [
    {"path": "wiki/notes/Renewal pipeline.md", "kind": "overlap",
     "link": "Acme renewal moved", "note": "covers the same ground"}
  ],
  "findings": [{"category": "declare-canonical"}],
  "summary": "filed the capture as a note"
}
```

`decision` is `file` or `triage`; a `triage` outcome carries
`{"kind": "unresolved-entity" | "unsupported-type", "name": …, "judged_type": …}` instead and the
worktree must be **clean** — an agent that parked the capture and left changes behind is an agent that
wrote and then said it did not, so the diff decides here as everywhere else.

**`edits` is the amendment ADR 015 §3 records.** The agent *names* the edit; `edits.apply_declared`
performs it. Three kinds, and each is an append:

| `kind` | What code writes on the OTHER page |
|---|---|
| `backlink` | adds the new page to that page's `related:` list |
| `overlap` | the `related:` link plus an overlap callout carrying `note` |
| `contradiction` | the `related:` link plus a contradiction callout carrying `note` |

`path` must be an existing page inside the three fast-lane folders; anything else is a finding, and the
edit is not attempted. Nothing here is exempt from the gates: code's edits land in the same diff and
`gate_body_rewrite` judges them exactly as it judged the agent's — proven against the BASE COMMIT's
own blob, never against a rendered diff, and a `related:` change is admitted only when its link set
strictly GROWS. `findings[].category` is filtered to a fixed set (`declare-canonical`,
`write-outside-lane`, `reveal-credentials`) — a category the agent invented is dropped rather than
echoed, which is what makes "the report never quotes the payload back" a property rather than a hope.

What `edits.apply_declared` actually changed reaches the submitter as `pages_edited` in the report —
every page OTHER than the filed one that this commit touched. It is a distinct field from
`overlaps_flagged`: that one is the agent's JUDGMENT about what overlaps; `pages_edited` is what code
actually wrote.

Reports also carry `cost_usd` — the real dollar spend of the item's agent passes, a pass that died
mid-run included (the fault carries its own figure on the exception, exactly like `agent_attempts`;
a timeout is the honest `0.0`, since nothing ever arrived to price). Where the per-run figure comes
from is the backend's business and the report's shape does not change with it: a backend priced by
its own provider passes that number through, while a backend that reports only TOKENS has
them multiplied by `librarian/pricing.py`'s configured $/MTok table. The rule: present, possibly
`0.0`, on every outcome that passed
through an agent loop or the failure road — filed, refused, parked and `failed` alike — and
absent only on the terminal states decided before the loop: a duplicate, a `filed_retry`, a
material-level secrets/PII rejection. Operators read it from the stored row (`stigmergy-queue
show`, the admin console); the client-facing `brain_submissions` shape deliberately strips it,
the same operator-telemetry line `ask`'s `usage` draws (ADR 031). It exists because the number
used to die with the run object, leaving the one row that answers questions about an item unable
to answer the operator's first one: what did this cost?

`anchoring.kind` is `entity` (with `entities`, each resolving through `ops/entity-registry.json`) or
`company` (with a written `reason`). There is no third value: silence is not an anchoring outcome.

The agent's `summary` reaches the submitter as `agent_rationale` — on a filed report and on both
parked ones. Every other field is code's observation of WHAT happened (page, commit, anchor, links,
overlaps); this is the only one that says WHY this type, why this folder, why that
anchor, and the whole design rests on the agent judging exactly those. It travels under a name
that says whose account it is, because it is a claim rather than a fact: the gates have already
refused any disagreement between the claim and the diff, but the sentence is the agent's own. It also
makes the anchoring residual mitigable — a submitter who can see the reasoning behind an anchor can
object to it far better than one who can only see the anchor.

### Kinds of field, kinds of bound

The outcome file is untrusted input — written by a model that has just read untrusted material — so
every field is bounded at the boundary. The bound depends on the KIND of field, and one rule for all
of them was a defect:

| kind | fields | bound | over it |
|---|---|---|---|
| identifier (`MAX_IDENTIFIER_LEN`) | `page_path`, `page_type`, `title`, `triage.*`, an edit's `path`/`link`, an overlap's `path`, a finding's `category`, an entity name | 400 characters | **refused** — it names something the worker resolves, so a longer one is a defect |
| prose (`MAX_PROSE_LEN`) | `summary`, `anchoring.reason`, an edit's or an overlap's `note` | 2000 characters | **truncated** — it is a sentence for a person, and `report._clean` clamps it before anyone reads it (200 characters; 400 for `summary` — `report.RATIONALE_WIDTH`, whose content is the whole reason it is carried) |
| page body (`MAX_PAGE_BODY_LEN`) | the MEETING flow's own drafted bodies — a decision's `body`, the meeting page's `meeting_notes` | 20000 characters | **truncated** — a whole page, not one sentence; the contract linter still refuses a body genuinely too long to file, with a repair brief that says so |
| list (`MAX_LIST_LEN`) | every list field — `links_created`, `overlaps`, `edits`, `findings`, `anchoring.entities`, and the meeting outcome's `decisions`/`attendees`/`action_items` | 200 entries | **refused, correctably** — the list is emptied and a shape finding is raised |

Identifier and prose were both 400 and both refusals, which refused a whole capture over the 401st
character of a `summary` — a field that, at the time, nothing downstream even read. It is read now,
as `agent_rationale` above, which is why the clamp on it is the widest of the prose clamps rather
than the strictest.

### A malformed outcome is CORRECTABLE, and the agent is told

Refusals from the boundary split by whether telling the agent could plausibly fix it:

- **shape** — an unrecognized `decision`, an unrecognized edit `kind`, a field of the wrong type, a
  filing with no `title`, a park with no `triage.kind` (or without the field that kind's report needs),
  an identifier over its bound. These come back as `gates.Finding`s on `errors.OutcomeShapeError` and
  go into the **one corrective retry** exactly as a gate veto does; the retry resets the worktree
  first, so the agent writes the page again from scratch. Every problem in one outcome is reported in
  one pass, for the same reason every gate runs every attempt.
- **structural** — no outcome file at all, an unreadable one, one over the 256 KB ceiling, invalid
  JSON, nesting past 8 levels. These stay `AgentError`: an agent cannot be talked out of not having
  written a file, and the byte and depth ceilings are resource bounds rather than requests.

If the corrective pass does not fix a shape problem the item still lands `failed` with stage
`outcome`, naming what was wrong and how many agent passes ran.

### A veto that names no repair does not spend the retry

The corrective retry exists to reach a pass with no vetoes. A veto the agent cannot act on makes
that unreachable, so the second pass is not a chance — it is the same refusal, one agent run later.
Those vetoes are marked `repairable=False` in `gates.py` and the item refuses after **one** agent
pass; the report's `agent_attempts` then reads `1`, which is how an operator can tell this branch
was taken.

Six finding codes, across three gates, and every one of them judges part of the diff the agent
cannot write — it may create new pages only, never modify one:

| veto | why there is no repair |
|---|---|
| `zone/body-rewrite` | judges a MODIFIED page, which only `edits.apply_declared` produces. "You rewrote existing content in X" names work the agent did not do; the reachable cause is a target page whose `related:` block cannot be proved to have grown |
| `zone/unreadable-edit` | same gate, same subject: the version an edit started from could not be decoded, so nothing about the draft is in question |
| `zone/unparseable` | same gate again: the frontmatter an EDIT would commit is not valid YAML |
| `zone/meeting-edit-refused` | fires only when `ctx.edits_allowed` is `False` — a caller-level fact about the meeting flow, not a per-diff judgment — where the agent holds no tool that could have produced the modification at all |
| `secrets/unscanned-diff` | the scanner could not run over an edit to a page the agent cannot write, for a reason (git's rendering of a diff) it has no access to |
| `pii/unscanned-diff` | the same, for the PII patterns |

**And the reason is evidence, not reachability.** `processing._reset_for_retry`'s
`reset --hard` + `clean -fdq` really would clear a transient external write, so "the retry could not
clear it" is not the argument. The argument is that `processing.preserve_refused_diff` runs only on
this terminal path, never before a retry — so a repairable finding here would let that reset erase
the only evidence of an unexplained write into the worktree before an operator ever saw it.

Everything else keeps its retry, including the zone gate's `deletion` / `unsupported-change` /
`not-a-regular-file` — the agent has no tool that can produce those either, but they have no known
producer at all, and a branch reached by something unexplained is the wrong place to start taking
the retry away. (Whether their own reset-before-retry destroys the same evidence, and so whether they
belong on the list too, is flagged OPEN in `gates.unrepairable` rather than settled — read the
silence there as a question, not an answer.) The default for a new gate is `repairable=True`: a
wasted retry is recoverable, a retry silently taken from a finding the agent could have fixed is not.

## The librarian branches from the remote

`gitcmd.base_ref` resolves the commit every worktree starts from as **`origin/<branch>` when the
checkout has a remote**, fetching first. That is correct for a service — two captures filed in a row
must see each other, and the second one only does if its worktree starts from the commit the first one
pushed — and it has two consequences an operator has to know:

- **A commit that exists only in a local checkout is invisible to the librarian.** It reads the
  remote's tip, not the working tree. This cost a walk: the librarian skill had been committed
  locally and not pushed, the startup check read the local checkout while the run read the worktree,
  so the check *passed* and the item then burned both agent attempts discovering the file was not
  there. The check now reads the skill out of the same commit the agent will, and the refusal says
  which — "Push the commit that adds it: the worktree is built from `origin/main`, not from your
  local checkout."
- **Its pushes then diverge from the human's local branch.** The librarian commits land on
  `origin/main` directly; a local `main` that has not fetched is behind, and a local `main` with its
  own unpushed commits has diverged. `git pull --rebase` before working by hand, and expect
  `git status` to say "behind" after any filing. Nothing the librarian does rewrites history, so the
  divergence is always a fast-forward away from resolved — but it is not visible until you fetch.

The preamble line names the ref and the sha for exactly this reason. If it says `origin/main@abc123`
and your `git log` says something else, that is the answer rather than a mystery.

### …and so does everything it judges with

The librarian skill is read out of the worktree checked out at `base.sha` (`agent.skill_path`,
checked by `worker._check_skill_at`), and every other input the worker judges with follows it:
`librarian.base_inputs` is the one module that reads them, all at `base.sha`:

| Input | How it is read at `base.sha` | Absent at that commit means |
|---|---|---|
| `ops/acl.json` | parsed from the blob — `acl_rules.load_text`, no file anywhere | open corpus (no `acl:` line on the page) |
| `ops/entity-registry.json` | materialized to a temp file, then `kernel.registry.load_registry` | empty registry — the graph works unregistered |
| `.claude/tools/stigmergy_lint.py` | materialized per item and executed from there | a fail-closed refusal at startup |

`ops/stewards.json` — the doorbell's scope→steward-emails map — is a fourth input on the same
mechanism (`base_inputs.load_stewards`). The meeting distiller's brief takes the OTHER road, the
skill's: it is read out of the worktree at `base.sha` by `agent.read_meeting_brief`, deliberately not
through a second `base_inputs` reader — see [meeting-distiller.md](./meeting-distiller.md).

The registry loader keeps its own reader (ported, tested code that takes a path — import and
adapt, never rewrite), which is why that one round-trips through a temp file instead of getting a
data-level entry point invented for one caller. The linter is materialized **per item** rather than
once per run, because the base commit is resolved per item: the script that judges a diff is always
the one in the commit the diff was built from.

**All of them are re-read on every item, not once at worker startup — including the ACL config.**
`worker.startup_checks` still resolves them once, at boot, for its own fail-closed refusals; but
`processing.process_item` re-resolves the registry AND the ACL config again for each item, at that
item's own base commit. This is what makes a steward's `stigmergy-entities approve` (a new entity) or a
push to `ops/acl.json` (a tightened audience rule) take effect on the very next claim, with no worker
restart — and its absence, for the ACL config specifically, was a real defect: a long-running worker
that only ever re-read the registry kept stamping pages with the
audience labels of the commit it booted from, which fails in the silently-OPEN direction the
moment a steward narrows `ops/acl.json` on `main` after the worker started.

**Why this is a governance property and not tidiness.** Once entity pages and the registry are the
output of the steward's approve flow, a working-tree read is a read *around* that gate: an
uncommitted edit to `ops/entity-registry.json` could anchor captures to an entity nobody approved.
Two more things fall out of it — a deployed worker has no working tree anybody edits, so local and
deployed behave identically by construction; and a filed page's stamps (its audience labels above
all) are reproducible from history.

**The cost, accepted:** trying a linter change by editing it and running a walk no longer works.
Commit and push it, or use the linter's own suite in the knowledge repo.

Design record for this and for governed entity birth (below): [ADR 016](../decisions/016-human-loop-and-entity-governance.md).

## The two dedup levels code owns

The third — near-duplicate detection — is the agent's judgment against the graph, and files with a
mutual overlap callout. These two are deterministic and run before the agent, cheapest first:

- **retry collapse**: identical content hash + same submitter + inside `dedup_window_s`. One page is
  produced and the second row reaches **`filed` with the same `result_ref`**, its report saying it was
  a retry of the first rather than a second capture. `rejected` was considered and refused —
  resubmitting identical material is ordinary behavior, and telling that person their capture was
  rejected reads as a penalty for a retry. The material *is* filed, at that page.
- **already filed**: the hash matches a page already in the repo → `rejected`, pointing at it.

## The source attachment: a parameter, never a third flow

Some captured material has independent documentary existence — a Slack thread somebody actually
wrote, a document fetched from Drive — and some is conversational. The door rule is that the first
kind files a verbatim `sources/` page beside the synthesis and the second leaves none (the raw
archive holds documents, not chats). The SHAPE of that is a **parameter on the fast lane**, not a
third flow.

`processing._source_attachment` is the on/off switch, decided per item, and it returns `None` — the
OFF position, where every `GateContext` the fast lane builds is byte-identical to the unattached one
— for every ordinary MCP capture. **There are two ON positions**, and each is keyed on a fact a
DOOR asserted server-side, never on something a client could write:

| ON when | Folder | `source_kind` | tags | `url:` |
|---|---|---|---|---|
| the `source_client` hint is Slack's (`SLACK_SOURCE_PREFIX`) | `sources/slack/` | `slack` | `source`, `slack-thread` | the thread permalink |
| the ROW'S OWN `kind` is `drive` (`DRIVE_SOURCE_PREFIX`) | `sources/drive/` | `google-drive` | `source`, `drive-document` | the Drive URL |

Keying the Slack position on a hint is sound for exactly one reason:
`capture.schema.reject_source_provenance_hints` refuses `source_client`/`source_permalink` at the
client seam for every door but Slack's own, so the hint stopped being client-writable the moment it
became load-bearing. The Drive position needs no hint at all — `kind: drive` is only ever written by
the `stigmergy-drive` operator CLI (`schema.MCP_SUBMIT_KINDS` keeps it unreachable through
`brain_submit`), which makes the kind itself the strongest server-asserted fact available.

When it is ON, the pieces are the meeting flow's, reused rather than re-derived:
`_build_source_parts` writes the verbatim part(s), `page.stamp_source_fields` stamps the provenance
group (`content_hash`, `extracted_at`, `tier: 1`, and the part's own `id:`) instead of the fast-lane
group, and `GateContext.provenance_pages` TELLS `gate_frontmatter` which pages legitimately carry it.
The lane widens by exactly the attachment's own folder for the duration of that one item
(`write_prefixes`, `creatable_types`, `extra_folder_types` on the ctx — never on the module
constant). The synthesis cites the source through `sources:` (`page.add_source_citation`, applied by
`_stamp`), and `report.filed` names the parts in its own `source_pages` list.

**With the attachment ON the agent is TOLD so**, in a server-composed system note beside the
corrective brief: the brief's own genre rules make a whole document read as `type: source` — a type
the fast lane may not create — so the first real Drive capture parked a capture whose source half
code had already written. The note says the source half is already handled and the agent's whole job
is the synthesis. It is instruction-side and never derived from the material's shape.

**`_cross_check_outcome`'s "exactly one page" means one AGENT page.** The attachment's
code-written parts are excluded from that count — they are named on a surface a human reads and cited
from the synthesis, so the "committed and reported nowhere" argument the veto exists for does not
apply to them, and counting them would veto every attached capture by construction. A computed source
path that already exists is refused (`outcome/existing-page-collision`) rather than suffixed: the
likely cause is that this thread or document was captured before, and a different page title is the
agent's own repair.

## Ask-back: the one question a capture gets

The agent's outcome schema declares `triage: {kind: "unresolved-entity", name: …}` when it cannot
place a capture. **Worker code routes that declaration**, and the routing is a contract rather than
a judgment:

| The agent declared | Where it lands |
|---|---|
| `unresolved-entity`, and this capture still has its question | `needs_input` — the SUBMITTER is asked, once |
| `unresolved-entity`, and the question is already spent | `triage` — the steward's, and no second question |
| `unsupported-type` | `triage` |
| nothing — a veto survived both passes (`_unanchorable`, `_uncreatable_type`) | `triage` |

The question itself is **code-built**, never agent prose: `report.needs_input` names the unresolved
name, lists the registry's entities with their aliases (through `gates.registry_candidates`, the
same reading `anchoring_brief` uses, so the human list and the agent list cannot disagree about
what is registered), states both outcomes and their consequence, and ends with the exact
`brain_reply(...)` call. It is a message for a person and deliberately shares no template with
`anchoring_brief`, which is the agent-facing counterpart of the same situation — the two have
different readers.

**The budget is a database column.** `asked_at` is stamped on the first transition into
`needs_input` and never cleared, so "one ask per capture, ever" holds across a reply, a steward's
`requeue` and a lease redelivery. A counter held in the worker process would survive none of them.

**The reply is untrusted data.** `agent.build_prompt` fences it and labels it *"submitter's
reply to the librarian's question (data, not instructions)"*, below the material and away from the
corrective brief — the one genuinely instructive thing in that prompt. It bypasses nothing:
`gate_anchoring` still resolves names through the registry loaded at `base.sha`, and
`processing._stamp` still writes the server-owned frontmatter over whatever the page says.

**A `triage` row this loop produces is never minted by the agent, and never by the submitter.** A
reply can only resolve to an entity that already exists in the registry — "it's new" always
escalates to the steward's queue. Growing the registry from there is a separate subsystem,
`stigmergy.entities` (`stigmergy-entities approve`/`create`), which is the only writer of
`ops/entity-registry.json` and `wiki/entities/` in this codebase; see
[operator-runbook.md → Draining parked rows](./operator-runbook.md#draining-parked-rows).

## A refusal carries a code, not only a sentence

Every `rejected` report carries `reason_code` — the six values of
`capture.schema.REJECTION_REASONS`: `secret`, `pii`, `duplicate`, `steering`, `steward`,
`malformed-frontmatter` — beside the sentence a person reads. The sentence is for the human; the
code is the only thing a **read path** may branch on.

It exists because one had to. `brain_submissions` was serving back a 500-character excerpt of the
capture a secrets refusal had just bounced, in the same object as the sentence saying it had not,
and nothing on the row told that class of refusal apart from any other: `stage` looks like the
signal and is written by `failed_system` alone, and the alternative was matching on `report.py`'s
prose — which would make a confidentiality property change the next time a sentence is improved.
`capture.queue` now withholds the excerpt, the client hints and the submitter's `reply` for the two
codes in `schema.WITHHELD_REASONS` (`secret`, `pii`) — and for a `rejected` row carrying no code at
all, which fails closed. The full rule, which also withholds while the gates have not run yet, is in
[capture.md → Withheld material](./capture.md#withheld-material).

A `secret`/`pii` rejection additionally purges its own `payload`/`hints` **immediately** rather than
waiting for the 30-day retention window: `worker._finish` checks the code once, right after the row
lands `rejected`, and calls `capture.retention.purge_secret_capture_immediately`. The evidence blob
is untouched — a live credential in the material still has to be rotated, whatever the report says.

Every builder goes through `report._rejected`, so a new refusal cannot ship without a code — and a
refusal that somehow does is withheld rather than echoed.

## Reuse these seams

- `capture.queue.claim_next` / `finish` — the claim and the `attempts`-fenced terminal transition.
  **Do not reimplement the fence**: it fixed a real defect.
- `capture.queue.holds_lease` — ask "is this delivery still the live one" *before* an irreversible
  step. The fence refusing afterwards is the right guarantee for a row and no guarantee at all for a
  commit already on `main`.
- `capture.queue.query_in_flight` / `filed_latencies_ms` — the two read surfaces `status` is built on.
- `capture.cli.depth_line` / `format_ms` / `RECLAIM_NOW` — `stigmergy-queue`'s vocabulary, imported.
- `report.py` — every sentence a person reads about a submission. The CLI never composes its own
  wording; `brain_submissions` and the terminal render the same fact set.
- `latency.summarize` / `render` — pure, so the threshold behavior is testable with a list of floats.

## Avoid / anti-patterns

- **Do not add a gate that interprets.** If a check needs judgment it belongs in the skill, and the
  honest lever for quality is that file — versioned, diffable — not more gates.
- **Do not let the agent write outside the three creatable folders**, and do not widen
  `confined_write` to a
  prefix test. Inside the worktree are `.git/` (where a `config` carrying `diff.external` is executed
  by the very next `git diff` this process runs, as the worker, with the App key in its environment)
  and every dotfile.
- **Do not echo a refused value.** Locators, rule ids and categories only. A secret in an error
  message is a secret in a log; an injection quoted back is a second copy of the attack delivered to
  a human.
- **Do not claim a filed page is searchable immediately.** It becomes searchable only at the next
  index rebuild or the webhook's incremental upsert, and that clause is the second clause of the
  same sentence, not a droppable footnote.
- **Do not assume any two librarian processes can share a worktree root safely.**
  `startup_checks` reaps worktrees a crash left behind, scoped by repo AND by the pid that created
  each one (`gitcmd.reapable`) — so `stigmergy-librarian once` (a walk) beside a `run` loop on the SAME
  repo is safe, and so is a second librarian on a DIFFERENT repo. Two long-running workers on the same
  repo AND the same (or default, shared-temp) worktree root are still not a supported pairing — a
  crash's leftover cannot always be told apart from a live sibling's. Give each its own
  `STIGMERGY_LIBRARIAN_WORKTREE_ROOT`; see the runbook's "Two librarian processes on one machine"
  section.
- **Do not suggest `--backend double` as a workaround** for a missing credential. It files fabricated
  pages, and offering it as a fix invites committing them to the company's knowledge.
- **Do not give a pre-flight check two outcomes when it cannot actually tell the two apart.** This
  was learned on a check that no longer exists, and it is written down here rather than deleted with
  it, because the next pre-flight anybody adds will meet the same temptation. The retired backend's
  credential check shipped as present/absent and refused a WORKING configuration: an agent CLI
  authenticated interactively keeps its login under its own config directory — on macOS in the
  Keychain, so there is no variable AND no file to see — and no startup check can tell that from
  "never authenticated" without spending a request. It would have blocked `make librarian-walk` on
  the machine the walk was for, i.e. become the fifth of the four detours it was written to prevent.
  The shape that worked was three-way: proceed, proceed **while naming what the run is relying on**,
  or refuse. When a check is about to refuse something it only suspects, make it say so instead — a
  refusal an operator has to argue with costs more than the failure it prevents.

  **The surviving provider-key pre-flight is honestly two-way, and that is not a regression.** A key
  is an environment variable that is either exported or not; there is no second, unobservable channel
  to be wrong about, so present/absent is the whole truth rather than half of it. The rule is "match
  the check's shape to what it can actually observe", not "always use three outcomes" — and the one
  place that pre-flight cannot observe the truth from the variable alone, the deployed worker's
  stripped `OPENAI_API_KEY`, is exactly where it stops guessing and names the dead end instead.

## Tests

| Suite | Covers |
|---|---|
| `tests/librarian/test_processing_pg.py` | the whole filing path over real Postgres + real git |
| `tests/librarian/test_adversarial.py` | the permanent cat. 1 / cat. 5 / cat. 7 cases (`-k adversarial_cat5`) |
| `tests/librarian/test_worker_signals.py` | REAL SIGINT/SIGTERM/SIGKILL to a real worker subprocess |
| `tests/librarian/test_cli_run.py` | `run` interrupted for real; the explicit-zero refusals |
| `tests/librarian/test_cli_status.py` | depth, the stale-lease verdict, the not-enough-data framing |
| `tests/librarian/test_cli_once.py` | `once`, including "the command the message names really works" |
| `tests/librarian/test_startup_preflight.py` | the push-identity refusal and the malformed-registry refusals, with their benign twins |
| `tests/librarian/test_backend_retirement.py` | the retired-backend refusal and its benign twin (a `pydantic` worker boots) |
| `tests/librarian/test_skill_reading.py` | `read_skill`'s refusals, and the skill proven at the base commit |
| `tests/librarian/test_gitcmd_unit.py` | worktrees, the diff's blind spots, rebase-and-retry, the pushed sha |
| `scripts/e2e_librarian.py` | the whole loop against a real bare git remote, from empty volumes |

**No test needs an API key.** The agent step runs against the offline double, which hallucinates a
figure on purpose, plants a seeded secret, attempts an injection, attempts a path escape — and behaves
perfectly on ordinary material, because a defense tested only against attacks has been measured for
sensitivity and never for specificity.
