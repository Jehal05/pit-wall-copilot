# Pit Wall Copilot backend — container image (Gate 5).
#
# *** HONEST STATUS ***
# This Dockerfile was written and reviewed against `backend/requirements.txt`
# and `app/config.py`'s path assumptions. `docker build -f backend/Dockerfile
# -t pit-wall-copilot-backend .` was attempted in this environment (a real
# Docker daemon IS available here) and failed at the very first layer —
# `FROM python:3.11-slim` — with:
#   "failed to resolve source metadata for docker.io/library/python:3.11-slim:
#    unexpected status from HEAD request to
#    https://registry-1.docker.io/v2/library/python/manifests/3.11-slim:
#    403 Forbidden"
# This environment's outbound network goes through a proxy allowlist that
# permits the standard package registries (npm, PyPI, crates.io, Go
# modules, JSR) but NOT any container registry (docker.io, ghcr.io,
# public.ecr.aws, ...), so no base image can be pulled here — this is a
# network-policy restriction, not a Dockerfile defect. It has not been
# pushed to a registry or deployed to ECS/App Runner/anywhere else.
# Building and running it for the first time is real work for whoever picks
# up Gate 5 with access to an unrestricted Docker daemon and (for the
# dynamodb path) a real AWS account — see docs/deployment-runbook.md.
#
# Build context is the REPOSITORY ROOT (not backend/), because the app
# resolves its default scenario/knowledge-base paths relative to the
# repository root (app/config.py: REPO_ROOT = backend/'s parent) — the
# image preserves that same backend/ + scenarios/ + docs/ layout so no
# extra env vars are required to find them:
#
#   docker build -f backend/Dockerfile -t pit-wall-copilot-backend .
#
# Runs in STATE_REPOSITORY=memory by default (no AWS needed) — pass
# STATE_REPOSITORY=dynamodb plus the usual AWS credentials/env vars
# (see .env.example) to point it at a real table instead.

# Spec v1.1 pass 4, mandatory correction 8: "pin base images/digests where
# practical." This environment's outbound proxy has no container registry
# on its allowlist (see the module docstring above), so the exact
# `sha256:...` digest for this tag cannot be resolved and pinned FROM here
# — that requires a real Docker daemon with registry access, one `docker
# pull python:3.11.9-slim-bookworm` and `docker inspect --format='{{index
# .RepoDigests 0}}' python:3.11.9-slim-bookworm` away. What IS done here,
# and does not require registry access: replacing the floating,
# auto-updating `3.11-slim` tag (which silently resolves to a different
# underlying image every time it's rebuilt) with a fully-qualified,
# specific version + Debian codename tag, which only ever refers to one
# concrete image. EXTERNAL_RELEASE_CHECKLIST.md documents the exact
# `docker pull`/`docker inspect` command to run once real registry access
# is available, to replace this tag with its resolved digest for a
# genuinely immutable production build.
FROM python:3.11.9-slim-bookworm AS base

# The simulator/watch-evaluator loop needs no OS-level build tools —
# fastapi/pydantic/boto3 all ship wheels for this image's platform — so no
# compiler toolchain is installed, keeping the image small and reducing
# its exposed surface.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching across code-only changes).
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Application code + the demo data it reads by default (scenario JSON,
# sporting-rules KB documents) — see the module docstring above for why
# this layout (repo-root-relative) is preserved rather than flattened.
COPY backend/app backend/app
COPY scenarios scenarios
COPY docs docs

WORKDIR /app/backend

# Runs as a non-root user — a real, if modest, hardening step for a
# container that will sit behind a public endpoint once deployed.
RUN useradd --create-home --uid 10001 pitwall
USER pitwall

EXPOSE 8000

# Spec v1.1 pass 3, correction 6: a container health check, using a plain
# Python stdlib HTTP request rather than curl/wget (neither is installed
# in this slim image, and adding one merely to run a health check would
# grow the image and its exposed surface for no real benefit). Exits 0
# only on a real HTTP 200 from GET /health, non-zero otherwise (including
# on a connection error, non-200 status, or timeout) — no credentials are
# read, sent, or logged by this check, and /health itself returns no
# secret-bearing data (see backend/app/api/routes.py's health_check()).
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request\ntry:\n    with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2) as r:\n        sys.exit(0 if r.status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

# No --reload in the image (that's a local-dev convenience covered by
# docker-compose.yml below); a production container runs the app as built.
#
# Spec v1.1 pass 4, mandatory correction 8: "graceful shutdown." uvicorn
# is PID 1 in this container (no separate init/tini process — the image
# has no shell-wrapped entrypoint that would leave uvicorn as a child
# process instead), so it receives ECS's SIGTERM directly. uvicorn's
# default signal handler already stops accepting new connections and lets
# in-flight requests finish before exiting; `--timeout-graceful-shutdown`
# makes that window explicit and bounded (matching the ECS task
# definition's `StopTimeout` in infrastructure/template.yaml, which caps
# how long ECS itself waits before sending SIGKILL) rather than relying on
# uvicorn's own unstated default.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "25"]
