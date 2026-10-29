# Horae container image (#40). Multi-stage: the builder holds uv and the resolved virtualenv, the
# final stage holds only the interpreter and that virtualenv (no uv, no pip, no compiler).
#
# Runtime contract (docs/operations/container-image.md):
#   - runs as the numeric, non-root user 10001:10001
#   - works with `docker run --read-only`: the only writable places are /instance (mount a
#     volume there: config, lock, plugins, state, vault) and a tmpfs on /tmp
#   - no network is needed to start; no secret is baked in
#   - /opt/venv (the interpreter `horae init` pins the built-in plugins to) and /instance are
#     stable absolute paths: instances created by one image keep working with the next
#   - PID 1 is tini, which reaps the plugin processes and forwards signals

# Base images are pinned by digest (the multi-arch index digest). Renovate bumps the digest and the
# tag together. Both stages must use the same Python so the virtualenv's interpreter link is valid.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
# Dependencies first, from the lockfile only: this layer is reused until uv.lock changes.
# The lock also holds the workspace member horae-sdk (sdk/), which is only a dev dependency of the
# core: with --frozen uv reads the lock as it is, never the members' pyproject.toml, and --no-dev
# leaves the SDK out, so sdk/ is not part of the build context (.dockerignore). The smoke test
# checks the SDK is not in the image.
COPY pyproject.toml uv.lock ./
# The uv cache is a BuildKit cache mount: kept between builds, never part of a layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# Then the project itself, installed non-editable so the final stage needs no source tree.
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="horae" \
      org.opencontainers.image.description="Horae, a predictable self-hosted personal agent" \
      org.opencontainers.image.source="https://github.com/horae-dev/horae" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# tini as PID 1 (Debian package, pinned; Renovate does not bump apt pins: bump it with the base
# image). No package manager is needed at run time: drop pip and friends, and ensurepip, which
# bundles a pip wheel (they are what could pull and run code in a compromised container). The
# venv has no pip of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini=0.19.0-1+b3 \
    && rm -rf /var/lib/apt/lists/* \
    && (python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true) \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
              /usr/local/lib/python3.12/site-packages/pip* \
              /usr/local/lib/python3.12/site-packages/setuptools* \
              /usr/local/lib/python3.12/site-packages/wheel* \
              /usr/local/bin/pip* \
    && groupadd --gid 10001 horae \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
               --shell /usr/sbin/nologin horae \
    && mkdir /instance \
    && chown 10001:10001 /instance \
    && chmod 0700 /instance

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    HORAE_INSTANCE=/instance

# The instance directory (horae.yaml, horae.lock, pipelines/, plugins/, state/, vault/) is the
# one writable place: mount a named volume (`-v horae:/instance`, which starts owned by 10001) or
# a bind mount owned by 10001. No VOLUME instruction: an anonymous volume created behind the
# user's back would hide a forgotten `-v` until the container is removed with its data.
# /tmp is expected to be a tmpfs.
WORKDIR /instance
USER 10001:10001

# `horae --version` is the only check that is meaningful before the HTTP API exists; #37 replaces
# it with a /healthz probe.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD ["horae", "--version"]

ENTRYPOINT ["tini", "--", "horae"]
CMD ["--help"]
