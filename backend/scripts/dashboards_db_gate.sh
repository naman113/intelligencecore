#!/usr/bin/env bash
# Non-skippable real-Postgres gate for Phase 3 dashboard storage; DASH-01..04
# invariants and the DASH-03 concurrency proof (tests/test_dashboards_db.py).
# Phase 4: also runs tests/test_dashboard_refresh_db.py (SERVE-01/02/05 claim, writes, seed, preview tokens).
# Phase 6: also runs tests/test_dashboard_kpi_db.py, test_dashboard_publish_db.py and test_dashboard_tools_db.py (KPI history, publish race, save tool); floor 49.
# Phase 9: also runs tests/test_history_retention_db.py (KPI-04 retention: exactly, never current, rollback race).
# Phase 13.1: also runs tests/test_forecasts_db.py (forecast definitions, points, retention).
# Phase 18: also runs tests/test_dashboards_spec_db.py (spec columns, CHECK, trigger, spec save and publish, HTML gate); floor 56.
# Phase 17: test_dashboard_refresh_db.py p17 tests (sync-aligned claim, backoff, deferred, mixed case, data_as_of); floor 61
# Usage:
#   bash backend/scripts/dashboards_db_gate.sh                    # always run
#   bash backend/scripts/dashboards_db_gate.sh --if-changed REF   # run only if watched files differ from REF
# Env:
#   DASH_TEST_DATABASE_URL  superuser URL (default: p3 stack, 127.0.0.1:9033)
#   DASH_GATE_MIN_PASSED    pass-count floor (default 61)
#   DASH_GATE_ALLOW_REMOTE  set to 1 to allow a non-loopback host
# Exit codes: 0 PASS or SKIP; 1 test or leftover failure; 2 setup, preflight or usage error.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || { echo "FAIL dashboards db gate: cannot cd to backend"; exit 2; }

MODE=always
REF=""
if [ "$#" -eq 0 ]; then
  :
elif [ "$#" -eq 2 ] && [ "$1" = "--if-changed" ]; then
  MODE=changed
  REF="$2"
else
  echo "usage: dashboards_db_gate.sh [--if-changed REF]"
  exit 2
fi

export DASH_TEST_DATABASE_URL="${DASH_TEST_DATABASE_URL:-postgresql://agent:agent@127.0.0.1:9033/agent_memory}"
MIN="${DASH_GATE_MIN_PASSED:-61}"

REDACTED="$(uv run --frozen python -c "
import os
from urllib.parse import urlsplit
p = urlsplit(os.environ['DASH_TEST_DATABASE_URL'])
print('%s:%s/%s' % (p.hostname or '', p.port or '', p.path.lstrip('/')))
" 2>/dev/null)"
HOST="${REDACTED%%:*}"
if [ -z "$REDACTED" ]; then
  echo "FAIL dashboards db gate: cannot parse DASH_TEST_DATABASE_URL"
  exit 2
fi
case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  *)
    if [ "${DASH_GATE_ALLOW_REMOTE:-}" != "1" ]; then
      echo "FAIL dashboards db gate: refusing non-loopback host $REDACTED; the tests CREATE and DROP databases (set DASH_GATE_ALLOW_REMOTE=1 for a dedicated test server)"
      exit 2
    fi
    ;;
esac

if [ "$MODE" = changed ]; then
  if ! git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null 2>&1; then
    echo "FAIL dashboards db gate: unknown ref $REF"
    exit 2
  fi
  SHORT="$(git rev-parse --short "${REF}^{commit}")"
  CHANGED="$(git -C .. diff --name-only "$REF" -- \
    backend/app/dashboards.py backend/app/dashboards_api.py backend/app/schema.sql \
    backend/tests/test_dashboards_db.py backend/scripts/dashboards_db_gate.sh \
    backend/app/dashboard_refresh.py backend/app/dashboards_serve.py \
    backend/tests/test_dashboard_refresh_db.py \
    backend/app/kpihistory.py backend/app/actionsvc.py backend/app/dashboardtools.py \
    backend/app/dashboardlint.py backend/tests/test_dashboard_kpi_db.py \
    backend/tests/test_dashboard_publish_db.py backend/tests/test_dashboard_tools_db.py \
    backend/app/consolidation.py backend/tests/test_history_retention_db.py \
    backend/app/forecasts.py backend/tests/test_forecasts_db.py \
    backend/tests/test_dashboards_spec_db.py)"
  if [ -z "$CHANGED" ]; then
    echo "SKIP dashboards db gate: no dashboard storage changes since $SHORT"
    exit 0
  fi
  echo "dashboards db gate: changed since $SHORT:"
  echo "$CHANGED"
fi

if ! PRE="$(uv run --frozen python -c "
import asyncio, os, sys
import asyncpg
async def main():
    c = await asyncpg.connect(os.environ['DASH_TEST_DATABASE_URL'], timeout=5)
    await c.fetchval('SELECT 1')
    await c.close()
try:
    asyncio.run(main())
except Exception as e:
    print(type(e).__name__)
    sys.exit(1)
" 2>/dev/null)"; then
  echo "FAIL dashboards db gate: cannot reach Postgres at $REDACTED (${PRE:-error}); start the stack with ic.sh up p3 or p4"
  exit 2
fi

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT
uv run --frozen pytest -q tests/test_dashboards_db.py tests/test_dashboard_refresh_db.py \
  tests/test_dashboard_kpi_db.py tests/test_dashboard_publish_db.py tests/test_dashboard_tools_db.py \
  tests/test_history_retention_db.py tests/test_forecasts_db.py \
  tests/test_dashboards_spec_db.py 2>&1 | tee "$OUT"
RC="${PIPESTATUS[0]}"

SUMMARY="$(grep -v '^[[:space:]]*$' "$OUT" | tail -1)"
count() { local n; n="$(echo "$SUMMARY" | grep -Eo "[0-9]+ $1" | head -1 | grep -Eo '^[0-9]+')"; echo "${n:-0}"; }
PASSED="$(count passed)"
FAILED="$(count failed)"
ERRORS="$(count 'errors?')"
SKIPPED="$(count skipped)"

fail() { echo "FAIL dashboards db gate: $1 ($SUMMARY)"; exit 1; }
[ "$RC" -eq 0 ] || fail "pytest exit status $RC"
[ "$FAILED" -eq 0 ] || fail "$FAILED failed"
[ "$ERRORS" -eq 0 ] || fail "$ERRORS errors"
[ "$SKIPPED" -eq 0 ] || fail "suite skipped: DASH_TEST_DATABASE_URL not honoured"
[ "$PASSED" -ge "$MIN" ] || fail "only $PASSED passed, floor is $MIN"

LEFT="$(uv run --frozen python -c "
import asyncio, os
import asyncpg
async def main():
    c = await asyncpg.connect(os.environ['DASH_TEST_DATABASE_URL'], timeout=5)
    rows = await c.fetch(\"SELECT datname FROM pg_database WHERE datname LIKE 'dash_test_%'\")
    await c.close()
    print(' '.join(r['datname'] for r in rows))
asyncio.run(main())
" 2>/dev/null)"
if [ -n "$LEFT" ]; then
  echo "FAIL dashboards db gate: leftover throwaway databases: $LEFT"
  exit 1
fi

echo "PASS dashboards db gate: $PASSED passed, 0 failed, 0 skipped, 0 leftover dash_test_* databases ($REDACTED)"
exit 0
