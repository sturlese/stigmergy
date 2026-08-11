# ADR 033 — the structured filing flow: a gatherer, an account that carries the page, and code that writes it

Status: accepted; **D6's retirement gate is SPENT and the `sdk` backend is gone** (see
[Amendment — the gate is spent](#amendment--the-gate-is-spent-the-sdk-ordinary-path-is-retired)).
Narrative:
[`docs/reference/librarian.md`](../reference/librarian.md) (the two shapes of the ordinary flow and
their configuration), [`docs/reference/capture.md`](../reference/capture.md) (what a submitter's
capture meets on the way in). Code maps:
[`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md),
[`evals/index.md`](../../evals/index.md). Predecessors:
[ADR 015](./015-librarian.md) (agent judges, code vetoes; the agent's write lane),
[ADR 020](./020-meeting-distiller.md) (the meeting flow as a sibling entry point — structured,
tool-less, code writes every page: the shape this ADR copies),
[ADR 026](./026-the-purge.md) D2 (the trust layer's removal, and why the skill is the only briefing),
[ADR 032](./032-filing-port-and-pricing-seam.md) (the port, the pricing seam, and the expand–contract
plan this is the second step of).

## Context

After ADR 032 the ordinary capture flow was **the last place the platform depended on the Claude
Code harness**. The meeting flow had already been lifted onto a second provider without anything
about it being rewritten, because ADR 020 had happened to build it in the portable shape: the agent
holds no page-writing tool, it explores nothing, and its whole answer is one structured account.
The ordinary flow was the opposite of all three — the agent explored the checkout with
`Read`/`Glob`/`Grep`, wrote the page itself with `Write`, and was confined by SDK permission hooks.

That exploration is what made a provider swap a rewrite rather than a configuration change. Every
other backend would have needed its own tool loop, its own permission mechanism and its own
confinement rule, and the confinement rule is the one thing in this package that has already been
wrong in three separate ways (`agent.confined_write`'s own docstring carries the history).

**And at corpus scale the exploration is not obviously the better design either.** An agent
grepping for "what does this brain already know about Northwind" spends turns and tokens answering
a question code can answer exactly, from the same files, in one pass — and answers it worse: a
model's search is shaped by what it thought to look for, where a deterministic pass over the
checkout returns the same candidates every time whatever the model was thinking. The cost axis and
the quality axis pointed the same way, which is rare enough to act on.

The date behind ADR 032 has not moved: Sonnet's introductory pricing ends 2026-08-31, and the
librarian is the system's only writer.

## Decisions

**D1 — a deterministic GATHERER builds the context, and it reads the CHECKOUT rather than the
index.** `librarian/gather.py` is a pure function of `(worktree, registry, material)` plus two
bounds: the entities the material names (resolved through the registry's own alias map, never a
second matching rule), the top-K existing pages it lexically overlaps with, each with a bounded
excerpt and its own outbound links, the link neighbourhood one hop out from those, and the repo's
whole wikilink vocabulary. No database, no clock, no model — so it is unit-testable without a key,
and two gathers of one capture are equal objects, which is what makes two golden runs comparable.

**The reader moved; the data ORIGIN did not — and that is TRUE BECAUSE OF one filter, not by
default.** The worktree is the knowledge repo at this item's base commit, which is exactly what the
exploring agent's own `Read`/`Glob` reached, so this is the same data arriving by a shorter road.
But the agent's reads were confined by a permission hook that resolved `realpath` first
(`agent.confine_reads` → `page.is_inside`), and `corpus.load_pages` is the INDEX's parser with no
such notion: it walks `rglob("*.md")` and reads whatever it finds. So `gather._confined` drops every
row that is a symlink or does not resolve inside the worktree, before anything else looks at one.
Without it the structured shape would read strictly MORE of the filesystem than the shape it
replaces — a regression wearing a refactor's clothes, and the claim in this paragraph would have
been false. The same filter is applied to the wikilink-vocabulary walk (`edits.page_names(...,
confined=True)`). It is fixed in the librarian and never in `corpus.py`: that module belongs to the
index, whose callers walk checkouts they cloned themselves, and pushing one package's threat model
into another's default is how a rule stops being read where it matters.

Reading `pages_index` instead was considered and refused: it would put a WRITE-path worker on the
read path's ACL-governed table, and `server.acl.visible()` is the one place read access is decided
(`tests/test_architecture.py` enforces that every reader of `pages_index` names an ACL predicate or
is a listed exception). The librarian would have needed such an exception for a question it can
answer without one. The reach it DOES make — `stigmergy.index.corpus`, a pure repo parser with no
database connection and no ACL surface — is the same library edge `stigmergy.views` and
`librarian.edits` already declare, and is recorded in both code maps. **No semantic-similarity
gathering, either** — no embeddings, no `pages_index` search. If the golden ever shows the lexical
gatherer missing anchors at corpus scale, reopening this needs a design for the ACL question, not a
patch.

Its two bounds are configuration (`gather_top_k`, `gather_excerpt_lines`, and their environment
variables) rather than constants: they trade prompt cost against recall, and the number that is
right for a 40-page brain is not the one that is right for a 4,000-page one.

**D2 — the outcome envelope GROWS, additively, and which half is required is keyed on the backend.**
`agent.Outcome` gains an optional frozen `page` sub-object — `{title, page_type, body}` — carrying
the page's own text. The old shape (no `page`; the agent wrote the file and declared `page_path`) is
untouched and is exactly what the `sdk` backend keeps producing; `parse_outcome` accepts both, and
`title`/`page_type` stay SINGLE fields whichever half declared them, so `_commit_message`, `_stamp`,
`gate_zone` and the cross-checks keep reading one field rather than learning about two declaration
sites.

**There is no path in the new half and there never will be.** The folder is derived from
`page_type` through `page.FOLDER_BY_TYPE` — the one placement table every other placement question
reads — so a structured account cannot name a location at all.

Which half is REQUIRED is not the shared BOUNDARY's question, because `parse_outcome` judges both
channels and cannot know which backend ran. A backend DECLARES it:
`filing_port.FilingAgent.structured_ordinary`, a class attribute, read by
`processing._one_pass`. A type test (`isinstance(agent, PydanticFilingAgent)`) was refused for the
reason this repo refuses every inferred fact — a fourth backend, or a double standing in for one,
would then take the wrong branch by being the wrong class rather than by declaring the wrong thing.

**A backend's OWN output schema is the one place that does know, and the first paid run proved it
has to say so.** `pydantic_backend.FilingAccount` shipped with a default on every field, `decision`
included, reasoning that an omission should reach the boundary and be refused on its own terms
rather than raise inside the framework. That had the mechanism backwards: a default does not make
an omission visible, it makes it invisible. The framework's output validation accepted a half-empty
account, so its own `OUTPUT_RETRIES` never fired, and `parse_outcome` refused downstream —
`unknown-decision` four times and a missing `title` once, five of the golden's ordinary captures
dead in two passes each, with the WORKER's single corrective retry spent re-asking a model to
repair a shape a brief cannot reliably teach. The schema now requires what the boundary requires
(`decision` enum-derived from `agent.DECISIONS`; a `model_validator` demanding the fields THIS
decision obliges, with the repair instruction as the error message the framework hands back), so
the cheap road runs first and the expensive one is kept for real problems. The boundary keeps every
check regardless: it judges the file channel too, and a typed provider response is not a trusted
one. Two enforcement points, declared. The meeting schema got the same treatment on the same
mechanism before it fired there.

**Nothing about this was visible offline, and that is the finding underneath the finding.** Every
structured test drove `TestModel(custom_output_args=…)` with a hand-built COMPLETE account, so the
suite could only ever exercise the shape a model was assumed to return. The golden caught it on the
first paid run, which is the instrument doing exactly its job — and the durable lesson is that an
injected offline model proves the pipeline, never the schema's tolerance for what a real one
actually emits.

**One bound in the new half does not behave like its neighbours, deliberately.** `page.body` is
REFUSED over `MAX_PAGE_BODY_LEN`, never truncated. Prose truncates because nothing downstream
re-reads it — a clipped `summary` still says what it said. A page body IS the product: cutting it
would commit a page that stops mid-sentence, pass every gate (a truncated page is still
well-formed), and stay that way in the repo forever, with the only evidence in a log line. The
refusal is correctable and the agent gets its one corrective pass.

**D3 — CODE writes the page, and confinement gets STRONGER rather than weaker.**
`processing._write_ordinary_page` builds and writes the one page, on `_write_meeting_pages`'
discipline: the filename from the title (validated by `page.unnameable_reason`, collision-checked
through `page.path_key` — never `==`), the folder from the type, the frontmatter from the account,
the H1 from the title, `related:` from `links_created`. From that line down, `_one_pass` is unaware
of which shape produced the page: the stamp, all eight gates and `_cross_check_outcome`'s "exactly
one new page" rule run unchanged.

The confinement claim is the part worth stating plainly, because "we removed the permission hooks"
reads like a loss and is not. On the exploring path a hostile write is stopped by an allow-list
inside a `PreToolUse` hook — a defence that has to be correct about paths on a case- and
normalization-folding filesystem, and that has been wrong three times. On the structured path
**there is no write to stop**: the model holds no tool, and the account has no field that could
name a location. A hostile account can ask for a governed page TYPE, and that is refused before
anything is written and parked with the steward; it can ask for an unnameable title, and that is
refused; it can claim a `page_path` it did not write, and `_cross_check_outcome` refuses that too.
Every one of those is an existing mechanism, and the claim is exercised rather than asserted.

**D4 — the brief becomes backend-NEUTRAL, and the SDK carries the override.** The knowledge repo's
`.claude/skills/librarian/SKILL.md` is rewritten with no tool mechanics in it: it describes a worker
that hands the agent its context in one message and writes the page from one structured account, and
documents the outcome contract field by field for both halves. Every JUDGMENT rule survives —
placement, the three anchoring outcomes, the wikilink rule, overlap-versus-duplicate, the injection
posture, the one ask.

The mechanics live where they are true, which is the platform side: `agent.build_filing_header`
composes the preamble from a shared opening, a shared "nothing in this repo configures you" point
and a per-backend ENVIRONMENT paragraph — `build_meeting_header`'s arrangement, one entry point
over. The extraction is byte-preserving (`build_filing_header(ORDINARY_SDK_ENVIRONMENT)` reproduces
the pre-ADR-033 header exactly), and the SDK additionally carries a NAMED override note saying that
this run is handed no gathered context, holds five tools, and writes its own page.

**The direction of that note is the milestone.** In ADR 032 the brief was the tool-holding text and
the structured backend carried the correction; here the brief is the structured text and the SDK
carries it. The brief now describes the shape both future backends share and the shape it will still
be right about when the SDK path retires.

The brief and the gates are a two-sided contract, like the meeting one:
`tests/librarian/test_librarian_brief_contract.py` greps a rule table in both directions against a
frozen copy that ships with the suite, and the knowledge-repo PR lands with the platform PR.

**D5 — `backend=pydantic` serves a worker, and the M1 refusal dies with the limitation it
described.** `worker.startup_checks` refused that backend outright — correctly, for M1: a worker's
queue carries ordinary captures too, and a backend that serves one `kind` burns deliveries one row
at a time while looking configured. It serves both flows now, so the refusal has nothing to refuse
and the `meeting_only` escape that softened it for the eval rig has nothing to soften. What remains
are the checks that were always about the BACKEND — a provider-prefixed model id, a configured
price, the provider's own key — plus one addition: the skill is now proven at the base commit for
`sdk` AND `pydantic` (`agent.SKILL_READING_BACKENDS`), because both inject it.

**D6 — expand still, and M3 is the contract step with a named gate.** Nothing is retired here
either. Both ordinary shapes are alive, `double` is still the default and the suite's, `fly.toml` is
untouched, and the golden runs both. **The retirement gate for the `sdk` ordinary path is evidence
plus an explicit decision, in that order**: the full M0 golden scored on the structured flow and on
the SDK flow, on the SAME model, within the M0 bars, with the per-item cost of each recorded — and
then a human saying so. Not "the structured one exists", and not a date.

## Consequences

- The ordinary flow has TWO shapes behind one entry point, and `processing._one_pass` branches on
  one declared boolean. Every line below that branch is shared, which is the property that keeps the
  two from drifting into two flows.
- The gatherer is a new cost the exploring path does not pay: one `corpus.load_pages` walk of the
  checkout per agent pass plus one tokenization of every page, re-run on the corrective pass because
  `_reset_for_retry` puts the worktree back and a second pass judging a context it can no longer see
  would be judging something else. It is the only per-item cost that scales with the SIZE OF THE
  KNOWLEDGE REPO rather than with one capture, and it is assumed to fit inside
  `config.VISIBILITY_HEADROOM_S` — an assumption `minimum_visibility_timeout_s`'s docstring records,
  with the corpus scale at which to re-measure it, because a lease that is too short files a capture
  twice.
- **Code's own write got a guard the agent's write always had.** `processing._write_new` — THE write
  for all three page-building flows — now resolves the path before writing (`page.is_inside`, so a
  symlinked directory component is refused where `O_NOFOLLOW` only ever sees the leaf) and turns
  `OSError` into a named `WorktreeError` stage. Taking the tool away moved the write from a model to
  code; it did not make the write safe, and the confinement argument in D3 is only true because the
  code that replaced the tool is bounded the same way.
- **The prompt block is bounded as a whole** (`agent.MAX_GATHERED_CHARS`), not only field by field:
  `gather_top_k`, `gather_excerpt_lines` and the per-line clamp multiply, and two of the three are an
  operator's to set — a per-item prompt whose size is three configuration values multiplied together
  is a bill nobody predicted. Over the ceiling the lowest-ranked candidates are dropped whole, never
  a JSON value cut in half, and the block states the trim: a model told "these are the candidates"
  about a silently shortened list is being misled about its own context.
- On the structured path an agent can only link what it was handed. That is a real narrowing
  against `Glob`, and it is the intended trade: the vocabulary it is handed is the same set
  `edits.validate` answers "does this link resolve" with, so a link it makes from that list cannot
  be a dead one. `link_names_total` tells it when the list is a prefix rather than the whole graph.
- The page's filename is its TITLE on both shapes, and the structured writer does NOT slugify.
  A wikilink resolves by bare page name, so the filename is the name every other page must spell;
  filing `refund-policy-v2.md` beside a corpus of Title Case pages would break every
  `[[Refund Policy v2]]` a human — or the other backend, for the same capture — writes. The meeting
  flow slugifies because its own filenames are slugs; this flow is not that flow.
- `PydanticMeetingAgent` is now `PydanticFilingAgent`, with the old name kept as an alias until its
  callers migrate. The name had become a lie and the rename could not land in the same commit as
  the behaviour change without breaking four test modules at once.
- The knowledge repo carries a brief that no longer describes the tools an SDK run holds. That is
  only safe because the platform says so explicitly, in a named override immediately in front of
  the brief — the same mechanism, and the same positioning argument, ADR 032 introduced.

## Amendment — the gate is spent: the `sdk` ordinary path is retired

**D6 said the retirement gate was evidence plus an explicit decision, in that order, and not a
date.** Both were spent, in that order, and this amendment records what the evidence actually was
so a reader years from now can judge the decision rather than take it on trust:

- the full M0 golden scored on the STRUCTURED flow with every bar PASS;
- a 20-capture staging shakedown on the structured flow with zero flow failures;
- the container e2e running on CI for every push, on the deployed image, against the double;
- and then a human saying so. Nothing here fired because the second shape merely existed.

**No ADR 034, and the reason is the point.** A second record is owed when the thing that happened
diverged from the thing that was planned. Nothing diverged: D6 named the gate, the gate was met on
its own terms, and contracting is the step D6 said would follow. The one thing this milestone added
that D6 did not spell out — a NAMED startup refusal for the retired value — is what "contract"
means when the configuration outlives the code, not a different decision. Amending the record that
made the promise keeps the promise and its discharge in one place; a new ADR would split them.

### What went, and what the retirement cost

`SdkAgent`, both `_run` methods, the option builders, the three `PreToolUse`/`PostToolUse` hooks,
the tool allow/deny lists, the environment allow-list and the credential pre-flight; the
`claude-agent-sdk` dependency; the Node runtime and the agent CLI in the image, with their entries
in `scripts/docker/tool-checksums.txt`. The image is roughly **55% smaller**.

Three things went that were NOT replaced, and pretending otherwise would be the dishonest version
of this paragraph:

- **the tool-call ceiling** (`settings.max_tool_calls`) counted tool calls in a hook. A structured
  call holds no tool, so it bounds nothing — the variable is deprecated rather than silently
  ignored, and removed in a later release with its own consumer inventory. `max_turns` goes the
  same way. The WALL CLOCK survives, in the backend that still needs one.
- **the harness lockdown** — `setting_sources=[]`, `mcp_servers={}`, `strict_mcp_config=True`, and
  the subprocess environment allow-list — hardened a process that no longer exists. What replaces
  it is not a thinner guard but the absence of the surface: no subprocess, no settings file, no
  `.mcp.json` to be read. The DEFECT those settings were written for (repo content becoming
  executable configuration) is recorded in `agent.py`'s docstring, where the next harness will
  meet it.
- **the write-confinement HOOK.** The rule it enforced (`agent.confined_write`) is untouched and
  still runs on every offline filing, because the double routes its own writes through it.

### The two-edit upgrade, and why the refusal names it

A deployment's `STIGMERGY_LIBRARIAN_BACKEND` lives in `fly.toml` or a gitignored `.env`, and a `git
pull` updates neither. So the first worker to boot on the new image is configured for a backend
that is not there, and telling it "invalid backend" would name a typo nobody made.
`agent.RETIRED_BACKENDS` refuses it by name instead: what happened, that the replacement takes TWO
edits (the backend AND a provider-prefixed model id — changing only the first swaps this refusal
for the model one), and the image rollback that gets a worker running meanwhile. The queue is
durable, so nothing is lost while it is down.

`config.DEFAULT_MODEL` moved from the bare `claude-sonnet-5` to `anthropic:claude-sonnet-5` for the
same reason and it is not a model change: the surviving backend resolves ids through pydantic-ai,
where a bare name means an OpenAI model, so the shipped default would otherwise be the one value a
worker could not boot on without overriding.

### What the knowledge repo owed, and where it landed

This retirement had a half the platform could not make. The brief is the knowledge repo's text, and
its environment note said *"Some runs of this skill hold tools and a checkout, and write the page
themselves"* — true while two backends existed, false of every run once one did. The platform reads
that file and may not reword it; only the knowledge repo can.

**It landed.** Knowledge-repo commit `c1e0996ed497e70a9df82661c367294b48207a16` —
*"chore(skills): the brief describes one run style"* — rewrote that paragraph so the environment
note describes ONE run style instead of offering a variation that no longer exists. Everything else
in the brief is untouched: every judgment rule, the anchoring outcomes, the wikilink rule, the
injection posture, the one ask.

The two frozen copies in this repo are resynced to those bytes, and the pin is the check:

```
git show c1e0996:.claude/skills/librarian/SKILL.md | shasum -a 256
1a05db240cbfb7207c353534b0146eff96af2a2d6ddf700e89b5cb79f4ce6635
```

That sha256 is what `tests/librarian/fixtures/repo/` (the drift guard) and `evals/filing/repo/` (the
yardstick) now carry, so a brief that drifts from the landed one fails here rather than on a
deployment. `evals/README.md` records what the re-freeze means for the score series — the bars were
fixed under the previous bytes and the next golden row is the first measured under these.
