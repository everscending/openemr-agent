# syntax=docker/dockerfile:1
# ============================================================================
# Clinical Co-Pilot agent service (T020)
# ============================================================================
# Packages the FastAPI/uvicorn agent service (agent/src/copilot) for local
# dev-stack use via docker/development-easy. Dependencies are installed with
# `uv sync --frozen` against the committed agent/uv.lock — this Dockerfile
# packages the already-declared dependencies; it does not add, remove, or
# re-pin any of them.
#
# No secret is baked into this image. ANTHROPIC_API_KEY (and any other
# secret) is supplied at container *run* time via compose environment
# interpolation (e.g. `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}`), never as a
# build ARG, never COPY'd in, never hardcoded here.
#
# This same Dockerfile (or a close sibling) is the intended basis for T022's
# Railway build.

FROM python:3.12-slim

# uv is this project's package manager (agent/uv.lock is a uv lockfile).
RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency manifests copied first so this layer stays cached across source
# changes that don't touch dependencies.
COPY pyproject.toml uv.lock ./

# Source copied before `uv sync`: the project itself (hatchling build
# backend, packages = ["src/copilot"]) is installed non-editable from the
# lockfile, so `uv sync` needs the package source present to build it.
COPY src ./src

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

CMD ["uvicorn", "copilot.app:app", "--host", "0.0.0.0", "--port", "8080"]
