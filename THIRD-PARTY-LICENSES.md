# Third-party licenses

`stigmergy` is licensed under the GNU Affero General Public License, version 3 (see
[`LICENSE`](./LICENSE)). Its dependencies keep their own licenses. This file lists every package
installed with it and every package bundled into the console, from each distribution's own
metadata, and the obligations that are not automatic.

## Copyleft dependencies

- **PyMuPDF** (`pymupdf`, PDF text extraction): AGPL-3.0, or a commercial license from Artifex.
  It is compatible with this project's AGPL-3.0. Anyone who distributes Stigmergy, or offers it
  over a network, under terms other than the AGPL needs Artifex's commercial license or a
  replacement for PDF extraction.
- **psycopg** (`psycopg`, `psycopg-binary`, `psycopg-pool`, the Postgres driver and pool):
  LGPL-3.0-only. It is used as an ordinary installed library through its public API. A built
  image that bundles it carries the LGPL text and keeps the library replaceable, which a normal
  `site-packages` install already does.
- **certifi**: MPL-2.0, file-level copyleft that reaches only changes to certifi's own files.
- **git-filter-repo** (layer erasure rewrites history with it): MIT, or GPL-2.0 at the
  recipient's choice; it is used under MIT.

## Python packages installed with Stigmergy

Resolved from `uv.lock` without development tools. Packages marked with † install only on some
operating systems.

