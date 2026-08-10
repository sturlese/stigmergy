# The MCP server — `stigmergy.server`

The single API over the brain: one MCP server that enforces the page contract
**and** access control server-side, over TWO transports — stdio (local clients) and
streamable HTTP with per-user auth (remote, multi-user). Design record:
[ADR 007](../decisions/007-answer-layer.md),
[ADR 013](../decisions/013-http-transport-and-token-auth.md) (HTTP transport + token auth +
staging deploy); the index it reads is [ADR 012](../decisions/012-hybrid-index.md).

`service.py` holds the transport-agnostic core and `mcp_server.py` the tool closures both
transports share; everything that touches Postgres goes through `stigmergy.index`. The answering loop
(`ask`) and its verifier are served here but live in `stigmergy.answer` (narrative:
[answer.md](./answer.md)). Google OAuth remains the target for real per-caller identity — bearer
tokens are what stands in for it today.

**The server also writes.** `brain_submit` and `brain_submissions` mount the durable
capture queue (`stigmergy.capture`, narrative: [capture.md](./capture.md), decided in
[ADR 014](../decisions/014-capture-queue-and-attribution.md)) on the same service seam as the read
tools. Submitting still never touches git and still files no page — a capture is queued and
attributed here, and the librarian is what turns it into a page. The one exception on the whole
server is the review lane's own governed door: approving an `entity-proposal`
(`review_decide`, [ADR 030](../decisions/030-server-side-entity-minting.md)) mints through it,
below.

## Module map

