# The ONE image, serving all THREE process groups of the one Fly app:
#
#   app     stigmergy-server's HTTP transport
#   worker  stigmergy-librarian — the fast lane's back half
#   slack   stigmergy-slack — the Slack transport (Socket Mode)
#
# One image and not three, for the reason there is one app: one build, one deploy, one place to
# read logs, and no way for the three halves to drift onto different code. The cost is that the
# server's container carries the worker's toolchain (git, gitleaks, poppler) — accepted for the
# pilot and named in the spec's risks as image bloat, with every version pinned so a rebuild is
# reproducible. That toolchain is much smaller than it was: retiring the harness-driven filing
# backend took the Node runtime and its ~500MB agent CLI out of this image entirely, so the bloat
# risk is now mostly the Python wheels. The Slack transport adds no new toolchain (`slack-bolt`/`aiohttp`
# are pure-Python wheels, installed the same way every other dependency is below).
#
# Secrets (OPENAI_API_KEY, STIGMERGY_INDEX_DSN, STIGMERGY_TOKEN_STORE, the R2 group, the librarian
# GitHub App triple, ANTHROPIC_API_KEY — now the filing backend's PROVIDER key rather than a CLI's
# credential, same variable) are Fly secrets injected at runtime — NONE of them are
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
# A separate stage so `curl` and the downloaded archive never reach the runtime image — the final
# image gets the extracted binary and nothing that fetched it.
#
# ONE binary now, where there were two plus an npm package: `gitleaks`. The Node runtime and the
# agent CLI it hosted left with the backend that drove them — see
# docs/decisions/033-structured-filing-flow.md. (`xz-utils` went with Node, whose tarball was the
# only `.xz` this build ever fetched.)
FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464 AS toolchain

# The architecture is read from the BASE IMAGE, not from BuildKit's `TARGETARCH`, and that is a
# correction rather than a preference. The digest above pins linux/amd64 (Fly's platform), so this
# image is amd64 wherever it is built — on an arm64 Mac it simply runs under emulation. `TARGETARCH`
# reports the BUILD HOST's platform, so on that Mac it says `arm64` and the build cheerfully
# downloaded arm64 binaries into an amd64 container: the binary was there, and exec'ing it answered
# "No such file or directory". `dpkg --print-architecture` asks the image what it actually is,
# which is the only source that cannot disagree with the digest. (The binary that failed that way
# was the retired toolchain's; the rule is the download's, not that binary's, and `gitleaks` is
# fetched per-architecture in exactly the same way.)
ARG GITLEAKS_VERSION=8.30.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
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
    curl -fsSL -o "${gitleaks_tar}" \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${gitleaks_tar}"; \
    sha256sum --ignore-missing -c /tmp/tool-checksums.txt; \
    tar -xzf "${gitleaks_tar}" -C /usr/local/bin gitleaks; \
    rm -rf /tmp/dl

# ── stage 2: the runtime both process groups share ────────────────────────────────────────────
FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

WORKDIR /app

# `git` is the worker's substrate, not a convenience: it clones the knowledge repo, adds a
# throwaway worktree per capture, commits and pushes. `ca-certificates` for the https remote.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git poppler-utils \
 && rm -rf /var/lib/apt/lists/*
# `poppler-utils` is the drive flow's document hand (ADR 028 D4), run AT THE WORKER over the
# evidence blob: `pdftotext` extracts the text layer, and `pdftoppm`/`pdfinfo` from the same
# package rasterize pages for a provider-prefixed vision OCR (`kernel/converters.py`).

COPY --from=toolchain /usr/local/bin/gitleaks /usr/local/bin/gitleaks

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

# Non-root. The SERVER never needs to write anywhere in the image; the WORKER does — it keeps its
# knowledge-repo clone under the app user's home, which is why that home exists and is owned by it.
# (It also held the retired agent CLI's own configuration directory; the clone alone is reason
# enough for the home.) Worktrees and the materialized base inputs go to $TMPDIR,
# which is writable for everyone.
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
