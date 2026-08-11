# Third-party licenses

`stigmergy` is licensed under Apache-2.0 (see [`LICENSE`](./LICENSE)). Its dependencies keep
their own licenses. This file records them, and the one obligation that is not automatic.

## The one that carries an obligation: psycopg (LGPL-3.0-only)

[`psycopg`](https://www.psycopg.org/) — the PostgreSQL driver behind the derived index — is
**LGPL-3.0-only**, and `psycopg[binary]` ships the same license.

This does **not** make this project LGPL. psycopg is used as a separate, ordinarily-installed
library through its public API; nothing here is a derivative work of it. Apache-2.0 remains the
license of this repository's own code.

It does mean that **anyone redistributing a built artifact that bundles psycopg** — notably the
Docker image produced by this repository's `Dockerfile`, which runs `pip install .` — takes on
LGPL §4 and §6: ship or offer the LGPL-3.0 text, and keep the library replaceable. The second part
is already true by construction (psycopg is an ordinary `site-packages` install that a user can
swap for another build of the same version), so in practice the obligation is to carry the license
text alongside anything you distribute. If you publish images built from this repo, include this
file and psycopg's own `LICENSE.txt` from its distribution.

Nobody who merely *runs* the software, or who installs it from source with `pip install -e .`,
takes on anything: they are installing psycopg themselves, directly from PyPI, under its own terms.

## Direct dependencies

Verified against the installed distributions' own metadata.

| Package | License | What it is here for |
|---|---|---|
| `psycopg` | **LGPL-3.0-only** | the hybrid derived index: Postgres + pgvector |
| `aiohttp` | Apache-2.0 AND MIT | the Slack transport's async Socket Mode handler |
| `boto3` | Apache-2.0 | the evidence plane's S3-compatible object store |
| `google-genai` | Apache-2.0 | vision OCR for scanned documents |
| `claude-agent-sdk` | MIT | the librarian's filing agent |
| `mcp` | MIT | the MCP server — the only API over the brain |
| `openpyxl` | MIT | spreadsheet conversion |
| `pydantic` | MIT | schema validation across every boundary |
| `pydantic-ai` | MIT | the answering agent, and the librarian's meeting backend |
| `pyjwt` | MIT | the librarian GitHub App's RS256 assertion |
| `python-docx` | MIT | document conversion |
| `pyyaml` | MIT | page frontmatter |
| `slack-bolt` | MIT | the Slack transport |
| `httpx` | BSD-3-Clause | HTTP client |
| `starlette` | BSD-3-Clause | the HTTP transport's ASGI layer |
| `uvicorn` | BSD-3-Clause | the HTTP transport's ASGI server |
| `xlrd` | BSD | legacy spreadsheet conversion |

Everything above except `psycopg` is permissive and imposes no condition beyond attribution.

`certifi`, pulled in transitively, is MPL-2.0 — file-level copyleft that reaches only modifications
to certifi's own files, and so places no condition on this project.

## Tools, not dependencies

[`gitleaks`](https://github.com/gitleaks/gitleaks) (MIT) is executed as an external binary by the
librarian's secrets gate and by CI. It is never linked or vendored; it is pinned by SHA-256 digest
where it is downloaded.
