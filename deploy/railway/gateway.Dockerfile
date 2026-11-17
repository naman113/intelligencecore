# Gateway image for Railway (Quick 043). Build context = the REPO ROOT (Railway: root
# directory empty, RAILWAY_DOCKERFILE_PATH=deploy/railway/gateway.Dockerfile), so it can
# reach skills_library/ and deploy/readonly-role.sql, which sit outside backend/.
#
# Differs from backend/Dockerfile (which stays as is for local and VM use):
#  - no docker CLI: Railway has no Docker socket; model-written code runs in the separate
#    sandbox-runner service (SANDBOX_URL),
#  - skills_library is baked in (locally it is only a compose mount),
#  - uvicorn binds host '' (IPv4 and IPv6) so IPv4 and IPv6-only private networks both work.

FROM python:3.12-slim-bookworm

# uv: copied from the official distroless image (no curl|sh bootstrap).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# curl only for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY backend/scripts ./scripts
# skillservice.py resolves /app/skills_library; readonlykit.py reads /app/deploy/readonly-role.sql.
COPY skills_library /app/skills_library
COPY deploy/readonly-role.sql /app/deploy/readonly-role.sql

ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default artifact root; Railway sets IC_ARTIFACTS_DIR=/data/artifacts on the volume.
RUN mkdir -p /app/artifacts /app/inbox /app/documents

EXPOSE 8080

# /health returns 200 even when degraded, so grep the body.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/health | grep -q '"ok":true' || exit 1

# --host '' : asyncio binds every family (IPv4 + IPv6). '::' alone would be IPv6-only.
CMD ["uvicorn", "app.main:app", "--host", "", "--port", "8080"]
