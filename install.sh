#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Agentic System — single-file client-VM installer (Ubuntu/Debian).
#
#   git clone -b release <repo> && cd AgenticSystem
#   sudo ./install.sh --domain client.com           # serves app.client.com and dash.client.com
#                                                   # (both DNS records must point at this VM)
#   sudo ./install.sh --domain client.com --tls-internal   # Caddy internal CA (test VMs, private DNS)
#   sudo ./install.sh --domain client.com --app-host agent.client.com
#                                                   # keep a legacy app host so the WhatsApp
#                                                   # webhook URL does not change (--dash-host too)
#   sudo ./install.sh --ip-only | --ip ADDR         # no DNS: app at https://ADDR, dashboards at
#                                                   # https://ADDR:8443 (self-signed TLS)
#   ./install.sh --render-caddyfile <mode flags>    # print the Caddyfile and exit (no root)
#
# Idempotent: safe to re-run. Installs docker, uv, caddy; brings up the
# containers; installs systemd units; prints the dashboard URL + admin key.
# After install, everything else is configured on the dashboard Setup page.
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
# Default to the port a previous run installed, so re-running without --port
# does not silently move uvicorn back to 8080 while Caddy keeps proxying the
# old one (or vice versa). Explicit --port still wins.
APP_PORT=8080
[[ -f "$REPO_DIR/deploy/.installmeta" ]] && source "$REPO_DIR/deploy/.installmeta"
# Only APP_PORT carries over from a previous run; the address is remembered so
# a changed app address can be flagged at the end (the webhook URL moves with it).
PREV_SITE="${SITE:-}"
DOMAIN=""
IP_ONLY=0
IP_OVERRIDE=""
APP_HOST=""
DASH_HOST=""
TLS_INTERNAL=0
RENDER_ONLY=0

ALL_FLAGS="--domain X | --app-host H --dash-host H | --tls-internal | --ip-only | --ip ADDR | --port N | --render-caddyfile"
need_val(){ [[ $2 -ge 2 ]] || { echo "missing value for $1"; exit 1; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)    need_val "$1" $#; DOMAIN="$2"; shift 2;;
    --app-host)  need_val "$1" $#; APP_HOST="$2"; shift 2;;
    --dash-host) need_val "$1" $#; DASH_HOST="$2"; shift 2;;
    --tls-internal) TLS_INTERNAL=1; shift;;
    --ip-only) IP_ONLY=1; shift;;
    --ip)      need_val "$1" $#; IP_ONLY=1; IP_OVERRIDE="$2"; shift 2;;   # explicit address (NAT'd VMs, local test VMs)
    --port)    need_val "$1" $#; APP_PORT="$2"; shift 2;;
    --render-caddyfile) RENDER_ONLY=1; shift;;
    *) echo "unknown flag: $1 (use $ALL_FLAGS)"; exit 1;;
  esac
done

