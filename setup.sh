#!/usr/bin/env bash
# One-command dev environment setup. Run from the AgenticSystem folder:
#   ./setup.sh
set -uo pipefail

cd "$(dirname "$0")"
PASS=0; FAIL=0

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "── Checking prerequisites ─────────────────────────────"

if ! command -v brew >/dev/null 2>&1 && [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"   # installed but not on PATH yet
fi
if command -v brew >/dev/null 2>&1; then ok "Homebrew"; else
  bad "Homebrew missing — run the install command Claude put in your clipboard"; exit 1; fi

if command -v docker >/dev/null 2>&1; then ok "Docker CLI"; else
  echo "  📦 Installing Docker Desktop..."; brew install --cask docker && ok "Docker Desktop installed (open it once to finish setup)"; fi

if docker info >/dev/null 2>&1; then ok "Docker daemon running"; else
  bad "Docker daemon not running — open Docker Desktop, then re-run ./setup.sh"; fi

if command -v uv >/dev/null 2>&1; then ok "uv"; else
  echo "  📦 Installing uv..."; brew install uv && ok "uv installed"; fi

if command -v node >/dev/null 2>&1 && [ "$(node -e 'console.log(process.versions.node.split(".")[0])')" -ge 20 ]; then
  ok "Node $(node --version)"; else
  echo "  📦 Installing Node..."; brew install node@22 && ok "Node installed"; fi

if command -v cloudflared >/dev/null 2>&1; then ok "cloudflared"; else
  echo "  📦 Installing cloudflared..."; brew install cloudflared && ok "cloudflared installed"; fi

echo ""
echo "── Environment file ───────────────────────────────────"
if [ -f .env ]; then ok ".env exists"; else
  cp .env.example .env && ok ".env created from template — FILL IN THE KEYS"
fi
# Generate any blank secret, exactly as install.sh does. The template ships them
# empty on purpose: a committed secret is a secret every clone of this repo has.
for KEY in OPS_KEY WEBHOOK_SECRET; do
  if grep -qE "^${KEY}=$" .env 2>/dev/null; then
    VAL="$(openssl rand -hex 32)"
    # portable in-place edit (BSD sed on macOS needs the empty -i arg)
    sed -i '' -e "s|^${KEY}=$|${KEY}=${VAL}|" .env 2>/dev/null \
      || sed -i -e "s|^${KEY}=$|${KEY}=${VAL}|" .env
    ok "${KEY} generated"
  elif ! grep -qE "^${KEY}=" .env 2>/dev/null; then
    echo "${KEY}=$(openssl rand -hex 32)" >> .env && ok "${KEY} added"
  fi
done
# Phase 11: GOLIVE-02 settings encryption key (add if missing, never overwrite)
bash deploy/ensure-settings-key.sh .env
bash deploy/ensure-warehouse-secrets.sh .env >/dev/null  # Phase 14: WH-02 (the container needs its passwords; WAREHOUSE_URL stays opt-in)
chmod 600 .env
# backend reads .env from its own cwd
ln -sf ../.env backend/.env 2>/dev/null || true

echo ""
echo "── Python dependencies ────────────────────────────────"
(cd backend && uv sync) && ok "backend deps installed" || bad "uv sync failed"

echo ""
echo "── Infrastructure containers ──────────────────────────"
if docker info >/dev/null 2>&1; then
  docker compose up -d && ok "postgres (:5433) + langfuse (:3001) up" || bad "docker compose failed"
  echo "  📦 Building sandbox image..."
  docker build -q -t agentic-sandbox:latest ./sandbox && ok "sandbox image built" || bad "sandbox build failed"
  # Phase 15 (SRC-05): data-source sync image; a failed build only warns
  if [[ -d infra/ic-sync ]]; then
    docker build -q -t agentic-sync:latest ./infra/ic-sync >/dev/null && ok "sync image built" || echo "  WARNING: sync image build failed (data sources will report: sync runner not installed)"
  fi
fi

echo ""
echo "── Summary ────────────────────────────────────────────"
echo "  $PASS ok, $FAIL failed"
[ $FAIL -gt 0 ] && { echo "  Fix the ❌ items above, then re-run ./setup.sh"; exit 1; }

cat <<'EOF'

Next steps:
  1. Fill in .env (keys, DB URIs, ALLOWED_NUMBERS)
  2. Verify connections:  cd backend && uv run python -m app.selftest
  3. Start the gateway:   uv run uvicorn app.main:app --port 8080
  3. Start the tunnel:    cloudflared tunnel --url http://localhost:8080
     → copy the https URL it prints; webhook URL = <that-url>/webhook/whatsapp
  4. Set AGENT_WEBHOOK_URL + AGENT_WEBHOOK_SECRET on the Railway connector, redeploy
  5. Langfuse UI: http://localhost:3001 (create account on first visit)
EOF
