# mcp_server — container image (spec v1.1 repair 12).
#
# *** HONEST STATUS ***
# Written and reviewed against mcp_server/requirements.txt and server.py's
# own PIT_WALL_MCP_HOST/PIT_WALL_MCP_PORT env-var handling. `docker build
# -f mcp_server/Dockerfile -t pit-wall-copilot-mcp-server .` was attempted
# in this environment and failed the same way and for the same reason as
# backend/Dockerfile's own module docstring documents in detail: a real
# Docker daemon is available here, but the outbound proxy allowlist has no
# container registry on it, so `FROM python:3.11-slim` cannot be pulled
# (403 Forbidden from registry-1.docker.io) — a network-policy restriction,
# not a Dockerfile defect.
# Provisioned by infrastructure/template.yaml's McpServerEcrRepository +
# the mcp_server ECS Fargate service — see docs/deployment-runbook.md
# Step B for the build/push/deploy commands this image is for.
#
# Build context is the REPOSITORY ROOT (not mcp_server/), matching
# backend/Dockerfile's own reasoning: mcp_server/requirements.txt is
# deliberately its OWN, separate virtual-environment-worthy dependency set
# (the `mcp` SDK's Starlette version conflicts with backend/requirements.txt
# — see the top-level README §7a), so only mcp_server/ itself needs to be
# copied in, not backend/ or scenarios/ (mcp_server talks to the backend
# entirely over HTTP, per server.py — it never imports backend code or
# reads the scenario file directly).
#
#   docker build -f mcp_server/Dockerfile -t pit-wall-copilot-mcp-server .
#
# Requires PIT_WALL_API_BASE_URL (the deployed backend's URL) and
# PIT_WALL_DEMO_ACCESS_TOKEN (spec v1.1 repair 11 — no default baked in
# here or anywhere else) at runtime; PIT_WALL_MCP_ACCESS_TOKEN is optional
# (see server.py's _AuthMiddleware docstring) but strongly recommended
# once this is reachable from outside a private network.
# Spec v1.1 pass 4, mandatory correction 8: see backend/Dockerfile's own
# comment on this same change for the full reasoning (registry access is
# not available in this environment to resolve/pin an exact digest; a
# fully-qualified, non-floating version+codename tag is the practical
# improvement available here).
FROM python:3.11.9-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIT_WALL_MCP_HOST=0.0.0.0 \
    PIT_WALL_MCP_PORT=9000

WORKDIR /app

COPY mcp_server/requirements.txt mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

COPY mcp_server/server.py mcp_server/server.py

WORKDIR /app/mcp_server

RUN useradd --create-home --uid 10001 pitwall
USER pitwall

EXPOSE 9000

# Spec v1.1 pass 3, correction 6: same Python-stdlib health check pattern
# as backend/Dockerfile (no curl/wget in this slim image). Probes /ready
# rather than /health — /ready additionally confirms the configured
# backend is reachable (see server.py's docstring above /ready), which is
# the more useful signal for orchestrators deciding whether to route
# traffic to this container. No credentials are read, sent, or logged.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request\ntry:\n    with urllib.request.urlopen('http://127.0.0.1:9000/ready', timeout=2) as r:\n        sys.exit(0 if r.status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

# server.py's main() already reads PIT_WALL_MCP_HOST/PIT_WALL_MCP_PORT and
# calls uvicorn.run(...) itself (see build_streamable_http_app()) — no
# separate uvicorn CLI invocation is needed, unlike backend/Dockerfile's
# CMD (which runs uvicorn directly against app.main:app because
# app/main.py has no __main__ entry point of its own).
CMD ["python", "server.py"]
