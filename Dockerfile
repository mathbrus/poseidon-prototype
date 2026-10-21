# One recipe for every Poseidon image; `deploy/build-images.sh` holds the
# matrix — which workspace package, which source packages, which extra,
# which entrypoint — and is what kind-up.sh and CI build from.
#
# Lockfile-pinned: third-party dependencies come from uv.lock via
# `uv export`, so images and the host resolve identical versions. The
# workspace's pyproject files are copied first, alone, so the dependency
# layer caches across source edits; the sources are installed without
# deps on top of it, in a build stage whose only export is the installed
# tree — the final image carries no source checkout, no uv, no build cache.
#
# Build from the repo root, e.g.
#   docker build -t poseidon-mcp-gateway \
#     --build-arg PACKAGE=gateway --build-arg SRC="common registry gateway" \
#     --build-arg APT=git \
#     --build-arg ENTRYPOINT="uvicorn --factory mcp_gateway.app:create_app --host 0.0.0.0 --port 8080" .
ARG BASE=python:3.12-slim

# The workspace's dependency declarations and nothing else — what the
# dependency layer is keyed on, so a source edit does not rebuild it.
FROM ${BASE} AS manifests
COPY packages /packages
RUN find /packages -mindepth 2 -maxdepth 2 ! -name pyproject.toml -exec rm -rf {} +

FROM ${BASE} AS build
ARG PACKAGE
ARG SRC
ARG EXTRA=""
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY --from=manifests /packages packages
RUN uv export --frozen --no-dev --no-emit-workspace --no-hashes \
      --package "$PACKAGE" ${EXTRA:+--extra "$EXTRA"} -o /tmp/requirements.txt \
    && pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt
COPY packages packages
RUN pip install --no-cache-dir --no-deps --prefix=/install \
      $(for package in $SRC; do printf './packages/%s ' "$package"; done)

FROM ${BASE}
ARG APT=""
ARG WORKSPACE=""
ARG ENTRYPOINT
RUN if [ -n "$APT" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends $APT \
      && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=build /install /usr/local
# Non-root runtime user; pod specs set runAsNonRoot/runAsUser to match
# (common/podsecurity.py).
RUN useradd --create-home --uid 10001 poseidon
# The sandbox image alone runs workspace code: it gets a writable
# workspace the user owns (tar delivery, git worktrees and test runs all
# work without root; pip installs from workspace code fall back to --user)
# and a git identity for its local commits — no credentials involved.
RUN if [ -n "$WORKSPACE" ]; then \
      mkdir -p "$WORKSPACE" && chown poseidon:poseidon "$WORKSPACE" \
      && git config --system user.name "poseidon-sandbox" \
      && git config --system user.email "sandbox@poseidon.local" \
      && git config --system --add safe.directory "$WORKSPACE"; \
    fi
# The OpenHands SDK is env-gated quiet: no banner, no Laminar telemetry
# (LMNR_* absent). Harmless on the images that do not carry it.
ENV OPENHANDS_SUPPRESS_BANNER=1
# The entrypoint is a build argument, exec'd so the service is PID 1 and
# receives the pod's signals; Kubernetes `args` append to it.
RUN printf '#!/bin/sh\nexec %s "$@"\n' "$ENTRYPOINT" > /usr/local/bin/poseidon-entrypoint \
    && chmod +x /usr/local/bin/poseidon-entrypoint
WORKDIR ${WORKSPACE:-/opt/poseidon}
# The version this image answers to (common.version): the tree's
# `git describe`, passed by deploy/build-images.sh; empty for a build
# outside git. Declared after every RUN so a new commit changes only
# the image's config, never its layers.
ARG VERSION=""
ENV POSEIDON_VERSION=${VERSION}
LABEL org.opencontainers.image.version=${VERSION}
USER 10001
ENTRYPOINT ["poseidon-entrypoint"]
