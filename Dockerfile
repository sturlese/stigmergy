# The ONE image, serving all THREE process groups of the one Fly app:
#
#   app     stigmergy-server's HTTP transport
#   worker  stigmergy-librarian — the fast lane's back half
#   slack   stigmergy-slack — the Slack transport (Socket Mode)
#
# One image and not three, for the reason there is one app: one build, one deploy, one place to
# read logs, and no way for the three halves to drift onto different code. The cost is that the
# server's container carries the worker's toolchain (git, gitleaks, Node, the Claude Code CLI) —
# accepted for the pilot and named in the spec's risks as image bloat, with every version pinned
# so a rebuild is reproducible. The Slack transport adds no new toolchain (`slack-bolt`/`aiohttp`
# are pure-Python wheels, installed the same way every other dependency is below).
#
# Secrets (OPENAI_API_KEY, STIGMERGY_INDEX_DSN, STIGMERGY_TOKEN_STORE, the R2 group, the librarian
# GitHub App triple, ANTHROPIC_API_KEY) are Fly secrets injected at runtime — NONE of them are
# baked into this image or checked into this repo. `stigmergy-librarian-boot` strips the read path's
# key back out before exec'ing the worker (see `librarian/bootstrap.py`).
#
# `deploy/identities.json` is populated by `scripts/deploy_staging.sh` from your knowledge-repo
# checkout just before `fly deploy` runs. The versions COMMITTED here are empty defaults, so a
# fresh clone can build; the deploy script restores them on the way out so a real roster never
# outlives a deploy (`tests/test_deploy_defaults.py`). This repo never stores the knowledge repo's
# content (README.md). The librarian e2e writes placeholders before building when they are absent,
# because both roles share this one image and the COPY below is unconditional.
#
# Base image pinned by digest, linux/amd64 (Fly's platform) — resolved via
# `docker manifest inspect python:3.12-slim` on 2026-07-20. Re-resolve the same way for a
# deliberate base-image bump; do not float the tag back unpinned.

# ── stage 1: the worker's third-party toolchain, pinned and checksum-verified ──────────────────
# A separate stage so `curl`, `xz-utils` and the downloaded archives never reach the runtime image
# — the final image gets the extracted binaries and nothing that fetched them.
FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464 AS toolchain

# The architecture is read from the BASE IMAGE, not from BuildKit's `TARGETARCH`, and that is a
# correction rather than a preference. The digest above pins linux/amd64 (Fly's platform), so this
# image is amd64 wherever it is built — on an arm64 Mac it simply runs under emulation. `TARGETARCH`
# reports the BUILD HOST's platform, so on that Mac it says `arm64` and the build cheerfully
# downloaded arm64 binaries into an amd64 container: `node` was there, and exec'ing it answered
# "No such file or directory". `dpkg --print-architecture` asks the image what it actually is,
# which is the only source that cannot disagree with the digest.
ARG GITLEAKS_VERSION=8.30.1
ARG NODE_VERSION=24.18.0
# The Agent SDK drives the Claude Code CLI as a subprocess (see librarian/agent.py), so the CLI is
# part of the runtime the `sdk` backend needs — pinned exactly, for the same reason
# `claude-agent-sdk` is pinned exactly in pyproject.toml: a minor bump can change what the agent is
# allowed to do between CI and staging. Its `engines` requires Node >= 22, which is why Node comes
# from nodejs.org rather than from Debian (bookworm ships 18).
ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/docker/tool-checksums.txt /tmp/tool-checksums.txt

WORKDIR /tmp/dl
RUN set -eux; \
    debarch="$(dpkg --print-architecture)"; \
    case "${debarch}" in \
      amd64) arch=x64 ;; \
      arm64) arch=arm64 ;; \
      *) echo "unsupported image architecture ${debarch}: add its digests to scripts/docker/tool-checksums.txt first" >&2; exit 1 ;; \
    esac; \
    gitleaks_tar="gitleaks_${GITLEAKS_VERSION}_linux_${arch}.tar.gz"; \
    node_tar="node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    curl -fsSL -o "${gitleaks_tar}" \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${gitleaks_tar}"; \
    curl -fsSL -o "${node_tar}" \
      "https://nodejs.org/dist/v${NODE_VERSION}/${node_tar}"; \
    sha256sum --ignore-missing -c /tmp/tool-checksums.txt; \
    tar -xzf "${gitleaks_tar}" -C /usr/local/bin gitleaks; \
    mkdir -p /opt/node; \
    tar -xJf "${node_tar}" -C /opt/node --strip-components=1; \
    rm -rf /tmp/dl

