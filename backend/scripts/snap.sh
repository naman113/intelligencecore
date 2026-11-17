#!/usr/bin/env bash
# Dump live system state to backend/snapshot.json so Claude can read it.
# Usage (from anywhere):  bash ~/Desktop/Repo/AgenticSystem/backend/scripts/snap.sh
set -euo pipefail
cd "$(dirname "$0")/.."                      # -> backend/

# Admin key = OPS_KEY (WEBHOOK_SECRET only as a fallback for pre-split .env
# files — it is the connector's secret and does not grant admin on new installs).
K=$(grep -hE '^OPS_KEY=' .env ../.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)
if [ -z "${K}" ]; then
  K=$(grep -hE '^WEBHOOK_SECRET=' .env ../.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)
fi
B="${BASE_URL:-http://localhost:8000}"
OUT="snapshot.json"

if [ -z "${K}" ]; then echo "could not find OPS_KEY (or WEBHOOK_SECRET) in .env"; exit 1; fi

{
  echo "=== HEALTH ==="; curl -s "$B/health" || echo "app not reachable on $B"
  echo; echo "=== LOGS ==="; curl -s "$B/api/logs?limit=400" -H "x-ops-key: $K"
  echo; echo "=== CASES ==="; curl -s "$B/api/cases?limit=50" -H "x-ops-key: $K"
  echo; echo "=== RUNS ==="; curl -s "$B/api/runs?limit=15" -H "x-ops-key: $K"
  echo; echo "=== ACTIONS ==="; curl -s "$B/api/actions?status=&limit=15" -H "x-ops-key: $K"
  echo; echo "=== PEOPLE ==="; curl -s "$B/api/people" -H "x-ops-key: $K"
} > "$OUT" 2>&1

echo "wrote backend/$OUT — tell Claude to read it"
