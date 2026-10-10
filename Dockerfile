FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

FROM node:24-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS web

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

RUN sed -i \
      -e 's|URIs: http://deb.debian.org/debian$|URIs: https://snapshot.debian.org/archive/debian/20260713T000000Z|' \
      -e 's|URIs: http://deb.debian.org/debian-security$|URIs: https://snapshot.debian.org/archive/debian-security/20260713T000000Z|' \
      -e '/^Signed-By:/a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates=20250419 \
      git=1:2.47.3-0+deb13u1 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md LICENSE CLA.md THIRD-PARTY-LICENSES.md ./
COPY src ./src
COPY --from=web /build/src/stigmergy/admin/static ./src/stigmergy/admin/static
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

# The writer keeps its repository clone under the non-root user's home.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

ENV STIGMERGY_REPO=/home/app/knowledge
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Keep this command byte-identical to fly.toml's app process.
CMD ["stigmergy-server", "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