ENV PATH="/opt/node/bin:${PATH}"
# Installed into the same prefix so the whole Node runtime — interpreter, npm and the CLI — copies
# into the runtime image as one directory.
#
# Fetched and checksum-verified like gitleaks and Node above, rather than pulled straight from the
# registry by name. Otherwise the asymmetry is real: the version would be pinned and the bytes
# would not, so a rebuild would trust whatever the registry served for that version.
#
# `--ignore-scripts`, and then the package's own postinstall BY NAME. The flag alone would ship a
# broken image, which is why it is not the whole fix: the `bin/claude.exe` inside the tarball is a
# 500-byte stub whose entire body prints "Either postinstall did not run (--ignore-scripts...)", and
# `install.cjs` is what replaces it with the native binary from the matching optional dependency. So
# the flag buys exactly what it is for — nothing npm resolves gets to run arbitrary code during the
# build — and the one script that must run is invoked explicitly, from the tarball verified two
# lines above, and does no network I/O at all (it hardlinks a file that is already on disk).
#
# The size assertion is the proof, and it is here because a silent version of this failure is the
# expensive one: a stub-sized `claude` means every `sdk` run fails at the agent, on staging, one
# item at a time. `-gt 1048576` distinguishes the 500-byte stub from the ~500MB binary with room for
# either to change by orders of magnitude.
#
# Residual, named rather than left to be found: the optional dependency carrying that native binary
# is pinned by version and by npm's own registry integrity metadata, not by a digest in this repo.
# It is a per-platform package, so pinning it means a line per architecture in tool-checksums.txt.
RUN set -eux; \
    mkdir -p /tmp/dl; cd /tmp/dl; \
    tarball="claude-code-${CLAUDE_CODE_VERSION}.tgz"; \
    curl -fsSL -o "${tarball}" \
      "https://registry.npmjs.org/@anthropic-ai/claude-code/-/${tarball}"; \
    sha256sum --ignore-missing -c /tmp/tool-checksums.txt; \
    npm install -g --ignore-scripts --no-fund --no-audit "./${tarball}"; \
    cd /; rm -rf /tmp/dl; \
    pkg="$(npm root -g)/@anthropic-ai/claude-code"; \
    node "${pkg}/install.cjs"; \
    [ "$(wc -c < "${pkg}/bin/claude.exe")" -gt 1048576 ] || \
      { echo "the claude CLI is still the postinstall stub: the native binary for this platform was not placed, so every sdk run would fail at the agent" >&2; exit 1; }; \
    npm cache clean --force

# ── stage 2: the runtime both process groups share ────────────────────────────────────────────
FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

WORKDIR /app

# `git` is the worker's substrate, not a convenience: it clones the knowledge repo, adds a
# throwaway worktree per capture, commits and pushes. `ca-certificates` for the https remote.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git poppler-utils \
 && rm -rf /var/lib/apt/lists/*
# `poppler-utils` (pdftotext) is the drive flow's text-layer hand (ADR 028 D4): it runs
# AT THE WORKER, over the evidence blob.

COPY --from=toolchain /usr/local/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=toolchain /opt/node /opt/node
ENV PATH="/opt/node/bin:${PATH}"

# `README.md` and the two licence files are here because `pyproject.toml` NAMES them (`readme`,
# `license-files`): hatchling reads them while generating metadata, so a build context missing any
# one of them fails at `pip install` with `Readme file does not exist` — before a single line of
# this project's own code runs. `tests/test_deployment_config.py` pins the correspondence, because
# the local gate (`make test`) never builds this image and so cannot notice.
#
# THIRD-PARTY-LICENSES.md also has to travel INSIDE the image on its own merits: this image bundles
# psycopg, which is LGPL-3.0-only, and §4/§6 wants the dependency notices shipped with the artifact.
COPY pyproject.toml README.md LICENSE THIRD-PARTY-LICENSES.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The ops files baked into the image, in the state the build context has them.
#
# `deploy/` is COMMITTED, holding empty defaults, so that a fresh clone can build this image with
# no setup at all. `scripts/deploy_staging.sh` overwrites every one of them from the knowledge repo's own
# `ops/` immediately before a deploy — so a real deployment bakes real data and a bare `docker
# build` bakes nothing. `tests/test_deploy_defaults.py` fails if a real bake is ever committed.
#
# The empty defaults are each the safe end of their own behaviour: no identities means the server
# refuses to serve rather than serving an open brain; an empty registry means the graph works
# unregistered; an empty channel map means every audience falls back to posting nowhere.
COPY deploy/identities.json /app/identities.json
COPY deploy/entity-registry.json /app/entity-registry.json
COPY deploy/slack-channels.json /app/slack-channels.json
COPY deploy/stewards.json /app/stewards.json

# Non-root. The SERVER never needs to write anywhere in the image; the WORKER does — it
# keeps its knowledge-repo clone and the Claude Code CLI's own configuration directory under the
# app user's home, which is why that home exists and is owned by it. Worktrees and the materialized
# base inputs go to $TMPDIR, which is writable for everyone.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# Where `stigmergy-librarian-boot` clones to. In the image rather than only in fly.toml so the
# composition and staging inherit one default, and so a `docker run` of the worker needs one
# environment value (the repo URL) instead of two.
ENV STIGMERGY_REPO=/home/app/knowledge

EXPOSE 8080

# The default command is the SERVER: `fly.toml`'s [processes] names every group explicitly and
# overrides this, and a plain `docker run` of this image still starts the server.
CMD ["stigmergy-server", "--transport", "http", "--host", "0.0.0.0", "--port", "8080", \
     "--identities", "/app/identities.json", \
     "--entity-registry", "/app/entity-registry.json", \
     "--stewards", "/app/stewards.json"]
