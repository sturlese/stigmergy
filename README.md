# Stigmergy

Stigmergy is a Karpathy-style wiki for a team: immutable source material enters one queue, one
librarian keeps a small Git-and-Markdown wiki current, and every read is scoped to the caller.

The platform adds only the infrastructure a shared deployment requires: identity and ACLs,
concurrent capture, private binary evidence, extraction and OCR, Slack acquisition, hybrid search,
stable entity identity, autonomous corpus repair, and a master backoffice.

## Core model

```text
local MCP bridge ─┐
Slack 🧠 reaction ├─ acquire bytes ─ CaptureEnvelope ─ durable queue ─ one Git writer
master backoffice ┘                                      │
                                                        ├─ immutable sources/YYYY/MM/<uuid>.md
                                                        ├─ current wiki/notes and wiki/concepts
                                                        └─ one commit + one Changes record
```

- Exact original bytes are content-addressed in a private S3-compatible object store.
- One readable source page is immutable after capture, except for explicit deletion.
- The librarian may create, rewrite, consolidate, and delete wiki pages without approval.
- Visibility constrains writes as well as reads; restricted evidence cannot broaden into an open
  page.
- Entity pages contain only opaque identity and scoped name claims. `describe_entity` composes
  visible knowledge from ordinary pages at read time.
- Credible contradictions remain explicit and cited. A later master resolution is a new capture,
  never a blocker on the original run.
- The gardener is one scheduled `lint-and-fix` operation inside the writer. It creates no task
  backlog for people.

All model-backed runtime paths use one OpenRouter credential with provider fallback disabled,
zero-data-retention routing required, and a closed model allowlist: DeepSeek V4 Flash for filing
and semantic repair, GLM 5.2 for answers, Qwen3 Embedding 8B at 2560 dimensions for search, and
Qwen3 VL 8B for OCR. Direct Anthropic, OpenAI, and Gemini credentials are neither accepted nor
forwarded to runtime processes.

The full implementation contract is [the team-wiki specification](./specs/karpathy-team-wiki.md).

## Supported capture surfaces

- `stigmergy-bridge`: local stdio MCP for Codex or Claude Code. It accepts exactly one of text,
  local path, or URL. Private Google Drive acquisition uses local OAuth and the OS keychain;
  Google credentials never reach Stigmergy.
- Slack: a brain reaction snapshots the thread and downloads supported attachments under the
  configured channel audience.
- Backoffice: the master can paste text, upload a file, or submit a public URL.

Supported artifacts are UTF-8 text/Markdown, HTML, PDF, DOCX, PPTX, PNG, JPEG, Google Doc exports,
Google Slides exports, and canonical Slack snapshots. File type is detected from bytes and parser
validation. OCR runs only for images and scanned or poor PDF pages.

## MCP tools

The cloud and local bridge expose the same compact product surface:

- `search_brain`
- `read_page`
- `list_entities`
- `describe_entity`
- `ask`
- `brain_submit`
- `brain_submissions`
- `brain_delete`

Local paths and private Drive URLs are acquired only by the local bridge. Large files use
short-lived object uploads rather than base64 tool arguments.

## Install the local bridge

Install the package once on each machine that runs Codex or Claude Code:

```bash
uv tool install /path/to/stigmergy
export STIGMERGY_TOKEN="<identity-token>"
```

For Codex, add a project-scoped `.codex/config.toml`:

```toml
[mcp_servers.stigmergy]
command = "stigmergy-bridge"
args = ["--url", "https://stigmergy.fly.dev"]
env_vars = ["STIGMERGY_TOKEN", "STIGMERGY_GOOGLE_CLIENT_SECRETS"]
required = true
```

For Claude Code, add a project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "stigmergy": {
      "command": "stigmergy-bridge",
      "args": ["--url", "https://stigmergy.fly.dev"],
      "env": {
        "STIGMERGY_TOKEN": "${STIGMERGY_TOKEN}",
        "STIGMERGY_GOOGLE_CLIENT_SECRETS": "${STIGMERGY_GOOGLE_CLIENT_SECRETS:-}"
      }
    }
  }
}
```

Private Google Drive capture additionally requires
`STIGMERGY_GOOGLE_CLIENT_SECRETS=/absolute/path/google-oauth-client.json`. The first private Drive
capture opens local OAuth; the resulting credential stays in the OS keychain.

## Development

Requirements are Python 3.12+, `uv`, Docker, and `gitleaks` for release validation.

```bash
make venv
make db-up
make test
make lint
```

Focused cross-subsystem acceptance tests are available as `make test-system`. `make help` lists
the supported developer and operator targets.

## Documentation

- [Architecture](./docs/ARCHITECTURE.md)
- [Operations and deployment](./docs/OPERATIONS.md)
- [Clean test-environment reset](./docs/RESET.md)
- [Quality evaluations](./evals/README.md)