# ── input validation: pure bash, before any side effect ──────────────────
bad(){ echo "invalid $1: $2" >&2; exit 1; }
valid_host(){ [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]; }
valid_ipv4(){
  local ip="$1" o
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  for o in ${ip//./ }; do (( 10#$o <= 255 )) || return 1; done
}
valid_ipv6(){ [[ "$1" =~ ^[0-9A-Fa-f:.]+$ && "$1" == *:* ]]; }
valid_port(){
  [[ "$1" =~ ^[0-9]{1,5}$ ]] || return 1
  local n=$((10#$1))
  (( n >= 1 && n <= 65535 )) || return 1
  case "$n" in 80|443|3000|3001|5433|6379|8090|8091|8443) return 1;; esac
}
valid_port "$APP_PORT" || bad "port" "$APP_PORT"

# ── mode resolution ──────────────────────────────────────────────────────
set_site_ip(){
  if valid_ipv4 "$PUBLIC_IP"; then SITE_IP="$PUBLIC_IP"
  elif valid_ipv6 "$PUBLIC_IP"; then SITE_IP="[$PUBLIC_IP]"
  else bad "ip address" "$PUBLIC_IP"; fi
}
if [[ "$IP_ONLY" == 1 ]]; then
  [[ -z "$DOMAIN" && -z "$APP_HOST" && -z "$DASH_HOST" && "$TLS_INTERNAL" == 0 ]] \
    || { echo "--ip-only/--ip cannot be combined with --domain, --app-host, --dash-host or --tls-internal"; exit 1; }
  PUBLIC_IP="$IP_OVERRIDE"
  if [[ -z "$PUBLIC_IP" && "$RENDER_ONLY" == 1 ]]; then
    echo "--render-caddyfile in ip mode needs --ip ADDR"; exit 1
  fi
  # With no --ip the address is auto-detected after the root check (needs network).
  [[ -n "$PUBLIC_IP" ]] && set_site_ip
  MODE=ip
elif [[ -n "$DOMAIN" || ( -n "$APP_HOST" && -n "$DASH_HOST" ) ]]; then
  if [[ -n "$DOMAIN" ]]; then
    valid_host "$DOMAIN" || bad "domain" "$DOMAIN"
    APP_HOST="${APP_HOST:-app.$DOMAIN}"
    DASH_HOST="${DASH_HOST:-dash.$DOMAIN}"
  fi
  valid_host "$APP_HOST" || bad "app host" "$APP_HOST"
  valid_host "$DASH_HOST" || bad "dash host" "$DASH_HOST"
  [[ "${APP_HOST,,}" != "${DASH_HOST,,}" ]] || { echo "invalid hosts: app and dash must differ (separate origins are the security boundary)" >&2; exit 1; }
  MODE=domain
else
  echo "usage: sudo ./install.sh --domain client.com   (or --ip-only)"
  echo "flags: $ALL_FLAGS"
  exit 1
fi

# ── Caddyfile rendering ──────────────────────────────────────────────────
render_caddyfile(){
  local app_site dash_site tls out
  if [[ "$MODE" == ip ]]; then
    app_site="$SITE_IP"; dash_site="https://${SITE_IP}:8443"; tls="tls internal"
  else
    app_site="$APP_HOST"; dash_site="$DASH_HOST"
    tls=""; [[ "$TLS_INTERNAL" == 1 ]] && tls="tls internal"
  fi
  out="$(sed -e "s|__APP_SITE__|${app_site}|g" -e "s|__DASH_SITE__|${dash_site}|g" \
             -e "s|__APP_PORT__|${APP_PORT}|g" -e "s|__TLS__|${tls}|g" -e '/^#/d' "$REPO_DIR/deploy/Caddyfile.tmpl")"   # template header comments are not rendered
  if [[ "$out" == *__* ]]; then echo "Caddyfile template left a placeholder unresolved" >&2; exit 1; fi
  printf '%s\n' "$out"
}
if [[ "$RENDER_ONLY" == 1 ]]; then render_caddyfile; exit 0; fi

# WR-04: never silently move a live VM's app address (the webhook URL and the UI
# move with it). Fail closed before any side effect unless explicitly allowed.
if [[ -n "$PREV_SITE" && "$MODE" == domain && "${PREV_SITE,,}" != "https://${APP_HOST,,}" && "${MOVE_APP_HOST:-0}" != 1 ]]; then
  echo "refusing: this VM serves ${PREV_SITE}; these flags would move the app to https://${APP_HOST}." >&2
  echo "  keep it:  --app-host ${PREV_SITE#https://} --dash-host <dash host>   or move it deliberately: MOVE_APP_HOST=1 sudo ./install.sh ..." >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then echo "run with sudo (installs packages + systemd units)"; exit 1; fi
# The user who invoked sudo owns the checkout and runs the app service.
RUN_USER="${SUDO_USER:-root}"

ok(){ echo "  ✅ $1"; }
step(){ echo; echo "── $1 ─────────────────────────────────────"; }

step "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git openssl ca-certificates gnupg lsb-release iproute2 >/dev/null
ok "base packages"

step "Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
usermod -aG docker "$RUN_USER" 2>/dev/null || true
ok "docker $(docker --version | cut -d' ' -f3 | tr -d ',')"

step "uv (python manager)"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x /usr/local/bin/uv ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi
ok "uv $(/usr/local/bin/uv --version 2>/dev/null | cut -d' ' -f2 || echo installed)"

step "Caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
ok "caddy $(caddy version | cut -d' ' -f1)"

step "Environment (.env)"
cd "$REPO_DIR"
if [[ ! -f .env ]]; then
  WEBHOOK_SECRET="$(openssl rand -hex 32)"
  OPS_KEY="$(openssl rand -hex 32)"
  cat > .env <<EOF
# Generated by install.sh $(date -u +%F). Everything else is configured on the
# dashboard Setup page — do not hand-edit unless you know why.
MEMORY_DATABASE_URL=postgresql://agent:agent@localhost:5433/agent_memory
# Shared with the WhatsApp connector only (webhook auth). NOT an admin key.
WEBHOOK_SECRET=${WEBHOOK_SECRET}
# Admin/ops key for the dashboard + /api endpoints. Keep off the connector.
OPS_KEY=${OPS_KEY}
LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -hex 32)
LANGFUSE_SALT=$(openssl rand -hex 16)
EOF
  # Phase 11: GOLIVE-02 settings encryption key (add if missing, never overwrite)
  bash deploy/ensure-settings-key.sh .env >/dev/null
  bash deploy/ensure-warehouse-secrets.sh .env >/dev/null  # Phase 14: WH-02
  # Phase 14 (D-09): new installs read business data through the warehouse
  grep -q '^WAREHOUSE_URL=' .env || echo 'WAREHOUSE_URL=postgresql://127.0.0.1:5434/warehouse' >> .env
  chown "$RUN_USER" .env && chmod 600 .env
  ok ".env created with generated keys"
else
  WEBHOOK_SECRET="$(grep -E '^WEBHOOK_SECRET=' .env | cut -d= -f2- || true)"
  # Idempotent upgrade of pre-split .env files: add missing keys.
  if ! grep -q '^OPS_KEY=' .env; then
    echo "OPS_KEY=$(openssl rand -hex 32)" >> .env
  fi
  grep -q '^LANGFUSE_NEXTAUTH_SECRET=' .env || echo "LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -hex 32)" >> .env
  grep -q '^LANGFUSE_SALT=' .env || echo "LANGFUSE_SALT=$(openssl rand -hex 16)" >> .env
  OPS_KEY="$(grep -E '^OPS_KEY=' .env | cut -d= -f2- || true)"
  # Phase 11: GOLIVE-02 settings encryption key (add if missing, never overwrite)
  bash deploy/ensure-settings-key.sh .env >/dev/null
  bash deploy/ensure-warehouse-secrets.sh .env >/dev/null  # Phase 14: WH-02
  ok ".env exists — kept it (added any missing keys)"
fi
# Phase 9: set a KEY=VALUE line idempotently (one line per key after any number
# of re-runs). Values are pre-validated and never contain '|'.
set_env(){
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
# Resolve the auto-detected public address now that we are root with a network.
if [[ "$MODE" == ip && -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -fsS -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
  set_site_ip
fi
if [[ "$MODE" == ip ]]; then
  APP_URL="https://${SITE_IP}"; DASH_URL="https://${SITE_IP}:8443"
else
  APP_URL="https://${APP_HOST}"; DASH_URL="https://${DASH_HOST}"
fi
# These keys make every bare `docker compose` from REPO_DIR include the VM
# overlay (ui + dashboards). The gateway ignores them (pydantic extra="ignore").
set_env COMPOSE_FILE docker-compose.yml:docker-compose.vm.yml
set_env COMPOSE_PATH_SEPARATOR :
set_env APP_PORT "$APP_PORT"
set_env DASHBOARDS_PUBLIC_URL "$DASH_URL"
chown "$RUN_USER" .env && chmod 600 .env
ln -sf ../.env backend/.env

step "Python dependencies"
# --frozen installs exactly the committed uv.lock. pyproject pins only floors
# (langgraph>=0.2, langchain>=1.0, ...), so without the lock two clients
# installed a month apart run different code from the same git commit.
sudo -u "$RUN_USER" bash -c "cd '$REPO_DIR/backend' && /usr/local/bin/uv sync --frozen -q"
ok "backend deps installed (locked)"

step "Containers (postgres + falkordb + langfuse + warehouse + ui + dashboards)"
# Run from REPO_DIR: relative COMPOSE_FILE entries fail from another cwd.
docker compose up -d --build --quiet-pull 2>/dev/null || docker compose up -d --build
ok "compose services up"
if [[ -d sandbox ]]; then
  docker build -q -t agentic-sandbox:latest ./sandbox >/dev/null && ok "sandbox image built"
fi
# Phase 15 (SRC-05): data-source sync image; a failed build only warns
if [[ -d infra/ic-sync ]]; then
  docker build -q -t agentic-sync:latest ./infra/ic-sync >/dev/null && ok "sync image built" || echo "  WARNING: sync image build failed (data sources will report: sync runner not installed)"
fi

step "systemd service"
cat > /etc/systemd/system/agentic-api.service <<EOF
[Unit]
Description=Agentic System gateway + workers
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}/backend
# The app's own database lives in compose, and docker.service being up does not
# mean the containers are. Bring them up first so a reboot never lands the
# gateway in a crash loop against a DB that is not there yet.
ExecStartPre=-/usr/bin/docker compose -f ${REPO_DIR}/docker-compose.yml up -d
# Overlay (ui + dashboards). --no-build: a restart must never block on an image
# build that runs into TimeoutStartSec. The base-only line above stays, because
# an explicit -f overrides COMPOSE_FILE, so Postgres is up even if an overlay
# image is missing.
ExecStartPre=-/bin/sh -c 'cd "${REPO_DIR}" && exec /usr/bin/docker compose up -d --no-build'
ExecStart=${REPO_DIR}/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# enable --now is a no-op on an already-active unit, so a re-run would keep the
# OLD port/unit live while Caddy got rewritten to the new one. Restart always.
systemctl enable agentic-api >/dev/null 2>&1
systemctl restart agentic-api
ok "agentic-api service running on port ${APP_PORT}"

step "Caddy config"
# NEVER add a `log` directive to the Caddyfile: view tokens ride in query strings.
[[ -f /etc/caddy/Caddyfile ]] && cp -f /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
render_caddyfile > /etc/caddy/Caddyfile.new
if ! caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile; then
  echo "  ❌ rendered Caddyfile failed validation — keeping the existing /etc/caddy/Caddyfile"
  rm -f /etc/caddy/Caddyfile.new
  exit 1
fi
mv -f /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
systemctl reload caddy 2>/dev/null || systemctl restart caddy
ok "caddy serving ${APP_URL} (app) and ${DASH_URL} (dashboards)"

step "Network exposure"
# docker-compose binds everything to 127.0.0.1 — only caddy (80/443, plus 8443
# in ip mode) listens publicly. NB: docker-published ports bypass ufw, which is
# why the loopback bind is done in compose rather than left to a firewall.
public_listeners(){
  ss -H -ltn 2>/dev/null | awk '{print $4}' \
    | grep -Ev '^(127\.|\[::1\]|::1|\[::ffff:127\.)' | sed -E 's/.*:([0-9]+)$/\1/' | sort -nu || true
}
sleep 2
ALLOWED=" 22 80 443 "; [[ "$MODE" == ip ]] && ALLOWED=" 22 80 443 8443 "
PUB="$(public_listeners | tr '\n' ' ')"
UNEXPECTED=""
for p in $PUB; do [[ "$ALLOWED" == *" $p "* ]] || UNEXPECTED+="$p "; done
if [[ -z "$UNEXPECTED" ]]; then
  ok "public listeners: ${PUB:-none}"
else
  echo "  ⚠️  unexpected public listener(s): ${UNEXPECTED}"
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi inactive; then
  echo "  ⚠️  ufw installed but INACTIVE — recommended:"
  FW="ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp"
  [[ "$MODE" == ip ]] && FW="$FW && ufw allow 8443/tcp"
  echo "      $FW && ufw allow OpenSSH && ufw enable"
fi

step "agentctl"
ln -sf "$REPO_DIR/deploy/agentctl" /usr/local/bin/agentctl
chmod +x "$REPO_DIR/deploy/agentctl"
echo "APP_PORT=${APP_PORT}" > "$REPO_DIR/deploy/.installmeta"
echo "SITE=${APP_URL}" >> "$REPO_DIR/deploy/.installmeta"   # = APP_URL, kept for backward compatibility
echo "APP_URL=${APP_URL}" >> "$REPO_DIR/deploy/.installmeta"
echo "DASH_URL=${DASH_URL}" >> "$REPO_DIR/deploy/.installmeta"
echo "DOMAIN=${DOMAIN}" >> "$REPO_DIR/deploy/.installmeta"
echo "IP_ONLY=${IP_ONLY}" >> "$REPO_DIR/deploy/.installmeta"
ok "agentctl installed"

# Phase 11: GOLIVE-04 nightly backup timer
cat > /etc/systemd/system/agentic-backup.service <<UNIT
[Unit]
Description=Agentic System nightly agent_memory backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${RUN_USER}
ExecStart=/usr/local/bin/agentctl backup
UNIT
cat > /etc/systemd/system/agentic-backup.timer <<UNIT
[Unit]
Description=Nightly agent_memory backup

[Timer]
OnCalendar=*-*-* 01:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now agentic-backup.timer >/dev/null 2>&1
ok "nightly backup timer enabled (01:30, keeps ${BACKUP_KEEP_DAYS:-14} days)"
# end Phase 11 backup timer

echo
echo "══════════════════════════════════════════════════════════════════"
echo "  DONE. Save these — the keys are shown ONCE:"
echo
echo "  Operator UI:  ${APP_URL}/"
echo "  Console:      ${APP_URL}/dashboard"
echo "  Dashboards:   ${DASH_URL}/d/<slug>/?t=<view token>"
echo "  Admin key:    ${OPS_KEY}"
echo "                (paste into the 'ops key' box, top-right)"
echo "  Settings key: SETTINGS_ENCRYPTION_KEY in ${REPO_DIR}/.env — store it with the admin key; backups cannot be decrypted without it"  # Phase 11
echo "  Webhook URL:  ${APP_URL}/webhook/whatsapp"
echo "  Webhook key:  ${WEBHOOK_SECRET}"
echo "                (set BOTH on the WhatsApp connector; the webhook key is"
echo "                 deliberately separate from the admin key)"
echo
echo "  Next: open the dashboard → Setup section → enter LLM key,"
echo "  business DB, WhatsApp connector → hit Test on each card."
echo
echo "  Manage:  agentctl status | logs | restart | update | rollback <ref> | url"
echo
echo "  FIRST: log out and back in (or run 'newgrp docker') before 'agentctl"
echo "  status' — the docker group only applies to a new login, and until then"
echo "  the container table comes back empty."
if [[ "$MODE" == ip ]]; then
  echo
  echo "  NOTE: ip mode uses a self-signed cert — your browser will warn."
else
  echo
  echo "  DNS: point BOTH records at this VM:  ${APP_HOST}  and  ${DASH_HOST}"
fi
if [[ -n "$PREV_SITE" && "$PREV_SITE" != "$APP_URL" ]]; then
  echo
  echo "  ⚠️  The app address changed (${PREV_SITE} -> ${APP_URL})."
  echo "     Set the new webhook URL on the WhatsApp connector, or re-run with"
  echo "     --app-host <old host> to keep the old address."
fi
if [[ "$MODE" == ip ]]; then
  echo "  Firewall: only ports 80/443 and 8443 need to be open inbound."
else
  echo "  Firewall: only ports 80/443 need to be open inbound."
fi
echo "══════════════════════════════════════════════════════════════════"
