# Phase 15 (SRC-05): ic-sync, the short-lived sync container. dlt and the database drivers live here
# so backend/pyproject.toml and uv.lock stay frozen. Base pinned by digest, Python deps by hash.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
LABEL org.opencontainers.image.title=ic-sync

# Microsoft ODBC Driver 18 for SQL Server (Debian 12 repo). curl and gnupg are purged afterwards.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/icsync
COPY requirements.txt /opt/icsync/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

RUN useradd -u 10001 -m icsync
COPY icsync /opt/icsync/icsync

ENV PYTHONPATH=/opt/icsync \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUNTIME__DLTHUB_TELEMETRY=false \
    DLT_DATA_DIR=/tmp/dlt \
    HOME=/home/icsync
USER icsync
CMD ["python", "-m", "icsync", "version"]
