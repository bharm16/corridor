# Corridor's deployable image: one image, three roles.
#
# Web, batch and migration run the same bytes with different commands, because
# nothing in the repository has a separate build context. `workers/render` is
# not a service -- `src/corridor/render_profiles.py` shells out to it with
# `uv run --project workers/render --frozen`, from inside this same tree -- so
# the render environment has to be present here, not in a second image.
#
# Both uv projects are synced at build time. Nothing may reach the network at
# run time: the first page render used to build the render environment on
# demand, and doing that inside a running task would be slow, silent and
# unreliable.

# Pinned by digest for the same reason as uv below: a moved tag changes the
# Python and OS bytes that later run with the database credential and the
# task roles, with no change in this repository to review.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base

# `uv` is not merely a build tool here: the render subprocess invokes it at
# run time, so its version is part of the runtime contract.
#
# Pinned by digest, not by tag. A tag is a mutable pointer: if 0.10.3 is
# moved or the registry account is compromised, these bytes change without
# any change here -- and they execute during both sync steps, in the image
# that later receives the migration credential and the task roles. The
# surrounding lockfiles do not cover the tool that reads them.
COPY --from=ghcr.io/astral-sh/uv:0.10.3@sha256:7a88d4c4e6f44200575000638453a5a381db0ae31ad5c3a51b14f8687c9d93a3 \
     /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/opt/corridor/uv-cache

# libpango/libcairo/libgdk-pixbuf/libffi  WeasyPrint's native stack; it fails
#                  at import without them, not at first render.
# ca-certificates  needed to fetch the RDS trust bundle below.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        libcairo2 \
        libffi8 \
        libgdk-pixbuf-2.0-0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/corridor

# The RDS certificate bundle the entrypoint pins with sslmode=verify-full.
# Baked into the image so a task never depends on fetching it at start-up.
RUN curl -fsSL --retry 3 \
      https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
      -o /opt/corridor/rds-global-bundle.pem \
    && test -s /opt/corridor/rds-global-bundle.pem

# Dependencies first, so a source-only change does not re-resolve them.
COPY pyproject.toml uv.lock ./
COPY workers/render/pyproject.toml workers/render/uv.lock ./workers/render/
RUN uv sync --locked --no-install-project --no-dev \
    && uv sync --project workers/render --frozen --no-dev

# The render subprocess resolves `workers/render` relative to the repository
# root, so the tree has to keep its shape.
COPY src/ ./src/
COPY workers/ ./workers/
COPY prompts/ ./prompts/
COPY gold/pdf/v1/render-profiles.json gold/pdf/v1/render-profile-measurement.json ./gold/pdf/v1/
COPY docs/operations/openai-responses-provider-posture.md ./docs/operations/
COPY scripts/container_entrypoint.py ./scripts/
COPY alembic.ini ./
RUN uv sync --locked --no-dev

ENV PATH="/opt/corridor/.venv/bin:${PATH}" \
    PYTHONPATH=/opt/corridor/src

# The content-addressed store's root and the page-image directory must exist,
# not merely be configurable: LocalFilesystemStore.probe treats a missing root
# as unreadable rather than empty, so /readyz answers 503 without them. They
# are needed under the s3 backend too -- config.py keeps corpus_store as the
# local staging directory the object store fills on demand.
RUN mkdir -p /opt/corridor/corpus/files /opt/corridor/out/page-images

# Non-root. The uv cache and both virtualenvs must stay readable, and the
# render subprocess writes page images under a temporary directory it is given.
RUN useradd --system --create-home --uid 10001 corridor \
    && chown -R corridor:corridor /opt/corridor
USER corridor

# The commit this image was built from, so a retried release can prove an
# existing tag is the same image rather than pushing over it.
ARG GIT_REVISION=""
ENV CORRIDOR_CODE_REVISION="$GIT_REVISION"
LABEL org.opencontainers.image.revision="$GIT_REVISION"
LABEL org.opencontainers.image.source="https://github.com/bharm16/corridor"

# Fails closed: the entrypoint refuses to start without a role.
ENV CORRIDOR_TASK_ROLE=""

ENTRYPOINT ["python", "/opt/corridor/scripts/container_entrypoint.py"]
# Overridden per task definition. Named here so `docker run <image>` is the web
# role rather than an error.
CMD ["uvicorn", "corridor.web.app:app", "--host", "0.0.0.0", "--port", "8412"]
