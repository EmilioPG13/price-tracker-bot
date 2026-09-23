# syntax=docker/dockerfile:1

# Two stages: uv and the build tooling stay in the first, and the image that runs
# carries only the virtualenv, the migrations and a Python to run them with. Both stages
# must use the same base, because a virtualenv points at the interpreter it was built by.
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv

# Bytecode compiled at build time rather than on the first import after every restart;
# copied rather than hardlinked, since the cache is a mount that does not reach the
# final image; and never a downloaded Python, because the base image is the one to use.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, in a layer of their own: they change far less often than the
# code, so an edit to a handler rebuilds in seconds instead of reinstalling matplotlib.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# README.md because pyproject.toml names it as the package readme, and the build
# backend refuses to build without it. --no-editable installs the package itself into
# the virtualenv, so the final stage needs no copy of src/.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM ${PYTHON_IMAGE}

# Not root: the bot parses pages from the internet, and a parser bug should not be
# able to write anywhere a root process could.
RUN useradd --create-home --uid 10001 bot

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The migrations ship with the code they describe, so an image can bring any database
# up to its own schema: `docker run <image> alembic upgrade head`.
COPY alembic.ini ./
COPY alembic ./alembic

# Unbuffered, or `docker logs` shows nothing until a buffer fills — which for a bot
# that logs a few lines per check can be hours.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER bot

# matplotlib builds a font cache the first time it is imported, which takes seconds.
# Doing it here means the first /chart after a restart does not pay for it.
RUN python -c "import matplotlib.font_manager"

CMD ["price-tracker"]