| Package | Version | License |
|---|---|---|
| `aiohappyeyeballs` | 2.7.1 | PSF-2.0 |
| `aiohttp` | 3.14.3 | Apache-2.0 AND MIT |
| `aiosignal` | 1.4.0 | Apache-2.0 |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.14.2 | MIT |
| `attrs` | 26.1.0 | MIT |
| `boto3` | 1.43.78 | Apache-2.0 |
| `botocore` | 1.43.78 | Apache-2.0 |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `cffi` | 2.1.1 | MIT-0 |
| `charset-normalizer` | 3.5.1 | MIT |
| `click` | 8.4.2 | BSD-3-Clause |
| `colorama` † | 0.4.6 | BSD-3-Clause |
| `cryptography` | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| `frozenlist` | 1.8.0 | Apache-2.0 |
| `genai-prices` | 0.1.10 | MIT |
| `git-filter-repo` | 2.47.0 | MIT OR GPL-2.0 |
| `google-api-core` | 2.34.0 | Apache-2.0 |
| `google-api-python-client` | 2.199.0 | Apache-2.0 |
| `google-auth-httplib2` | 0.4.1 | Apache-2.0 |
| `google-auth-oauthlib` | 1.4.0 | Apache-2.0 |
| `google-auth` | 2.56.3 | Apache-2.0 |
| `googleapis-common-protos` | 1.75.1 | Apache-2.0 |
| `griffelib` | 2.2.0 | ISC |
| `h11` | 0.16.0 | MIT |
| `httpcore2` | 2.12.0 | BSD-3-Clause |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httplib2` | 0.32.0 | MIT |
| `httpx-sse` | 0.4.3 | MIT |
| `httpx2-jsfetch` † | 1.0 | BSD-3-Clause |
| `httpx2` | 2.12.0 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `idna` | 3.19 | BSD-3-Clause |
| `jaraco-classes` | 3.4.0 | MIT |
| `jaraco-context` | 6.1.2 | MIT |
| `jaraco-functools` | 4.6.0 | MIT |
| `jeepney` † | 0.9.0 | MIT |
| `jiter` | 0.16.0 | MIT |
| `jmespath` | 1.1.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `jsonschema` | 4.26.0 | MIT |
| `keyring` | 25.7.0 | MIT |
| `logfire-api` | 4.41.0 | MIT |
| `lxml` | 6.1.2 | BSD-3-Clause |
| `markdown-it-py` | 4.2.0 | MIT |
| `mcp` | 1.29.0 | MIT |
| `mdurl` | 0.1.2 | MIT |
| `more-itertools` | 11.1.0 | MIT |
| `multidict` | 6.9.1 | Apache-2.0 |
| `nh3` | 0.3.7 | MIT |
| `oauthlib` | 4.0.0 | BSD-3-Clause |
| `openai` | 3.28.0 | Apache-2.0 |
| `opentelemetry-api` | 1.44.0 | Apache-2.0 |
| `pillow` | 12.3.0 | MIT-CMU |
| `propcache` | 0.5.2 | Apache-2.0 |
| `proto-plus` | 1.28.3 | Apache-2.0 |
| `protobuf` | 7.36.0 | BSD-3-Clause |
| `psycopg-binary` | 3.3.4 | LGPL-3.0-only |
| `psycopg-pool` | 3.3.3 | LGPL-3.0-only |
| `psycopg` | 3.3.4 | LGPL-3.0-only |
| `pyasn1-modules` | 0.4.2 | BSD-2-Clause |
| `pyasn1` | 0.6.4 | BSD-2-Clause |
| `pycparser` | 3.0 | BSD-3-Clause |
| `pydantic-ai-slim` | 2.55.0 | MIT |
| `pydantic-core` | 2.46.4 | MIT |
| `pydantic-graph` | 2.55.0 | MIT |
| `pydantic-settings` | 2.15.0 | MIT |
| `pydantic` | 2.13.4 | MIT |
| `pyjwt` | 2.15.1 | MIT |
| `pymupdf` | 1.28.2 | AGPL-3.0 OR Artifex commercial |
| `pyparsing` | 3.3.2 | MIT |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause |
| `python-docx` | 1.2.0 | MIT |
| `python-dotenv` | 1.2.3 | BSD-3-Clause |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `python-pptx` | 1.0.2 | MIT |
| `pywin32-ctypes` † | 0.2.3 | BSD-3-Clause |
| `pywin32` † | 312 | PSF-2.0 |
| `pyyaml` | 6.0.3 | MIT |
| `referencing` | 0.37.0 | MIT |
| `requests-oauthlib` | 2.0.0 | ISC |
| `requests` | 2.34.2 | Apache-2.0 |
| `rpds-py` | 2026.6.3 | MIT |
| `s3transfer` | 0.19.2 | Apache-2.0 |
| `secretstorage` † | 3.5.0 | BSD-3-Clause |
| `six` | 1.17.0 | MIT |
| `slack-bolt` | 1.30.0 | MIT |
| `slack-sdk` | 3.43.0 | MIT |
| `sniffio` | 1.3.1 | MIT OR Apache-2.0 |
| `sse-starlette` | 3.4.8 | BSD-3-Clause |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `truststore` | 0.10.4 | MIT |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.4 | MIT |
| `tzdata` † | 2026.3 | Apache-2.0 |
| `uritemplate` | 4.2.0 | BSD-3-Clause OR Apache-2.0 |
| `urllib3` | 2.8.0 | MIT |
| `uvicorn` | 0.52.4 | BSD-3-Clause |
| `xlsxwriter` | 3.2.9 | BSD-2-Clause |
| `yarl` | 1.24.5 | Apache-2.0 |

## Bundled into the console

The backoffice is built from `web/` and served from the image. Its runtime packages:

| Package | Version | License |
|---|---|---|
| `@fontsource-variable/inter` | 5.3.0 | OFL-1.1 |
| `@fontsource-variable/jetbrains-mono` | 5.3.0 | OFL-1.1 |
| `@fontsource-variable/newsreader` | 5.3.0 | OFL-1.1 |
| `@tanstack/react-query` | 5.104.0 | MIT |
| `class-variance-authority` | 0.7.1 | Apache-2.0 |
| `clsx` | 2.1.1 | MIT |
| `cmdk` | 1.1.1 | MIT |
| `get-nonce` | 1.0.1 | MIT |
| `graphology` | 0.26.0 | MIT |
| `graphology-layout-forceatlas2` | 0.10.1 | MIT |
| `lucide-react` | 1.48.0 | ISC |
| `motion` | 13.4.4 | MIT |
| `radix-ui` | 1.6.7 | MIT |
| `react` | 19.3.0 | MIT |
| `react-dom` | 19.3.0 | MIT |
| `sigma` | 3.0.3 | MIT |
| `tailwind-merge` | 3.7.0 | MIT |

The three `@fontsource-variable` packages carry the Inter, JetBrains Mono, and Newsreader fonts
under the SIL Open Font License 1.1, which allows bundling them with any software.

## Tools, not dependencies

[`gitleaks`](https://github.com/gitleaks/gitleaks) (MIT) scans the repository for secrets in CI.
Development tools (`pytest`, `ruff`, `vite`, `vitest`, `typescript`, and their dependencies) are
used to build and test the project and are not shipped in the image.
