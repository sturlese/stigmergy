---
status: implemented
---

# Backoffice users, personal sign-in, and a permission-scoped wiki

A workspace's people, layers, MCP tokens, and Slack channel layers live in Postgres
(`server/roster.py`) and are administered in its backoffice. Every person signs in with their own
token, and everyone reads a wiki of exactly the pages their layers allow. Layer rules are in
`specs/audience-layers.md`; tenancy and platform operators in `specs/multi-tenancy.md`. Nothing
reads a roster file at runtime.

## 1. Roles

- **Reading every layer** is the `brain-admins` group, a role rather than a layer.
- **Administering a workspace** is the `is_admin` flag. It exists only on people, never on agents,
  and requires membership of `brain-admins` (a database check), because the operational sections
  show every page.
- **Platform operators** use the master console; inside a workspace they administer it as
  `specs/multi-tenancy.md` §2 describes.

## 2. Data model

| Table | Key | Columns and rules |
|---|---|---|
| `roster_principals` | `(tenant_id, subject)` | subject (lowercase email), display_name, kind (`person`/`agent`), is_admin, groups text[], default_audience text[], disabled_at, created/updated at and by. Saving requires a default layer: `public` or one of the principal's layers |
| `roster_groups` | `(tenant_id, name)` | description (at most 1,000 characters), created at and by. A layer name is 2–32 lowercase letters, digits, or hyphens, starting with a letter, and not reserved. Each workspace starts with `brain-admins` |
| `roster_tokens` | `id` (uuid); `sha256` unique across the platform | subject, label (at most 80 characters), prefix, created at and by, last_used_at, expires_at, revoked_at |
| `roster_sessions` | `sha256` of the session value | subject, token_id (the token that opened it), created_at, last_seen_at, expires_at, revoked_at |
| `roster_slack_channels` | `(tenant_id, channel_id)` | groups: exactly one layer, or null for `public`; never `brain-admins` |

Every table carries `tenant_id` under the row-level policy of `kernel.tenancy`. Every change to
principals, groups, tokens, or channels appends to `admin_actions` with actor, action, target, and
outcome, never a token or session value.

## 3. Identity

- **ID-01** MCP bearer tokens, Slack emails, the writer's actor scope, capture layer checks, and the
  backoffice resolve principals from these tables through one module.
- **ID-02** A request resolves its principal with one indexed query, so a revoked token, a disabled
  person, or a removed group stops working on the next request.
- **ID-03** A disabled principal, a revoked or expired token, and a suspended workspace are refused
  with the same response as an unknown token.
- **ID-04** Tokens are `stg_` plus 43 URL-safe characters (256 random bits), stored only as SHA-256;
  `last_used_at` moves at most once a minute. One kind of token connects a person's MCP clients and
  signs them in to the backoffice. An agent's token opens a session too, which reaches only the
  wiki it may read.
- **ID-05** The last active administrator cannot be disabled, demoted, or deleted. Roster writes
  that check other rows are serialized by an advisory lock. A group in use by a person, as a
  default, or by a channel cannot be deleted, nor a layer that holds indexed knowledge or a capture
  still being filed; deletion is refused until the index has caught up with the repository.
- **ID-06** With direct database access, `stigmergy-admin --tenant <slug> bootstrap <email> --name
  <name>` creates a workspace's first administrator and prints one token, only while it has none;
  `stigmergy-admin --tenant <slug> mint <email> [--label]` issues a new token to an enabled
  administrator who lost theirs.
- **ID-07** `stigmergy-admin --tenant <slug> import --identities FILE --channels FILE --admin EMAIL
  [--agent EMAIL] [--token-store-file FILE]` loads a legacy file roster once: identities, the Slack
  channel map, and token digests (the file or `STIGMERGY_TOKEN_STORE`). It validates every entry
  like a backoffice edit, runs in one transaction, refuses a roster that already has people, and
  must name an administrator. Imported digests keep working, so existing clients need no new token.
