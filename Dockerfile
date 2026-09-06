# syntax=docker/dockerfile:1

# ---- CSS build: Tailwind v4 standalone CLI + daisyUI (same versions as flake.nix) ----
FROM debian:bookworm-slim AS css
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
ARG DAISYUI_VERSION=5.7.28
# "latest" tracks whatever Tailwind v4.x is newest at build time. For a fully
# reproducible build, pin a tag from https://github.com/tailwindlabs/tailwindcss/releases
# and pass it as --build-arg TAILWIND_VERSION=v4.x.y.
ARG TAILWIND_VERSION=latest
WORKDIR /build
COPY assets ./assets
# Tailwind scans these for class names (assets/app.css has `@source
# "../giveaway_quest/templates"`); without them every utility is purged and the
# built stylesheet has daisyUI's base but none of the layout classes.
COPY giveaway_quest/templates ./giveaway_quest/templates
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) tw_arch=linux-x64 ;; \
        arm64) tw_arch=linux-arm64 ;; \
        *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    if [ "$TAILWIND_VERSION" = "latest" ]; then \
        tw_url="https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-${tw_arch}"; \
    else \
        tw_url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${tw_arch}"; \
    fi; \
    curl -fsSL -o /usr/local/bin/tailwindcss "$tw_url" && chmod +x /usr/local/bin/tailwindcss; \
    mkdir -p assets/vendor; \
    curl -fsSL -o assets/vendor/daisyui.mjs \
        "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui.mjs"; \
    curl -fsSL -o assets/vendor/daisyui-theme.mjs \
        "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui-theme.mjs"; \
    mkdir -p out; \
    tailwindcss -i assets/app.css -o out/app.css --minify

# ---- Python runtime ----
FROM python:3.13-slim AS runtime
RUN pip install --no-cache-dir uv \
    && useradd --system --create-home --uid 10001 giveaway
WORKDIR /app

# Dependencies first so they cache independently of app code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY giveaway_quest ./giveaway_quest
COPY README.md ./
COPY --from=css /build/out/app.css ./giveaway_quest/static/app.css
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    GQ_DATA_DIR=/data

RUN mkdir -p /data && chown giveaway:giveaway /data
USER giveaway

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/robots.txt', timeout=3)" || exit 1

CMD ["gq", "serve", "--host", "0.0.0.0", "--port", "8000"]
