# Gateway image: FastAPI app + all background loops in one process.
#
# Two non-obvious requirements:
#  1. The docker CLI is installed because sandbox.py shells out to `docker run`
#     for the Analysis/Document agents. The container talks to the HOST daemon
#     through the mounted socket (docker-out-of-docker) — it does not run its
#     own. Without the CLI those agents return "docker CLI not found on host".
#  2. Dependencies install from the committed uv.lock with --frozen, the same
#     pin discipline install.sh and agentctl use, so a local container and a
#     client VM resolve to identical versions.

FROM python:3.12-slim-bookworm

# uv: copied from the official distroless image (no curl|sh bootstrap).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    # docker CLI only (docker-ce-cli), NOT the engine
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# Dependency layer: cached until the lock or manifest actually changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts

# On PATH so `python`/`uvicorn` resolve to the locked venv without `uv run`.
# PYTHONPATH is belt-and-braces: uvicorn's --app-dir already defaults to "." so
# `app.main` resolves from WORKDIR, but a console script otherwise puts
# .venv/bin on sys.path[0], and the failure mode is a bare ModuleNotFoundError.
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# sandbox.py resolves ARTIFACTS_ROOT as <repo>/artifacts == /app/artifacts.
# Compose mounts a named volume here; the sandbox containers mount the SAME
# volume by name (see SANDBOX_ARTIFACTS_VOLUME), which is what makes agent
# file handoff work without host-path translation.
RUN mkdir -p /app/artifacts /app/inbox /app/documents

EXPOSE 8080

# Healthcheck hits /health, which deliberately returns 200 even when degraded —
# so grep the body rather than trusting the status code.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/health | grep -q '"ok":true' || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