| Module | Does |
|---|---|
| `settings.py` | explicit runtime config (`from_args`); no env reads at import |
| `identity.py` | the one identity → audiences resolver over `ops/identities.json` (stdio, startup) **and** the per-request half: bearer token → sha256 → the token store → email, fail-closed on every step |
| `acl.py` | the one visibility rule (`visible`) every read path filters through |
| `review.py` | `review_queue`/`review_decide` over the **two** kinds a human decides (`entity-proposal`, `parked-capture` — the strings live in `stigmergy.review_kinds`, below both this package and `stigmergy.slack`); the doorbell's read side (`items_for_doorbell`, `load_stewards`, `record_undeliverable`). `reject` and every `parked-capture` verdict never write to git; approving an `entity-proposal` mints through the governed door instead ([ADR 030](../decisions/030-server-side-entity-minting.md)) — `entities.remote.mint_via_clone`, the same `entities.mint.mint` `stigmergy-entities approve`/`create` call, ONE commit, authored as the librarian App with an `Approved-by:` trailer |
| `service.py` | `BrainService` — the transport-agnostic core; `build_service`/`open_scoped_resources` wire it fail-closed. Also the rate-limit + audit wrapper (`_call`/`call_async`) both transports share; `list_entities`/`describe_entity` (the entity-navigation door) and entity-first resolution INSIDE `_search` itself, so every client gets it. `require_embedder` is the keyless seam: a server started without an embedding key still starts and still serves `read_page`/the capture tools, and the tools that cannot work say which capability is missing (`CapabilityUnavailableError`) instead of failing opaquely. `_search` also hands the resolved entity id DOWN as `entity_hint` — the rank-time boost is TOLD, never re-inferred from query tokens |
| `ratelimit.py` | `RateLimiter`, a per-identity token bucket, injectable clock. **TWO live buckets**: `overall` (30/min, spent by every wrapped call) and an additional `ask` (10/min). A third constructor knob, `propose_per_min`, is accepted and stored but nothing spends it — `_extra` registers `"ask"` alone. It survives as the shape for the next expensive write tool, not as a live limit |
| `audit.py` | `audit_log` DDL + `AuditWriter` — one row per tool call, both transports; a write failure is logged loudly and never fails the serving call. `result` (nullable JSONB) is a per-tool outcome SUMMARY (`{"hits": n}` for `search_brain`, `{refused, suppressed, verdict, first_verdict, citations, retried}` for `ask` via
`answer.service.audit_summary` — `first_verdict` is the FIRST draft's verdict, the only field that
says what a corrective retry was for) — never a question or an answer, by construction |
| `mcp_server.py` | the FastMCP tool closures (shared by BOTH transports) + the `stigmergy-server` console entry point (`--transport stdio\|http`) |
| `errors.py` | the domain exceptions, all under `StigmergyServerError`: `IdentityError`, `StartupError`, `RateLimitError`, `CapabilityUnavailableError` and `RegistryError` — library code never raises `SystemExit`; `mcp_server.main` maps these to a clean stderr line + exit code (stdio) or the transport's own error body (HTTP). `RegistryError` is load-bearing rather than incidental: the registry loader's own `ValueError` carries the path it failed on, and `search_brain` echoes a `ValueError` verbatim, so the conversion is what keeps a server filesystem path out of a tool result |
| `transport_http.py` | the streamable-HTTP transport — the bearer-auth ASGI middleware, per-request identity resolution, and the one shared FastMCP app (`stateless_http=True`, mandatory — see "HTTP transport" below) every identity serves through. It mounts `webhook.py`'s route via FastMCP's `custom_route` and exempts its EXACT path from the bearer middleware, and it composes the admin console ([admin-console.md](./admin-console.md)) in FRONT as an ASGI branch — `/admin*` never reaches the bearer middleware at all, which is what keeps the webhook exemption meaning exactly one path |
| `issue_token.py` | `stigmergy-issue-token <email>` — the operator token-issuance CLI |
| `webhook.py` | `POST /webhook/github` — HMAC-verified incremental index upsert on merge; the one declared, narrow exception to "the server never imports the librarian" (`githubapp`'s App-credential primitives, reused rather than reimplemented). It reuses `corpus.page_row` and `store.upsert_pages`/`delete_pages` rather than growing a second writer, and re-resolves outbound `links` against the index's own existing paths. `_propagate_split_chain_supersession` knows both part-id conventions the codebase has written (`-p<n>` and the historical `#p<n>`) and only ever propagates from a chain PRIMARY to parts sitting in that primary's own directory, so an id-less `-p2`-stemmed twin in another folder never inherits. Failure here never breaks the write path: the page is already committed to git, and the nightly rebuild reconciles regardless |
| `entity_aliases.py` | reads `ops/entity-registry.json` as a plain file contract of its own (never imports `stigmergy.kernel.registry`'s reader/writer — a deliberate second parser, not a shared one) and resolves a registered alias inside a free-text query (`load_aliases`/`resolve_entity`), used by entity-first search at the SERVICE layer. `load_registry` (full `{id,name,type,aliases}` records) and `resolve_exact` (input names one entity, not a substring search) are `list_entities`/`describe_entity`'s resolution |
| `pilot_report.py` | `stigmergy-pilot-report` — questions/identity/week, answered-with-citation vs honest refusal, capture→filed and capture→searchable latency, from real `audit_log`/`capture_queue` rows. Reads only |

The server may import `stigmergy.index` and `stigmergy.capture` freely. It may **not** import
`stigmergy.librarian` — they talk through the queue, a durable row, never an import — with exactly
**two** module-scoped exceptions, each a named symbol list rather than a general license and each
mechanically pinned by `tests/test_architecture.py`:

- `webhook.py` → `librarian.githubapp` (the App-credential primitives) and
  `librarian.errors.LibrarianConfigError`;
- `review.py` → `librarian.gitcmd` (read `ops/stewards.json` at the base commit),
  `librarian.base_inputs` (parse it) and `librarian.gates` (scan a steward's free-text note for
  secrets).

The tests assert both directions: an import outside the list fails, and so does a DECLARED symbol
nothing actually imports, so a stale exception cannot sit there widening the door. Importing the
librarian's async queue-drain loop (`worker`/`processing`/`agent`) is refused independently of
either list — a slow agent run must never happen inside an HTTP request. `review.py` additionally
reaches a small declared slice of `stigmergy.entities`/`stigmergy.kernel` for the entity registry and
the entity-id generator; `stigmergy.kernel` is a library every package may depend on. The capture edge
is one-way: `stigmergy.capture` never imports `stigmergy.server`, so the queue has no opinion about
identity, rate limits or transports — those are resolved here and passed down.

## The four read tools (MCP)

| Tool | What it does |
|---|---|
| `search_brain(query, filters?, max_results?, include_superseded?)` | contract-ranked hits: superseded pages demoted, entity/period/freshness boosted. Each hit carries `factors`, `score`, `arms`, snippet and contract flags; the response carries the index `built_at` and embedding model. `filters` scopes by frontmatter column — the seven `search.FILTER_COLUMNS` (`zone`, `type`, `status`, `entity`, `owner`, `tier`, `as_of`); an unknown name is a clean error. **The tool's own docstring names `search.FILTER_COLUMNS` as the source of that list**, so it cannot advertise a filter the index does not carry: it once offered a `period` filter that was not a column at all ([index.md](./index.md#queryable-filters-vs-stored-columns)), and every client that took the offer got the unknown-name error. Superseded pages are demoted (kept) unless `include_superseded=false`. `max_results` is clamped to `[1, 80]` (2× `rank.CANDIDATE_POOL`) — a negative, zero or oversized value never slices the ranked set open, and it counts **documents**, not rows: a split document's parts collapse to one top-k slot. When `filters` names no explicit `entity`, the query is resolved against the registry and, on a match, the resolved id is told to the ranker — one blended search in which that entity's material scores higher. It is deliberately not SCOPED to it: scoping made a company-wide page unreachable through every query naming a registered company (ADR 022 D4, amended) |
| `read_page(path)` | one page, trust signals first (title, entity, as_of, superseded banner), body fenced as `UNTRUSTED-DATA`. An unknown **or** out-of-scope path returns the same `unknown page` response — existence never leaks. It also returns `type`, `status`, `supersedes`, and the navigation graph — `links`/`backlinks`, each `{path, title}`, existence-scoped, capped at 20 with the truncation stated in `links_note`/`backlinks_note` |
| `list_entities()` | the ACL-scoped entity vocabulary: every id you may see, enriched from the registry (`name`/`aliases`/`type`) where a registry record exists; an anchored-but-unregistered id serves as its bare id alone. `count` states how many |
| `describe_entity(entity)` | everything anchored to one entity, layered: registry metadata + its own page, a view reference (or `null`), and a dated timeline of every other anchored page. `entity` resolves through **two** paths, not one (the ADR 022 D5 amendment): a registered id/name/alias through the same registry loader `list_entities` and entity-first search use, **or** exact verbatim membership of the caller's own scoped-id set — so an anchored-but-unregistered id, which `list_entities` already serves honestly as a bare id, resolves here too. The second path is never normalized: a scoped id is an index fact, not free text a person typed, so fuzzing the comparison could only manufacture a false match. Both consult `scoped_entities()`, the ONE existence rule — computed unconditionally BEFORE resolution, which is what closes a timing oracle (a never-registered input skipping the DB read a registered-but-out-of-scope one pays for, so the latency itself says which case applies). Unknown and out-of-scope entities return the byte-identical `{"error": "unknown entity: <input>"}` |

Output is structured JSON. All four enforce the client's ACL scope: out-of-scope pages are
absent from search, unreadable, and their discovery hints do not resolve. ACL
filtering runs in Python, so `search_brain` rank/fetches a generous candidate
pool **before** filtering, then truncates to the requested count — an out-of-scope row must never
steal a slot from a visible one; `read_page`'s links/backlinks and `describe_entity`'s timeline
follow the identical filter-then-cap order. Full mechanism: [navigation.md](./navigation.md),
decided in [ADR 022](../decisions/022-entity-navigation.md).

## The capture tools (the write path)

| Tool | What it does |
|---|---|
| `brain_submit(kind, material, hints?)` | queue a capture (`kind`: `raw` or `page`), archived to the evidence plane and attributed by the SERVER. Returns an ack with the submission id; it promises **queued and attributed**, never "saved to the brain" — nothing is in the brain until the librarian files it |
| `brain_submissions(limit?, status?)` | the caller's own submissions with state, timestamps, `result_ref` and any open `question` (plus a `reply_hint` naming the exact `brain_reply` call, once `needs_input`); an unrestricted (steward) identity sees the whole queue with `mine` marking its own rows. Echoed capture text is fenced as `UNTRUSTED-DATA` |
| `brain_reply(submission_id, answer)` | answer the librarian's one question about a `needs_input` capture. Only the original submitter, or an unrestricted (steward) identity replying on their behalf, may answer; every other case — a nonexistent id, somebody else's row, somebody else's row that isn't even `needs_input` — gets one identical, generic refusal (no existence leak). A caller who IS authorized gets a specific message if the row simply isn't waiting on a reply. The answer is bounded at 2000 characters, recorded on the row, traced with the actor, and the row returns to `queued` |

All three ride the same `BrainService._call` wrapper as the read tools, so rate limiting, the audit
row and the error shaping apply unchanged — there is no second write path. `submitted_by` is the
resolved caller identity (the `--identity` name over stdio, the token's email over HTTP) and is
never an argument. `brain_submit` nevertheless DECLARES four server-owned fields —
`submitted_by`, `verification`, `acl`, `content_hash` — for exactly one reason: FastMCP builds its
argument model with pydantic's `extra="ignore"`, so an undeclared field is dropped SILENTLY by the
SDK, and declaring them is what turns passing one into an **explicit error** with no row and no
blob created. Fields NOT on that signature are still structurally safe (nothing anywhere reads
client input into a server-computed column) but are dropped rather than refused — a residual
[capture.md](./capture.md) records rather than papers over. `kind` is restricted here to
`raw`/`page` (`capture_schema.MCP_SUBMIT_KINDS`) and NOT to the queue's full `KINDS`: growing that
tuple for an operator CLI must never silently widen what a model-chosen MCP argument may be.
`brain_reply`'s audit row records the answer's size and hash, never its text (same posture as
`brain_submit`'s own material). Full mechanism, the queue's state machine, the ask-back loop and the
evidence key scheme: [capture.md](./capture.md), decided in
[ADR 014](../decisions/014-capture-queue-and-attribution.md) and
[ADR 016](../decisions/016-human-loop-and-entity-governance.md).

## The review tools

`review_queue`/`review_decide` cover the **two** kinds a human genuinely decides.
`stigmergy.review_kinds.ITEM_KINDS` is the one definition, and it names two:

| Tool | What it does |
|---|---|
| `review_queue(limit?)` | the unified, ACL-scoped inbox over two item kinds: `entity-proposal` (a capture that wants an entity minted) and `parked-capture` (a capture waiting on a human answer). An unrestricted (steward) identity sees every item; a scoped identity sees only its own |
| `review_decide(item_kind, item_id, verdict, notes?, name?, entity_id?, entity_type?, aliases?, role?, requeue?)` | record a verdict, attributed to the caller's resolved identity, into the append-only `review_decisions` record. Authorization is per kind: an `entity-proposal` decision requires the caller to be a STEWARD (`ops/stewards.json`) and refuses self-approval — the proposer of an entity may never be its own approver; `parked-capture` accepts the row's own submitter or a steward. `reject` and every `parked-capture` verdict never write to git — Postgres only, categorically. Approving an `entity-proposal` is the one path that does ([ADR 030](../decisions/030-server-side-entity-minting.md)): it mints through the governed door, exactly ONE commit, authored as the librarian App with an `Approved-by: <caller>` trailer. That verdict needs `name` (the page title) and `entity_type` (one of `entities.generator.ENTITY_TYPES`) — omitting either is refused, loud and actionable, naming what is missing, and mints nothing; `entity_id` defaults to `name`'s slug, `aliases`/`role` are optional, and `requeue=true` sends the originating capture back to the librarian once the push lands. The verdict vocabulary is per kind: `entity-proposal` takes `approve`/`reject` (there is nothing to request changes to — either the name resolves to an identity worth minting or it does not), `parked-capture` takes `capture.dispositions`' own three verbs (`requeue`/`resolve`/`reject`), not a generic `approve`/`reject`/`request_changes` |

Both ride the same `BrainService._call` wrapper as every other tool. Every unauthorized refusal and
every nonexistent-id refusal is the same byte-identical sentence (no existence leak); a caller
already authorized still sees the specific refusal underneath. Full mechanism:
`src/stigmergy/server/review.py`'s own module docstring; the Slack surface these two tools also back
is [slack.md](./slack.md#the-steward-doorbell-and-the-review-surface).

## The `ask` tool — the answering loop

`ask(question)` is counted separately from the four read tools above because its own
evidence-gathering agent has its OWN bounded tool set, run under this server's identity: `search`
(which accepts `filters`, same shape as `search_brain`'s), `read_page`, and
`describe_entity`, the entity-navigation surface every other client already has, rendered for the
agent so a broad entity question stops spending its budget on a search-and-read walk. Three tools,
and `list_entities` is deliberately **not** among them: the agent discovers ids from
search hits and from `describe_entity`'s own registry layer, and `ANSWER_SYS` tells it to prefer
`search(filters={"entity": <id>})` once one is known. It writes a cited answer, and a
**deterministic verifier** traces every figure back to the evidence the tools returned *this run*
and every citation quote back to its page. It gets **exactly one** corrective retry with the
findings — and the **strict gate** then scans both human-readable channels that would ship (the
answer prose and the citation quotes) and, on any untraced figure, withholds the whole draft:
`answer_markdown` and `citations` come back empty and the figure surfaces only inside `verdict`.
No untraced number ever leaves the server in readable prose. A refusal's own `reason` is not a
model channel at all — the server composes it from what it recorded this run — so there is nothing
of the model's left to scan there. The full mechanism lives in `stigmergy.answer` and is narrated in
[answer.md](./answer.md); the tool here is a thin skin over it.

The `ask` closure also does two things the read tools do not: `check_arg_length("question", …)` and
`service.require_embedder()` run INSIDE the rate-limited/audited call and before the agent, so a
keyless server refuses in milliseconds rather than after an evidence-gathering run that could never
have succeeded.

The response is structured JSON:

```json
{
  "question": "what is initech's arr?",
  "refused": false,
  "answer_markdown": "arr-usd for initech (2026-03): 512000 usd — source local-kpi!Sheet1!R3C3.",
  "citations": [{"path": "wiki/entities/initech/kpi.md", "quote": "…"}],
  "confidence": "high",
  "verdict": {"verdict": "verified", "unverified_figures": [], "citation_problems": []},
  "reason": "",
  "retried": false,
  "suppressed": false,
  "built_at": "2026-07-20T…"
}
```

- `verdict.verdict` is `verified` (no problems) · `partial` (exactly one problem) · `failed` (2+).
  A `partial` **ships labeled** only when its single problem is citation-only; a single untraced
  figure also reads `partial` but is suppressed. `failed` never ships — it surfaces **only** on a
  suppressed refusal (see `suppressed`). `verdict.unverified_figures` is the one place a withheld
  figure appears; it is never restated in `answer_markdown`, a citation quote, or `reason`.
- On a refusal (`refused: true`) `answer_markdown` and `citations` are empty and `reason` states
  what was searched and what came back; it carries **no** "want me to research/ingest it?" offer —
  `ask` never calls `brain_submit` on the caller's behalf; capturing what a refusal turned up is a
  separate, explicit action (see "The capture tools" above). A refusal because a page is out of
  scope is byte-shape-identical to one because nothing matched — existence never leaks. The
  composer runs a defensive figure scan over its own output and, in the unlikely event it fires,
  falls back to the generic `no_surface` sentence rather than shipping a hand-built exception.
- `suppressed: true` marks a refusal produced by the strict gate (an untraced figure was withheld
  from the answer or a citation quote); the findings still travel in `verdict`.
- `built_at` is the index timestamp, as in `search_brain` — a stale index is self-diagnosing.
- A refusal additionally carries `refusal_case`, `searched` and `surfaced` (page TITLES, never
  paths), beside `reason` rather than instead of it — a client that only ever read `.reason` keeps
  working. `searched` is not every query the agent tried: only the ones that are themselves a
  substring of the asker's own question ship verbatim, the rest are folded into a count, because
  the agent's query text is agent-authored and a steered agent could otherwise dictate server
  prose. The five cases and the sentence each composes are in
  [answer.md](./answer.md#refusal-is-a-first-class-result).

### Model / env config

| Var (flag) | Default | Meaning |
|---|---|---|
| `ANSWER_LLM` (`--answer-llm`) | `openai` | `fake` runs the whole path **keyless** (demo/CI/evals); an invalid value fails fast at startup |
| `ANSWER_MODEL` | `gpt-5.6-terra` | the synthesizer model (`OPENAI_API_KEY` required for `openai`) |
| `ANSWER_REASONING_EFFORT` | `medium` | reasoning effort for the OpenAI Responses model |

Note there is no CLI flag for the model or the reasoning effort: `--answer-llm` picks the BACKEND,
and the other two are environment-only, read once in `Settings.from_args`.

**Cost note.** Swapping to a cheaper model is a **one-variable change** — `ANSWER_MODEL`,
or `--model` on the eval runner — and the instrument that decides it already exists:
[`evals/run_qa.py`](../../evals/run_qa.py) measures one model per run against the frozen corpus,
and `make gates` judges honesty ≥ 0.90 and groundedness ≥ 0.84. There is **no** dual-model run
wired up; a cheaper default is a measurement someone takes, not a switch that flips itself. A
missing `OPENAI_API_KEY` with `ANSWER_LLM=openai` yields a clean `ask` error (never a traceback).

## Run it

```sh
make db-up                                                          # postgres+pgvector (the index)
.venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain          # build/refresh the index (needs a key)
.venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain --embedder fake   # keyless (tests/CI double)

# stdio: one process = one identity
.venv/bin/stigmergy-server --identity steward@example.com --repo ../stigmergy-brain

# HTTP, per-request identity (no --identity — see "HTTP transport" below)
STIGMERGY_TOKEN_STORE='{"<sha256hex>":"steward@example.com"}' \
  .venv/bin/stigmergy-server --transport http --port 8080 --repo ../stigmergy-brain
```

`--repo` defaults `--identities` to `<repo>/ops/identities.json` **and** `--entity-registry` to
`<repo>/ops/entity-registry.json` (the registry entity-first search, `list_entities` and
`describe_entity` all resolve through), and it is also the fallback for the knowledge-repo checkout
`review_decide`'s steward resolution reads `ops/stewards.json` from — that one can be set on its
own with `$STIGMERGY_KNOWLEDGE_REPO`, since there is no flag for the checkout itself. All of them
need an explicit value in production, where no `--repo` is passed at all. The DSN comes from
`--dsn` or `$STIGMERGY_INDEX_DSN`
(default `postgresql://stigmergy:stigmergy@localhost:54321/stigmergy`).

**`--stewards` / `$STIGMERGY_STEWARDS_PATH` is the one an operator forgets, and it fails quietly.**
It points at a baked `stewards.json` for a process that has NO knowledge-repo checkout — which is
exactly the deployed `app` and `slack` groups, started with baked `--identities`/`--entity-registry`
and no `--repo` at all. Where a checkout exists, the copy read at the base commit wins and this is
never consulted. Where one does not and this is unset, the steward map resolves EMPTY: the doorbell
rings for nobody and every entity-proposal decision fails closed — on a server whose
`ops/stewards.json` is perfectly correct in git.

**Both review kinds resolve stewards at the UNIVERSAL scope, and only there.** `stewards.json`
keys may be zone path prefixes, but neither kind has a page path to match one against — an entity
proposal has no page yet and a parked capture never got one — so both resolve with an empty scope,
which by construction can only match `"*"`. A map with careful zone-prefix entries and no `"*"`
therefore authorizes nobody for anything decidable here, and the refusal it produces is
byte-identical to the one a genuine outsider gets. Zone prefixes earn their keep on the doorbell's
routing, not on authorization.

The query embedder defaults to whatever model the index was built with (`index_meta`).
**A missing `OPENAI_API_KEY` is a DEGRADED start, not a refusal to start**: the process comes up
with an `UnavailableEmbedder`, `read_page` and the capture tools work normally, and
`search_brain`/`ask` refuse by naming the missing capability. That is deliberate — capture must not
depend on the read path's quota, key rotation or provider outage. What still exits non-zero is an
unknown `--embedder` NAME (a typo, not an absent credential — it would otherwise silently degrade a
server the operator thought they had configured), any identity failure, and an empty index. A
Postgres-side startup failure (down, or unreachable) never prints the raw DSN — only
`host:port/dbname`, credentials stripped — so MCP-client and cron logs stay credential-free.

## Connect Claude Code / Desktop (stdio)

Add an MCP server to your `.mcp.json` (Claude Code) or the Desktop config. stdio = one process
per client, so the identity is fixed per connection:

```json
{
  "mcpServers": {
    "stigmergy": {
      "command": "/path/to/stigmergy/.venv/bin/stigmergy-server",
      "args": ["--identity", "steward@example.com", "--repo", "/path/to/stigmergy-brain"],
      "env": { "STIGMERGY_INDEX_DSN": "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy" }
    }
  }
}
```

For the real embedder, add `OPENAI_API_KEY` to `env`. Point a second entry at a different
`--identity` to work as another audience scope side by side.

## HTTP transport

`stigmergy-server --transport http --host <h> --port <p>` serves the SAME 10 tools over MCP
streamable HTTP that stdio does — the four read tools (`search_brain`/`read_page`/
`list_entities`/`describe_entity`), `ask`, the three capture tools
(`brain_submit`/`brain_submissions`/`brain_reply`), and the two review-lane tools
(`review_queue`/`review_decide`) — gated by per-user
bearer-token auth instead of a fixed `--identity` flag: many testers share one
running process, each scoped to their own audiences. Full narrative + the design tradeoffs:
[ADR 013](../decisions/013-http-transport-and-token-auth.md).

**One shared app, `stateless_http=True` (mandatory, not a style choice).** `build_mcp(service)`
is called exactly once (stdio's own test contract); every identity is served through the SAME
FastMCP app via `transport_http._ScopedServiceProxy`, which forwards each tool-closure attribute
access to the per-request `BrainService` the auth middleware resolved (a `contextvars.ContextVar`,
set in the SAME coroutine as the downstream call — no task hand-off). This design is only correct
under `stateless_http=True`: FastMCP's DEFAULT stateful mode spawns a session's dispatch task ONCE
and keeps running it inside the context captured at session-creation time, so a later request's
identity would never actually reach the already-running task — which opens both
a session-hijack shape (execute under the session creator's identity/ACL scope while presenting
your own valid token, but the session creator's `mcp-session-id`) and an unbounded-task DoS
(stateful sessions have no idle timeout and `initialize` isn't rate-limited pre-auth). Stateless
mode gives every HTTP request a fresh, request-scoped dispatch task instead — no
`mcp-session-id` is ever handed out, so there is no session identity to borrow. stdio never calls
`streamable_http_app()` at all and is unaffected either way. Full mechanism: `transport_http.py`'s
module docstring and [ADR 013](../decisions/013-http-transport-and-token-auth.md) §4.

**Auth, fail-closed at every step**:
`Authorization: Bearer <token>` → SHA-256 hex → the token store (`$STIGMERGY_TOKEN_STORE` inline
JSON, or `$STIGMERGY_TOKEN_STORE_FILE` a path — both shape `{"<sha256hex>": "<email>"}`) → email →
`ops/identities.json` (the SAME resolver stdio uses, keyed by email) → audiences. ANY
failure — no token, an unrecognized hash, an email absent from `identities.json`, a malformed
store or identities file — returns the exact same generic body:

```
HTTP/1.1 401 Unauthorized
{"error": "unauthorized"}
```

No identity list, no filesystem path, no DSN fragment ever crosses this boundary; the real
reason is logged server-side only. Issue a tester's token with:

```sh
.venv/bin/stigmergy-issue-token ana@example.com
```

It prints the plaintext token ONCE (send it to the tester over a trusted channel — it is a
bearer credential) and the sha256 line to add to the token store. The plaintext is never
written to disk, a log, or a repo by this command.

**Host-header allowlisting** (ADR 013 amendment): the MCP SDK auto-enables
DNS-rebinding protection scoped to `127.0.0.1`/`localhost`/`::1` only, which 421s a real client
at the real deployed hostname before auth ever runs. `build_http_app` builds an explicit
`TransportSecuritySettings` that keeps that protection ON and adds the real host(s) from
`$STIGMERGY_PUBLIC_HOST` (comma-separated; unset = localhost-only behavior, local dev
unaffected) — see the operator runbook's Troubleshooting section for the `421`/`Invalid Host
header` symptom.

**Rate limiting**: 30 requests/min overall per identity, plus a stricter 10/min for
`ask` alone — a shared, process-wide `RateLimiter` (token bucket, injectable clock) so the
budget is honest across every request from that identity, not per connection. Refused calls get
a clear `{"error": "rate limited: ..."}` — never a bare protocol error. Deliberately **not**
applied to stdio (see ADR 013 §5): stdio's local-operator trust model doesn't need it, and the
existing `test_rebuild_while_serving.py` hammer test assumes unlimited local throughput.

**Argument-size bounds** (`service.py::MAX_ARG_CHARS`): every user-controlled
string argument reachable over the public HTTP boundary — `query`, `path`, `question`,
`entity`, and each `filters` **key and value** — is capped at 8192 characters and rejected with a
plain `ValueError` (a marker attribute, not a dedicated exception type — `check_arg_length` in
`service.py`) BEFORE the DB read, embedder call, or LLM call that argument would otherwise
trigger. This runs ahead of, not instead of, the rate limiter and audit write below.

**Rate limiting covers writes too**: `brain_submit` spends the same 30/min overall bucket as
every read tool. A refused submit creates **no queue row and no evidence object** — validation and
the limiter both run before the first write — and returns the same generic refusal shape.

**Audit**: every tool call, both transports, writes one `audit_log` row
(ts, identity email, tool, full args JSON, duration_ms, outcome, error class) — see
[operator-runbook.md](./operator-runbook.md) for reading the trail and harvesting the golden
set from it. A write failure is logged loudly and never fails the serving call (an accepted
tradeoff). Every string inside `args` is independently truncated to
`MAX_ARG_CHARS` before the row is written (`service.py::_truncate_for_audit`, recursing into
`filters`) — a rejected oversized argument is still audited, but never as an unbounded JSONB row;
a truncated value keeps a human-readable `...[truncated N chars]` marker. The same bound applies to
dict **keys**: `filters` keys are as client-controlled as its values, and
the unknown-filter-name rejection is itself audited, so an oversized key would otherwise reach the
JSONB row through the very path the value cap closed.

A `brain_submit` row records the **act, never the content**: `kind`, the material's byte
size, its sha256 — the same hash the evidence key is built from, so the audit trail joins to the
archived object — the hint **keys** (never their values), and `server_owned_args_present`, the
NAMES of any server-owned arguments the caller tried to set, never their values. No captured text
ever reaches `audit_log`.

The audit row is bounded by **shape** as well as by string length: at most
`MAX_AUDIT_HINT_KEYS` (32) hint names, then a `...[N more keys]` marker, and at most
`MAX_AUDIT_DEPTH` (20) levels of nesting, then a `...[nested too deep]` marker. Both close ways to
make a row enormous — or unserializable — without any single string in it being long; the depth
bound in particular keeps a deeply-nested `filters`/`hints` value from raising `RecursionError`
inside the audit path and clobbering the caller's real result (`_audit_args` wraps the shaping so
no failure there can ever surface through the served call).

**Request-body cap** (`transport_http.py::MAX_REQUEST_BODY_BYTES`): 1 MiB — 4× the 256 KB
material cap plus JSON-RPC envelope. A declared `content-length` above it is refused with a
generic `413` **before any of the body is read**; a chunked body with no declared length is cut
off at the same bound as it streams. Nothing below this middleware bounds a body — the MCP SDK
calls `await request.body()` with no limit and uvicorn imposes none — so without it the material
cap only fired *after* the server had buffered, parsed and hashed the whole thing.

### Every string reachable through the HTTP boundary

Every response body an HTTP client can receive from this server, and what it can contain:

| Source | Body | Can it leak an identity list / path / DSN fragment? |
|---|---|---|
| `_BearerAuthMiddleware` — any auth failure (no token, unrecognized hash, email absent from `identities.json`, malformed token store or identities file) | fixed `{"error": "unauthorized"}`, HTTP 401 | **No** — one constant, never built from the exception. The real `IdentityError` text (which, like `resolve_audiences`'s local-CLI message, may name a known-identities count) goes to `log.warning` only — server-side stderr/log, never the response |
| `search_brain` — `ValueError` (unknown filter name) | `{"error": str(ex)}` | No — echoes the CLIENT's own filter key plus the static allowed-column list; no server-side path/DSN |
| `search_brain` / `read_page` / `ask` — `RateLimitError` | `{"error": "rate limited: N requests/min exceeded — wait a moment and retry"}` | No — a static template with a configured integer, no identity or path |
| `search_brain` / `ask` — `CapabilityUnavailableError` (a keyless process: the embedder was never built) | `{"error": str(ex)}`, echoed VERBATIM | No — the message names a missing CAPABILITY and says capture still works, never a path or a key. Verbatim on purpose: collapsed to a class name it would read as an unexplained outage and send an operator hunting one |
| `search_brain` / `read_page` — any OTHER exception (`StigmergyIndexError` subtypes reachable from the live query path carry only static text with a literal `<dir>` placeholder, never an interpolated real path or DSN; anything unanticipated, e.g. a transient `psycopg.Error`) | `{"error": "<tool> failed (<ExceptionClassName>)"}` | No — class name only, `str(ex)` is never included |
| `ask` — any OTHER exception (a `pydantic_core.ValidationError` out of the agent/verifier stack, e.g. — a `ValueError` subclass, which is why the catch is narrowed by the `is_arg_length_error` marker rather than by type) | `{"error": "ask failed (<ExceptionClassName>); check ANSWER_LLM / OPENAI_API_KEY and that the index is built"}` | No — class name plus a fixed, value-free hint; `str(ex)` is never included, so untrusted LLM output cannot ride out through it |
| `read_page` / `ask` — a marker `ValueError` (`check_arg_length`'s own rejection) | `{"error": "<arg> too long (max 8192 characters)"}` | No — the argument NAME and a static limit, never the oversized value |
| `read_page` — unknown or out-of-scope path | `{"error": "unknown page: <path>"}` | No — `<path>` is the CLIENT's own request echoed back; existence itself stays scoped (identical body whichever reason applies) |
| `list_entities` — any exception (a malformed entity registry raises `ValueError` naming the registry's file PATH) | `{"error": "list_entities failed (<ExceptionClassName>)"}` | **No** — class name only, always; unlike `search_brain`'s unknown-filter `ValueError` (safe to echo — it names only the caller's own input), a registry-malformed message names a server-side filesystem path and must never reach the wire |
| `describe_entity` — a marker `ValueError` (`check_arg_length`'s own rejection) vs any other exception | marker: `{"error": "entity too long (max 8192 characters)"}`; other: `{"error": "describe_entity failed (<ExceptionClassName>)"}` | No — same narrowing as `read_page`: only the length-check's own known-safe message echoes verbatim |
| `describe_entity` — unknown or out-of-scope entity | `{"error": "unknown entity: <input>"}` | No — `<input>` is the CLIENT's own request echoed back; existence itself stays scoped (byte-identical whichever reason applies, mirroring `read_page`'s own rule) |
| Any unhandled exception inside the auth middleware or the ASGI app itself (a genuine bug, not a designed path) | Starlette's default `ServerErrorMiddleware` response, HTTP 500 | No — `debug=False` (the `FastMCP`/`Starlette` default here), so Starlette's own handler returns a fixed generic body with no traceback |
| `brain_submit` / `brain_submissions` — `SubmissionRejected` (forged `submitted_by`, a server-owned or unknown hint key, an unknown `kind` or `status`, empty or oversized material) | `{"error": str(ex)}` | No — every message in this family is built from the CALLER's own field/hint/status value plus static text (a byte limit, the allowed kinds, the allowed hint keys, the status list). Same shape and same safety as `search_brain`'s unknown-filter error |
| `brain_submit` — `EvidenceError` (the object store is unreachable, misconfigured, or refuses the write) | `{"error": "evidence store unavailable (<ExceptionClassName>)"}` | No — the redaction happens INSIDE `capture.evidence`, before the exception leaves it: boto3's own exception text embeds the endpoint URL, the bucket name and the access key id, so `str(ex)` is never propagated. The full detail, bucket and endpoint included, goes to `log.error` server-side |
| `brain_submit` / `brain_submissions` — any OTHER exception | `{"error": "<tool> failed (<ExceptionClassName>)"}` | No — class name only, same posture as the four read/answer tools above |
| `_BearerAuthMiddleware` — a `content-length` over `MAX_REQUEST_BODY_BYTES` | fixed `{"error": "request too large"}`, HTTP 413 | **No** — one constant, like the 401 above. It is returned only AFTER auth succeeded, so it also cannot be used to probe whether a token is valid: an unauthenticated oversized request gets the 401, not this |
| `brain_reply` — `ReplyRejected` (identity case: nonexistent id, somebody else's row, somebody else's row not even `needs_input`) | `{"error": "..."}`, one fixed sentence | No — deliberately identical whichever of the three applies, the same no-existence-leak posture `read_page` already takes |
| `brain_reply` — `ReplyRejected` (state case: an authorized caller's OWN row is not `needs_input`, or the answer fails the length/emptiness check) | `{"error": "..."}`, naming the row's actual status | No — only raised for a caller already authorized to read the row through `brain_submissions`, so naming its status leaks nothing new |
| `review_queue` / `review_decide` — `CaptureError` (an unknown item kind/verdict, a missing required note, the `NOT_YOURS_TO_DECIDE` authorization refusal, an entity-proposal approve missing `name`/`entity_type`, a mint refusal — a collision, drift, a secret in the role/aliases text, a lost push race) | `{"error": "..."}` | No — the same fixed, no-existence-leak sentence for "does not exist" and "not authorized" alike (`review.NOT_YOURS_TO_DECIDE`); every other message here is built from the caller's own field/verdict value plus static text, including the entity-mint refusals mapped in from `entities.errors.EntityError` |
| `review_decide` — `CapabilityUnavailableError` (an entity-proposal approve with no librarian GitHub App credential or no `$STIGMERGY_LIBRARIAN_REPO_URL` configured, [ADR 030](../decisions/030-server-side-entity-minting.md) D3) | `{"error": str(ex)}`, echoed VERBATIM | No — names the missing capability by env var, same posture as `search_brain`/`ask`'s own keyless-embedder refusal above; mapped in from `entities.errors.CapabilityUnavailableError`, which entities itself raises (it may not import this package's error type directly) |
| `review_queue` / `review_decide` — any OTHER exception | `{"error": "<tool> failed (<ExceptionClassName>)"}` | No — class name only, same posture as every other tool above |

An unknown token and an unknown identity both resolve to the SAME first row above — the fixed
`{"error": "unauthorized"}` — which is the strongest possible guarantee against enumeration: there
is no per-reason variation for a tester (or the tester's tooling) to observe at all. A request
presenting TWO `Authorization` headers gets the same 401 and is never resolved against whichever
value would have won a dict collapse.

The table above covers the MCP surface. Two paths on the same Starlette app are outside it and
authenticate their own way: `POST /webhook/github` (HMAC over the raw body, the one exact-path
exemption from the bearer middleware) and `/admin*`, an ASGI branch mounted in FRONT of the
middleware — inert 404s unless `$STIGMERGY_ADMIN_TOKEN_HASH` is set, and documented in
[admin-console.md](./admin-console.md).

## Identities file

A versioned JSON map checked into the knowledge repo at `ops/identities.json`, **keyed by
email**:

```json
{
  "steward@example.com": "*",
  "ana@example.com": ["finance"],
  "bob@example.com": ["sales", "leadership"]
}
```

`"*"` = unrestricted (sees everything). A list is the client's audience scope: it sees unlabeled
pages plus pages sharing at least one label; a bare non-`*` string is accepted as the one-audience
scope it obviously means. The resolver is **fail-closed** everywhere — no identity, an unknown
identity, an unreadable/malformed file, or an identity whose value is neither of the shapes above
each exits non-zero (stdio) or 401s generically (HTTP) with an actionable message; the server never
starts open.

stdio's `--identity` flag takes the SAME email keys (e.g. `--identity steward@example.com`) —
`identity.resolve_audiences` is one resolver for both transports. HTTP resolves
the email from a bearer token instead of a flag (see "HTTP transport" above).

> This file is **configuration**: anyone who can edit it (or, for stdio, pass `--identity`)
> impersonates anyone AT THE AUDIENCE-SCOPE layer. HTTP callers additionally need a
> valid bearer token to reach that layer at all — real per-caller verification, not an honor
> system (the caveat now applies to stdio only). Google OAuth is still the unbuilt target for a
> real Workspace. Guard this file with the same care as any access-control config regardless.

## ACL semantics (the enforcement rule)

`acl.visible(acl, audiences)` is the one rule, applied to every surface (search, read,
discovery hints):

| Stored acl | Meaning | Scoped client | Unrestricted client |
|---|---|---|---|
| `NULL` (no acl) | open | ✅ visible | ✅ visible |
| `{}` (empty acl) | nobody | ❌ hidden | ✅ visible |
| `{sales,…}` | scoped | ✅ iff shares a label | ✅ visible |
| malformed value | untrusted | ❌ hidden | ❌ hidden (logged loudly) |

A malformed stored acl is hidden even from unrestricted clients and logged — a value we cannot
trust must never resolve to "open" at the point access is decided (fail-closed, ADR 012 §4).

## Rebuild workflow (staleness)

The server holds no copy of the index — every call reads Postgres — so a rebuild refreshes what
the server serves **without a restart**. Searches issued during a rebuild do not error (the
rebuild swaps the table inside one transaction; the reader runs autocommit so it never blocks a
`DROP`). Every `search_brain` response carries `built_at` so a stale index is self-diagnosing.
`read_meta` also tolerates an `index_meta` row written before the `built_at` column existed:
it is treated as an empty index rather than crashing on a missing column, so startup
surfaces the actionable `--rebuild` hint instead of a raw database error.

Manual:

```sh
.venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain
```

Local cron (e.g. hourly, keyless double shown; drop `--embedder fake` and add `OPENAI_API_KEY`
for the real model):

```cron
0 * * * *  cd /path/to/stigmergy && .venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain >> /tmp/stigmergy-index.log 2>&1
```

If you edit a page and forget the rebuild, search "misses" it until the next build — check the
`built_at` in any search response to see how fresh the index is. Scheduled rebuilds run in CI; see
[operator-runbook.md](./operator-runbook.md).

## Reuse these seams

- `stigmergy.server.acl.visible(acl, audiences)` — the **one** ACL rule. Every read path (search,
  `read_page` — including its links/backlinks — `list_entities`, `describe_entity`,
  discovery hints) must filter through this function; do not re-implement label matching anywhere
  else. `acl.all_visible(paths, visible_paths)` is its companion for text composed from MORE than
  one page's identity: all-or-nothing, never per-path, because a partially scrubbed sentence is the
  kind of defense that looks complete and is not.
- `stigmergy.server.identity.resolve_audiences(identities_path, identity)` — the **one** identity
  resolver. Fail-closed; raises `IdentityError` on any failure. Callers must not proceed without
  a resolved scope.
- `stigmergy.server.service.BrainService` / `build_service(settings, conn=None)` — the
  transport-agnostic core. `build_service` wires identity → connection → embedder → service
  fail-closed, in that order (identity resolves before any DB work). New tools/transports should
  call into a `BrainService` instance, never re-open the index themselves.
- `stigmergy.server.review.review_decide_safe` — the review lane's clean-refusal wrapper, for a
  caller (`stigmergy.slack`) that may not import `stigmergy.capture.errors`/`stigmergy.server.errors`
  directly. Use it rather than catching `CaptureError` from outside the declared exception list.
- `tests/server/conftest.py::mcp_session` — an async context manager that spawns the real
  `stigmergy-server` console entry point over stdio and drives it with a real MCP client
  (`ClientSession`). Use it for any test that must prove the transport, not just the service
  logic; `make_service` is the in-process equivalent for tests that only need `BrainService`.

## Avoid / anti-patterns

- Never query `stigmergy.index` (Postgres) directly from an MCP tool closure —
  every read goes through `BrainService`, which is the only place ACL filtering and the page
  contract are enforced. A tool that bypasses it re-opens the leak the service exists to close.
- Never import `stigmergy.librarian`/`stigmergy.entities` beyond the two small, declared,
  mechanically-pinned sets `webhook.py` and `review.py` already hold (enforced by
  `tests/test_architecture.py`, which fails on an unused declaration too). Packages talk through
  files and durable rows, never imports — `stigmergy.kernel` is the one library exempt from that
  rule, by design.
- Treat `ops/identities.json` as **configuration, not authentication**: anyone who can edit the
  file — or, over stdio, pass `--identity` — impersonates anyone at the audience-scope layer. The
  bearer tokens put a real per-caller check in front of it for HTTP, but they did not turn the file
  itself into a security boundary, and Google OAuth is still unbuilt. Do not
  build features that assume it is stronger than that.

## Tests

`tests/server/` — Postgres-backed suites skip cleanly without `make db-up` and FAIL (not skip)
when `$STIGMERGY_TEST_DSN` is set (CI mode), same posture as `tests/index/`. They run against the
`stigmergy_test` database, never the one a running brain serves; `tests/testdb.py` refuses anything
else ([operator-runbook.md](./operator-runbook.md#the-two-databases)):

| Suite | Covers |
|---|---|
| `conftest.py` | the fixture repo/identities (`Fixture`), `indexed` (real postgres + fake embedder), `mcp_session` (real stdio subprocess + real MCP client), `make_service` (in-process `BrainService`) |
| `test_identity.py` | `identity.resolve_audiences` fail-closed on every path |
| `test_acl_visibility.py` | the full `acl.visible` truth table, including the fail-closed malformed case |
| `test_service_acl.py` | `BrainService` end to end (fake embedder, real Postgres): ACL enforcement on `search`, `read_page`, `scoped_entities` and the view pages, plus the `max_results` clamp and the structured output shape |
| `test_acl_empty_and_malformed_e2e.py` | a deliberate `acl: []` and a build-time-malformed acl land in the exact same nobody-but-unrestricted state |
| `test_review.py` | the review lane end to end (`review_queue`/`review_decide` over the **two** item kinds), real git + real Postgres |
| `test_service_capture.py` | the write path at the service layer: attribution, the server-owned-field refusals, the submissions listing and its scoping |
| `test_fence.py` | the `UNTRUSTED-DATA` fence is inescapable in-band, including truncation-boundary edge cases |
| `test_startup.py` | startup through the real `main()`: no/unknown identity, malformed identities file, unreachable Postgres (credential-free error) and an `index_meta` row without `built_at` all exit non-zero — while a missing `OPENAI_API_KEY` DEGRADES instead (the process starts and still serves the write path) and an unknown embedder NAME is still a clean startup error |
| `test_mcp_adapter.py` | `build_mcp`'s own tool-closure logic in-process against a `create_autospec(BrainService)` double — argument wiring and error-to-JSON mapping, not ACL/ranking |
| `test_mcp_harness.py` | the real MCP protocol end to end: a spawned `stigmergy-server` subprocess over stdio, including the two-identities-two-realities test and the down-Postgres handshake-fails-promptly test |
| `test_ask_mcp.py` | the `ask` tool over the real stdio protocol (`ANSWER_LLM=fake`): verdict object, citations and `built_at` round-trip; the unanswerable refuses with an offer-free reason |
| `test_read_page_graph.py` | `read_page`'s `type`/`status`/`supersedes`/`superseded_by`, `links`/`backlinks`, two-identity existence leak both directions, the `NAV_CAP` truncation note |
| `test_entity_tools_pg.py` | `list_entities`/`describe_entity` — scoped vocabulary, registry postures, the description layers, id/name/alias resolution, byte-identical absence |
| `test_entity_first_search_pg.py` | entity-first resolution at `BrainService.search` AND through the real `search_brain` MCP surface |
| `test_entity_aliases.py` | `entity_aliases` as a pure file contract: `load_aliases`/`load_registry`/`resolve_entity`/`resolve_exact`, registry-missing fail-open vs registry-malformed raise |
| `test_entity_tools_neutralization_pg.py` | a hostile registry/page title never escapes through `list_entities`/`describe_entity` |
| `test_settings_entity_registry.py` | the `--entity-registry` precedence: an explicit path beats the `--repo` convention, and production passes no `--repo` at all |
| `test_keyless_capability.py` | a keyless process still starts, `read_page` and the capture tools still work, and `search_brain`/`ask` say which capability is missing |
| `test_webhook.py` | signature verification, the exact-path exemption, incremental upsert/delete, outbound-link re-resolution and the split-chain supersession propagation over BOTH part conventions |
| `test_host_header.py` | the `421 Invalid Host header` bug: `$STIGMERGY_PUBLIC_HOST` is allowlisted while DNS-rebinding protection stays on (and the `json_response=True` precondition that lets a raw `httpx` probe decode the body at all) |
| `test_admin_branch.py` | the admin branch on the real `build_http_app` wiring: inert 404s with no admin token configured, the console live behind its own token, and the MCP surface unchanged either way |
| `test_pilot_report.py` | `stigmergy-pilot-report`'s table over real `audit_log`/`capture_queue` rows |
| `test_granularity_tripwire_pg.py` | a standing tripwire: the same content filed one-page-per-subject vs bundled into one `entity: []` page, run against all three anchoring consumers (`search`'s `entity` filter, `describe_entity`'s timeline, `views.skeleton.members_of`) — a bundled page is STRUCTURALLY invisible to every one of them, however clearly its body names those subjects |
| `test_rebuild_while_serving.py` | a `stigmergy-index` rebuild refreshes a live server with no restart; searches issued mid-rebuild never error |
| `test_service_layer_wrapping.py` | `BrainService._call`/`.call_async` — rate-limit check before the wrapped call, one audit row per call (success, exception, and rate-limit refusal alike), no-op when both are `None` |
| `test_arg_length.py` | `check_arg_length`/`MAX_ARG_CHARS` short-circuits BEFORE the DB/embedder/LLM call, proven with poisoned doubles that raise on any attribute access if ever touched |
| `test_ratelimit.py` | `RateLimiter` token bucket, pure unit tests against an injectable fake clock (30/min overall, 10/min `ask`, the 31st-request boundary) — no Postgres, runs unconditionally |
| `test_audit.py` | `audit_log` DDL + `AuditWriter` against real Postgres — a row lands per call, a write failure is swallowed and logged, not raised |
| `test_transport_http.py` | the HTTP transport end to end — a real uvicorn server + a real MCP `streamablehttp_client` against the production `build_http_app` wiring; two identities, same prompt, different results, audit attributes each |
| `test_token_hygiene.py` | automated `git grep` proof that no real sha256 token-store entry (64 lowercase hex chars) ever lands in a tracked file of this repo |
| `test_issue_token_cli.py` | `stigmergy-issue-token` — prints the plaintext once + the store line, rejects a non-email argument with an actionable message |
