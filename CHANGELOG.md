# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below `1.0.0`, the public contracts may still move between minor releases.
The current product contract is [`specs/karpathy-team-wiki.md`](./specs/karpathy-team-wiki.md), the
runtime map is [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md), and the executable code and tests
remain authoritative.

## [Unreleased]

### Changed

- The librarian's prompt now puts what changes least first: the catalog, oldest page first, then
  the pages chosen for the capture, its provenance and source, and the task last. Providers reuse
  the cached prefix, so a filing correction pays a fraction of its input.

## [0.12.0] - 2026-10-10

First public release, under the GNU Affero General Public License 3.0 with a contributor license
agreement ([`CLA.md`](./CLA.md)).

### Added

- A librarian that files every capture (text, files, images through OCR, public URLs, private
  Google Drive documents through the local bridge, Slack threads) into a Git repository of
  Markdown pages: notes, concepts, entities, and projects, linked by wikilinks, with immutable
  sources and a citation on every claim.
- One serialized writer per deployment with deterministic gates on every write: provenance,
  access layers, entity names, links, preservation of cited lines, and an exact patch and change
  record per commit.
- Audience layers: a capture files in exactly one layer, readers see the union of their layers,
  and a whole layer can be erased with its history.
- Cited answers (`ask`), search, listing, page reads, and entity descriptions over MCP, with a
  hybrid lexical and vector index in Postgres.
- Several workspaces per deployment under forced row-level security, a master console for
  operators, a backoffice per workspace, workspace suspension, deletion, and refiling.
- Gardening that repairs links and aliases on a schedule, nightly index reconciliation, and a
  real-model evaluation suite with a filing parity gate.
- A self-hosting guide, [`docs/SELF-HOSTING.md`](./docs/SELF-HOSTING.md), that takes a new operator
  from empty Fly.io, Postgres, bucket, GitHub, and OpenRouter accounts to a first cited answer.
- The bare host redirects to the backoffice sign-in. The platform ships no landing page: an
  operator may build its own into the image (`web/landing.html` and `web/src/landing/`, ignored by
  git) and serve it with `STIGMERGY_LANDING=on`.
- An explicit parity gate opt-out for an operator deploying a released version:
  `STIGMERGY_PARITY_GATE=off make deploy-staging`.