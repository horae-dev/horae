# Horae container image (#40). Multi-stage: the builder holds uv and the resolved virtualenv, the
# final stage holds only the interpreter and that virtualenv (no uv, no pip, no compiler).
#
# Runtime contract (docs/operations/container-image.md):
#   - runs as the numeric, non-root user 10001:10001
#   - works with `docker run --read-only`: the only writable places are the /instance volume
#     (config, state, notes) and a tmpfs on /tmp
#   - no network is needed to start; no secret is baked in

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
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Then the project itself, installed non-editable so the final stage needs no source tree.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="horae" \
      org.opencontainers.image.description="Horae, a predictable self-hosted personal agent" \
      org.opencontainers.image.source="https://github.com/horae-dev/horae" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# No package manager is needed at run time: drop pip and friends from the interpreter (they are
# what could pull and run code in a compromised container). The venv has its own copy of nothing.
RUN python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true; \
    rm -rf /usr/local/lib/python3.12/site-packages/pip* \
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

# The instance directory (horae.yaml, pipelines/, state/, secrets/, notes) is the one writable
# volume; mount a named volume or a bind mount owned by 10001. /tmp is expected to be a tmpfs.
WORKDIR /instance
VOLUME ["/instance"]
USER 10001:10001

# `horae --version` is the only check that is meaningful before the HTTP API exists; #37 replaces
# it with a /healthz probe.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD ["horae", "--version"]

ENTRYPOINT ["horae"]
CMD ["--help"]
