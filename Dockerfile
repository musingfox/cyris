# syntax=docker/dockerfile:1
# wrangler: HTML digest publish runs `wrangler pages deploy` (needs
# CLOUDFLARE_API_TOKEN at runtime). Baked in, so a digest-time deploy fetches
# nothing. Pinned; keep in sync with publish.py WRANGLER.
#
# Node, not bun: under bunx, wrangler intermittently exited 0 mid-deploy with
# its output truncated at an arbitrary point — banner only, or partway through
# "Uploading... (34/35)". Pinning the wrangler version (4.121.0 → 4.122.0) did
# not stop it; the constant across every occurrence was the bun runtime, which
# wrangler does not support. That silent no-op is what cost the 2026-08-18
# evening and 2026-08-20 morning digests their Discord links.
FROM node:22-slim AS wrangler
RUN npm install -g wrangler@4.122.0

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

COPY --from=wrangler /usr/local/bin/node /usr/local/bin/node
COPY --from=wrangler /usr/local/lib/node_modules/wrangler /usr/local/lib/node_modules/wrangler
RUN ln -s /usr/local/lib/node_modules/wrangler/bin/wrangler.js /usr/local/bin/wrangler

# supercronic: container-aware cron — logs to stdout, respects TZ + inherits env vars
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.47
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) SHA=dcb1403c188a9438c47d4bba82a9c357fc9351ce91627fb2bae627f0f5becfc4 ;; \
      arm64) SHA=e1124aa34294e2bb8ab7002f347f4363ba35097f3daf4d3c44e9d813c1fb2bb8 ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${SHA}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# deps layer — cached unless manifest/lock change
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY docker/crontab /app/crontab

# supercronic fires crontab at container-local clock times (set TZ via compose)
CMD ["supercronic", "/app/crontab"]
