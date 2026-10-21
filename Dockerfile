# syntax=docker/dockerfile:1

# ---- CSS build: Tailwind v4 standalone CLI + daisyUI (same versions as flake.nix) ----
# Base images are pinned by digest and every download is checked against a
# sha256, so a re-pushed tag or a tampered release asset fails the build
# instead of running. Renovate bumps the versions/digests; after a
# Tailwind/daisyUI bump run `uv run invoke update-asset-hashes` (see NOTES.md).
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8 AS css
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
# renovate: datasource=github-releases depName=tailwindlabs/tailwindcss
ARG TAILWIND_VERSION=v4.3.3
ARG TAILWIND_SHA256_AMD64=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
ARG TAILWIND_SHA256_ARM64=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195
# renovate: datasource=github-releases depName=saadeghi/daisyui
ARG DAISYUI_VERSION=5.7.28
ARG DAISYUI_SHA256=f5085284bb5622a9bafcda9108a149b063bca73cab5eede5f0f17a5006e02847
ARG DAISYUI_THEME_SHA256=c915fae9cc53c1a80ce8251cd1edea635c5b3c640c0afdc5269340ec03c687f3
WORKDIR /build
COPY assets ./assets
# Tailwind scans these for class names (assets/app.css has `@source
# "../giveaway_quest/templates"`); without them every utility is purged and the
# built stylesheet has daisyUI's base but none of the layout classes.
COPY giveaway_quest/templates ./giveaway_quest/templates
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) tw_arch=linux-x64; tw_sha="$TAILWIND_SHA256_AMD64" ;; \
        arm64) tw_arch=linux-arm64; tw_sha="$TAILWIND_SHA256_ARM64" ;; \
        *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${tw_arch}"; \
    echo "${tw_sha}  /usr/local/bin/tailwindcss" | sha256sum -c -; \
    chmod +x /usr/local/bin/tailwindcss; \
    mkdir -p assets/vendor; \
    daisy_url="https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}"; \
    curl -fsSL -o assets/vendor/daisyui.mjs "${daisy_url}/daisyui.mjs"; \
    curl -fsSL -o assets/vendor/daisyui-theme.mjs "${daisy_url}/daisyui-theme.mjs"; \
    printf '%s  %s\n' \
        "$DAISYUI_SHA256" assets/vendor/daisyui.mjs \
        "$DAISYUI_THEME_SHA256" assets/vendor/daisyui-theme.mjs \
        | sha256sum -c -; \
    mkdir -p out; \
    tailwindcss -i assets/app.css -o out/app.css --minify

# ---- Python runtime ----
FROM python:3.13-slim@sha256:98ce6fb13b3803744fbca11434f00818f1e300e4e48a4f204e07b2c928b6218e AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv
# uv does all installing, so drop the base image's pip (and ensurepip's spare
# wheel): its vendored certifi/urllib3/msgpack lag behind and are what Trivy
# kept flagging, while nothing at runtime uses them.
RUN python -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled \
    && useradd --system --create-home --uid 10001 giveaway
WORKDIR /app

# Dependencies first so they cache independently of app code changes.
# --locked: fail if uv.lock is out of date rather than silently re-resolving
# to versions nobody reviewed.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY giveaway_quest ./giveaway_quest
COPY README.md ./
COPY --from=css /build/out/app.css ./giveaway_quest/static/app.css
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    GQ_DATA_DIR=/data

RUN mkdir -p /data && chown giveaway:giveaway /data
USER giveaway

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/robots.txt', timeout=3)" || exit 1

CMD ["gq", "serve", "--host", "0.0.0.0", "--port", "8000"]
