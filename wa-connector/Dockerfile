# WhatsApp connector (Baileys) — containerized for the local Docker stack.
#
# Copied from repos/Whatsapp (Railway deployment) and left functionally
# identical. The ONLY thing that makes the pairing permanent is where the auth
# state lives: whatsapp.js keeps it in <app>/wa-sessions via
# useMultiFileAuthState, so that path MUST be a named volume. On an anonymous
# or absent volume the creds die with the container and every restart demands a
# fresh QR scan.
#
# Two-stage: Baileys pulls optional native deps that need a toolchain to build,
# which the slim runtime image does not carry.

FROM node:20-bookworm AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:20-bookworm-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Mount points. Created here so the app can start before any volume is attached
# (and so clearSession's "delete contents, not the directory" logic holds — it
# refuses to unlink a mount point).
RUN mkdir -p /app/wa-sessions /app/logs

ENV NODE_ENV=production \
    PORT=3000

EXPOSE 3000

# /api/status is deliberately exempt from the API-key middleware (server.js), so
# the healthcheck needs no secret. It reports process liveness — `connected`
# in the body is the WhatsApp link state, which is legitimately false while
# waiting for a QR scan, so it must NOT gate the container health.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://127.0.0.1:3000/api/status > /dev/null || exit 1

CMD ["node", "server.js"]