- **ID-08** Failed authentication is throttled per client address, shared by MCP and the
  backoffice: ten failures in fifteen minutes block that address.

## 4. Sign-in and sessions

- **AUTH-01** The sign-in page accepts a person's token (or an operator token). The backoffice API
  accepts only the session, never a bearer token, and the browser never stores the token. The
  session value is random, stored as a hash, and issued anew at each sign-in. The cookie is
  `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/admin`. Sessions expire after 8 idle hours and
  at most 7 days.
- **AUTH-02** Every state-changing request needs the session cookie, an `Origin` naming this host,
  and the anti-CSRF header the page sends; a request missing any of them is refused.
- **AUTH-03** Sign-out ends the session; revoking a token ends every session it opened.
- **AUTH-04** Admin endpoints check the administrator flag on the server; hiding a menu is never
  the control.
- **AUTH-05** A member sees the Wiki, the Graph, and an account area showing who they are and their
  layers. Admin-only endpoints answer them as an unknown route and leave an audit row.
- **AUTH-06** There is no master token: administrators sign in like everyone else.

## 5. Administration screens

- **People**: list, create, edit, disable, and delete; fields are name, kind, administrator flag,
  layers, and default layer, all validated on save. An invalid edit leaves the previous state.
- **Tokens**, on a person's page: create with a label and an optional expiry; the token is shown
  once with ready-to-copy setup for Claude Code, Codex, and Claude Desktop, then only its prefix,
  creation, and last use. Revoke and regenerate; a token cannot be shown again.
- **Layers**: create, describe, list members, erase content (`specs/data-erasure.md` §3), and
  delete when unused.
- **Slack channels**: map a channel to one layer.
- **Operations**: overview, captures, changes, contradictions, entities, gardener, index, and
  activity.

## 6. Wiki

- **WIKI-01** An explorer lists the visible projects, entities (grouped by entity type), concepts,
  notes, and sources (grouped by month) in collapsible sections that remember their state, with a
  filter box. A command palette (⌘K) jumps to any visible page or section and searches contents
  with the same hybrid search as MCP.
- **WIKI-02** A page shows its title, type, last update, and the layers the reader sees it in; an
  entity's type and aliases; and the rendered body, where every resolved wikilink and source
  citation is an internal link and an unresolved link is plain text.
- **WIKI-03** A side panel lists visible backlinks, outgoing links, and cited sources, and draws the
  one-hop graph around the page from visible pages only.
- **WIKI-04** A hidden page and a nonexistent page give the same "not found" view, whether reached
  by link, URL, or search.
- **WIKI-05** Every wiki page, catalog, graph, and search request goes through `BrainService` with
  the reader's layers, so it is scoped, audited, and rate-limited like MCP. The index, not Git, is
  read.
- **WIKI-06** The wiki is read-only; knowledge changes only through captures.
- **WIKI-07** Markdown is rendered on the server with raw HTML and images disabled, then sanitized
  against an allowlist; links allow only internal routes and `https`, with
  `rel="noopener noreferrer"`.
- **WIKI-08** The wiki says only whether the index is up to date or catching up; when it last moved
  would reveal writes in layers the reader cannot see.
- **WIKI-09** A global graph shows every visible page and every link between two visible pages,
  including cited sources, colored by type, with search, type and layer filters, and hover previews.

A scoped reader sees identical explorers, pages, backlinks, graphs, and search results whether or
not pages outside their layers exist, and adversarial page content (`<script>`, event handlers,
`javascript:` links, CSS or SVG payloads) renders inert.

## 7. Console build

The console is a React application in `web/` (Vite, Tailwind CSS, Radix primitives in the shadcn
style, Sigma.js for graphs), built into the package with `make web`. Its output is static, so the
CSP stays strict: scripts only from the console's own origin, and runtime style elements only with
the per-page nonce the shell hands them. It has light and dark themes and works at laptop and
tablet widths.
