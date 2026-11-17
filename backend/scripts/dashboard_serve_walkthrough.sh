#!/usr/bin/env bash
# Live end-to-end proof of SERVE-01..05 on an ic.sh harness stack: the dashboards
# origin, the token-gated /d/ routes, per-version CSP, snapshot-only data, the
# refresh loop (seed, guardrail rejection, last_error, Postgres restart) and
# token-free logs (step 17 also probes non-canonical /d/ forms: double slash,
# encoded '?', dot segments, on both ports). It never touches the shared tenant DB container and never
# prints tokens or the ops key (tokens are shown only as len=N).
# Usage: API_URL=http://127.0.0.1:9041 DASH_URL=http://127.0.0.1:9042 STACK=p4 OPS_KEY=... \
#        [PERSON_ID=...] [RESILIENCE=1] bash backend/scripts/dashboard_serve_walkthrough.sh
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:9041}"
DASH_URL="${DASH_URL:-http://127.0.0.1:9042}"
STACK="${STACK:-p4}"
APP="ic-$STACK-app-1"
DASHC="ic-$STACK-dashboards-1"
PGC="ic-$STACK-postgres-1"
K="${OPS_KEY:-}"
if [ -z "$K" ]; then
  SECRETS="D:/Code repo/Intelligence corner/intelligence-corner/deploy/secrets.env"
  [ -f "$SECRETS" ] && K="$(grep '^OPS_KEY=' "$SECRETS" | head -1 | cut -d= -f2- | tr -d '\r')"
fi
[ -n "$K" ] || { echo "FAIL setup: OPS_KEY is required"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$(cd "$HERE/../.." && pwd)"
RUN="$(date +%s)"
SLUG="serve-$RUN"
FSLUG="serve-fail-$RUN"
RSLUG="serve-raw-$RUN"
W="$(mktemp -d)"
command -v cygpath >/dev/null 2>&1 && W="$(cygpath -m "$W")"
trap 'rm -rf "$W"' EXIT

PY=""
for c in python python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import json" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "FAIL setup: no working python found"; exit 1; }

fail() { echo "FAIL $1: ${2:-}"; exit 1; }
ok() { echo "ok $1"; }
expect() { [ "$2" = "$3" ] || fail "$1" "expected '$3' got '$2'"; }

api() {
  local m="$1" p="$2" f="${3:-}" out code
  out="$W/resp.$$"
  if [ -n "$f" ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$m" "$API_URL$p" -H "x-ops-key: $K" -H 'Content-Type: application/json' --data-binary "@$f")
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$m" "$API_URL$p" -H "x-ops-key: $K" -H 'Content-Type: application/json')
  fi
  case "$code" in
    2*) cat "$out" ;;
    *) echo "HTTP $code $m $p: $(head -c 300 "$out")" >&2; return 1 ;;
  esac
}

jget() {
  "$PY" -c '
import json, sys
d = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    d = d[int(part)] if isinstance(d, list) else d[part]
if isinstance(d, (dict, list, bool)) or d is None:
    print(json.dumps(d))
else:
    print(d)
' "$1"
}

# snapf QID FIELD: field of one snapshot from a version JSON on stdin.
snapf() {
  "$PY" -c '
import json, sys
d = json.load(sys.stdin)
for s in d.get("snapshots", []):
    if s["query_id"] == sys.argv[1]:
        v = s.get(sys.argv[2])
        print("" if v is None else v)
        break
' "$1" "$2"
}

# dget PATH [QUERY] [HEADER]: GET against the dashboards origin; prints the status.
dget() {
  if [ -n "${3:-}" ]; then
    curl -sS -D "$W/h" -o "$W/b" -w '%{http_code}' -H "$3" "$DASH_URL$1${2:-}"
  else
    curl -sS -D "$W/h" -o "$W/b" -w '%{http_code}' "$DASH_URL$1${2:-}"
  fi
}
rawget() { curl --path-as-is -sS -D "$W/h" -o "$W/b" -w '%{http_code}' "$1"; }
hdr() { grep -i "^$1:" "$W/h" | head -1 | cut -d: -f2- | sed 's/^ *//' | tr -d '\r'; }

wait_until() {
  local desc="$1" secs="$2" end; shift 2
  end=$(( $(date +%s) + secs ))
  while ! "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$end" ] || fail "$desc" "timed out after ${secs}s"
    sleep 3
  done
}

psqlc() { MSYS_NO_PATHCONV=1 docker exec -i "$PGC" psql -U agent -d agent_memory -v ON_ERROR_STOP=1 -At; }

vjson() { api GET "/api/dashboards/$1/versions/$2"; }
snap_refreshed() { [ -n "$(vjson "$1" "$2" | snapf "$3" refreshed_at)" ]; }

# ---- 1 origin and API up
expect "1 health" "$(curl -sS -o /dev/null -w '%{http_code}' "$API_URL/health")" 200
SERVED_AT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["libraries"][0]["served_at"])' "$APPDIR/dashboards/vendor.json")"
LIBSHA="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["libraries"][0]["files"][0]["sha256"])' "$APPDIR/dashboards/vendor.json")"
expect "1 lib status" "$(dget "$SERVED_AT")" 200
GOT="$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$W/b")"
expect "1 lib sha256" "$GOT" "$LIBSHA"
expect "1 lib nosniff" "$(hdr x-content-type-options)" nosniff
ok "1 origin and API up"

# ---- 2 person
if [ -n "${PERSON_ID:-}" ]; then
  PID="$PERSON_ID"
else
  echo '{"name": "Serve Walkthrough Operator", "role": "operator"}' > "$W/person.json"
  PID=$(api POST /api/people "$W/person.json" | jget created) || fail "2 person" "create failed"
fi
[ -n "$PID" ] || fail "2 person" "no person id"
ok "2 person"

# ---- 3 publish the contract example
"$PY" - "$APPDIR/docs/DASHBOARD_CONTRACT.md" "$W" <<'PYEOF'
import json, re, sys
src = open(sys.argv[1], encoding="utf-8").read()
html = re.search(r"```html\n(.*?)\n```", src, re.S).group(1)
queries = json.loads(re.search(r"```json\n(.*?)\n```", src, re.S).group(1))
w = sys.argv[2]
def dump(name, **kw):
    with open(f"{w}/{name}", "w", encoding="utf-8") as fh:
        json.dump(kw, fh)
dump("v1.json", title="Invoiced by customer", code=html, queries=queries, change_note="phase 4 walkthrough")
dump("v2.json", title="Invoiced by customer", code=html, queries=queries, change_note="draft")
fq = [dict(queries[0], sql="SELECT * FROM no_such_table", refresh_minutes=5)]
dump("fail.json", title="Failing query", code=html, queries=fq, change_note="failing query")
open(f"{w}/html.txt", "w", encoding="utf-8", newline="").write(html)
PYEOF
R=$(api POST "/api/dashboards/$SLUG/versions" "$W/v1.json") || fail "3 save v1"
expect "3 version" "$(echo "$R" | jget version)" 1
echo "{\"version\": 1, \"decided_by\": $PID}" > "$W/pub.json"
api POST "/api/dashboards/$SLUG/publish" "$W/pub.json" >/dev/null || fail "3 publish v1"
T=$(api GET "/api/dashboards/$SLUG" | jget dashboard.view_token)
[ -n "$T" ] || fail "3 view token" "empty"
echo "view token len=${#T}"
ok "3 publish contract example"

# ---- 4 wait for first refresh
wait_until "4 first refresh" 90 snap_refreshed "$SLUG" 1 invoiced_by_customer
V1="$(vjson "$SLUG" 1)"
expect "4 snapshot error" "$(echo "$V1" | snapf invoiced_by_customer error)" ""
ok "4 first refresh"

# ---- 5 page (wrapper), frame (content), CSP, headers
expect "5 page" "$(dget "/d/$SLUG/" "?t=$T")" 200
WRAPPER_CSP="$(hdr content-security-policy)"
case "$WRAPPER_CSP" in *"frame-src 'self'"*"frame-ancestors 'none'"*) ;; *) fail "5 wrapper csp" "frame-src/frame-ancestors missing" ;; esac
case "$WRAPPER_CSP" in *allow-same-origin*) fail "5 wrapper csp" "allow-same-origin present" ;; esac
grep -q 'sandbox="allow-scripts"' "$W/b" || fail "5 wrapper" "sandboxed iframe missing"
! grep -q "$T" "$W/b" || fail "5 wrapper" "token in the wrapper body"
expect "5 frame" "$(dget "/d/$SLUG/frame" "?t=$T")" 200
echo "$V1" | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["code"]))' > "$W/code.json"
"$PY" - "$W" <<'PYEOF' || fail "5 body" "frame body differs from stored code"
import json, re, sys
w = sys.argv[1]
stored = json.load(open(f"{w}/code.json"))
body = open(f"{w}/b", "rb").read().decode("utf-8")
body = re.sub(r"<script>\(function\(\)\{\['RTCPeerConnection'.*?</script>", "", body, count=1, flags=re.S)
body = body.replace(' crossorigin="anonymous"', "", 1)
sys.exit(0 if body == stored else 1)
PYEOF
echo "$V1" | jget script_hashes > "$W/hashes.json"
FRAME_CSP="$(hdr content-security-policy)"
"$PY" - "$W/hashes.json" "$FRAME_CSP" <<'PYEOF' || fail "5 csp" "content CSP differs"
import json, re, sys
hs = json.load(open(sys.argv[1]))
csp = sys.argv[2]
toks = " ".join("'" + h + "'" for h in hs)
m = re.fullmatch(r"default-src 'none'; script-src " + re.escape(toks) + r" '(sha256-[A-Za-z0-9+/]{43}=)'; "
    r"style-src 'unsafe-inline'; img-src data:; connect-src 'self'( http://\S+ https://\S+)?; "
    r"base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts", csp)
sys.exit(0 if m and "allow-same-origin" not in csp else 1)
PYEOF
expect "5 frame as document" "$(dget "/d/$SLUG/frame" "?t=$T" "Sec-Fetch-Dest: document")" 404
SRCDIR="$(printf '%s' "$FRAME_CSP" | tr ';' '
' | grep -F 'script-src')"
case "$SRCDIR" in *"'self'"*) fail "5 csp" "script-src carries 'self'" ;; esac
case "$FRAME_CSP" in
  *"sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="*) ;;
  *) fail "5 csp" "library hash missing" ;;
esac
expect "5 page again" "$(dget "/d/$SLUG/" "?t=$T")" 200
expect "5 referrer" "$(hdr referrer-policy)" no-referrer
expect "5 nosniff" "$(hdr x-content-type-options)" nosniff
expect "5 coop" "$(hdr cross-origin-opener-policy)" same-origin
expect "5 cache" "$(hdr cache-control)" no-store
ok "5 page, CSP, headers"

# ---- 6 data
expect "6 data" "$(dget "/d/$SLUG/data/invoiced_by_customer" "?t=$T")" 200
case "$(hdr content-type)" in application/json*) ;; *) fail "6 content-type" "$(hdr content-type)" ;; esac
expect "6 data csp" "$(hdr content-security-policy)" "default-src 'none'; frame-ancestors 'none'; sandbox"
expect "6 data cors" "$(hdr access-control-allow-origin)" "*"
"$PY" - "$W/b" <<'PYEOF' || fail "6 data shape" "unexpected snapshot JSON"
import json, re, sys
d = json.load(open(sys.argv[1]))
assert d["columns"] == ["customer", "invoiced"], d["columns"]
assert d["rows"], "no rows"
assert all(isinstance(r["invoiced"], (int, float)) for r in d["rows"])
assert re.match(r"^\d{4}-\d{2}-\d{2}T", d["refreshed_at"]), d["refreshed_at"]
assert d["error"] is None
PYEOF
ok "6 data"

# ---- 7 pinned
expect "7 pinned page" "$(dget "/d/$SLUG/v/1/" "?t=$T")" 200
expect "7 pinned data" "$(dget "/d/$SLUG/v/1/data/invoiced_by_customer" "?t=$T")" 200
ok "7 pinned"

# ---- 8 uniform 404
check404() {
  local label="$1" path="$2" q="${3:-}" code
  code="$(dget "$path" "$q")"
  expect "8 $label" "$code" 404
  expect "8 $label body" "$(tr -d '\r\n' < "$W/b")" "Not found"
}
check404 "wrong token" "/d/$SLUG/" "?t=wrong"
check404 "no token" "/d/$SLUG/"
check404 "duplicated token" "/d/$SLUG/" "?t=$T&t=$T"
check404 "empty token" "/d/$SLUG/" "?t="
check404 "unknown slug" "/d/no-such-$RUN/" "?t=$T"
check404 "unknown version" "/d/$SLUG/v/99/" "?t=$T"
check404 "bad n" "/d/$SLUG/v/abc/" "?t=$T"
check404 "undeclared qid" "/d/$SLUG/data/not_declared" "?t=$T"
CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$DASH_URL/d/$SLUG/")"
expect "8 POST" "$CODE" 403
ok "8 uniform 404"

# ---- 9 redirect
expect "9 redirect" "$(dget "/d/$SLUG" "?t=$T")" 301
LOC="$(hdr location)"
[ "$LOC" = "/d/$SLUG/?t=$T" ] || fail "9 location" "unexpected Location"
case "$LOC" in *http*) fail "9 location" "not relative" ;; esac
ok "9 redirect"

# ---- 10 proxy gate
expect "10 gateway direct" "$(curl -sS -o /dev/null -w '%{http_code}' "$API_URL/d/$SLUG/?t=$T")" 404
ok "10 proxy gate"

# ---- 11 draft and preview
R=$(api POST "/api/dashboards/$SLUG/versions" "$W/v2.json") || fail "11 save v2"
expect "11 version" "$(echo "$R" | jget version)" 2
expect "11 draft under view token" "$(dget "/d/$SLUG/v/2/" "?t=$T")" 404
P="$(MSYS_NO_PATHCONV=1 docker exec -e S="$SLUG" "$APP" python -c "
import asyncio, os
from app import db, dashboards
async def main():
    pool = await db.get_pool()
    async with pool.acquire() as c:
        r = await dashboards.mint_preview_token(c, slug=os.environ['S'], version=2, ttl_minutes=15)
        print(r['preview_token'])
asyncio.run(main())
" | tail -1 | tr -d '\r')"
[ -n "$P" ] || fail "11 preview" "mint produced nothing"
echo "preview token len=${#P}"
expect "11 preview page" "$(dget "/d/$SLUG/v/2/" "?p=$P")" 200
[ -n "$(hdr content-security-policy)" ] || fail "11 preview csp" "missing"
expect "11 preview on v1" "$(dget "/d/$SLUG/v/1/" "?p=$P")" 404
expect "11 preview on current" "$(dget "/d/$SLUG/" "?p=$P")" 404
wait_until "11 draft refresh" 90 snap_refreshed "$SLUG" 2 invoiced_by_customer
ok "11 draft and preview"

# ---- 12 rotation
T2=$(api POST "/api/dashboards/$SLUG/rotate-token" | jget view_token)
[ -n "$T2" ] || fail "12 rotate" "empty"
echo "rotated token len=${#T2}"
expect "12 old token" "$(dget "/d/$SLUG/" "?t=$T")" 404
expect "12 new token" "$(dget "/d/$SLUG/" "?t=$T2")" 200
ok "12 rotation"

# ---- 13 headless render
BROWSER=""
for c in "C:/Program Files/Google/Chrome/Application/chrome.exe" "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"; do
  [ -f "$c" ] && { BROWSER="$c"; break; }
done
if [ -z "$BROWSER" ]; then
  for c in google-chrome chromium chromium-browser; do
    command -v "$c" >/dev/null 2>&1 && { BROWSER="$(command -v "$c")"; break; }
  done
fi
if [ -z "$BROWSER" ]; then
  echo "SKIP render (no Chrome/Edge found)"
else
  # Phase 12: the page is a wrapper; the chart lives in an opaque-origin frame (separate
  # CDP target), so --dump-dom of the top page cannot see it. Use the OOPIF-aware check.
  if command -v node >/dev/null 2>&1; then
    node "$HERE/dashboard_render_check.mjs" "$DASH_URL/d/$SLUG/?t=$T2" --wait-ms 30000 > "$W/render.json" 2>/dev/null \
      || fail "13 render" "render check failed: $(head -c 300 "$W/render.json" | sed 's/?t=[^"]*//')"
    grep -q '"ok":true' "$W/render.json" || fail "13 render" "not ok"
  else
    echo "SKIP render (no node)"
  fi
  ok "13 render"
fi

# ---- 14 failing query
R=$(api POST "/api/dashboards/$FSLUG/versions" "$W/fail.json") || fail "14 save"
echo "{\"version\": 1, \"decided_by\": $PID}" > "$W/pubf.json"
api POST "/api/dashboards/$FSLUG/publish" "$W/pubf.json" >/dev/null || fail "14 publish"
snap_error() { [ "$(vjson "$FSLUG" 1 | snapf invoiced_by_customer error)" = "query failed" ]; }
wait_until "14 failing query error" 90 snap_error
FV="$(vjson "$FSLUG" 1)"
LE="$(echo "$FV" | jget last_error)"
case "$LE" in *"query failed"*"no_such_table"*) ;; *) fail "14 last_error" "unexpected: $LE" ;; esac
expect "14 other dashboard still serves" "$(dget "/d/$SLUG/data/invoiced_by_customer" "?t=$T2")" 200
ok "14 failing query"

# ---- 15 raw hand insert
RAWT="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
SQL="BEGIN;
INSERT INTO dashboards (slug, title, view_token) VALUES ('$RSLUG', 'raw', '$RAWT');
INSERT INTO dashboard_versions (dashboard_id, version, title, code, script_hashes, queries, status, published_at)
  SELECT id, 1, 'raw', '<!doctype html><title>raw</title><p>raw</p>', '{}',
  '[{\"id\":\"orders\",\"sql\":\"SELECT count(*) AS n FROM sales_orders\",\"refresh_minutes\":5},{\"id\":\"r\",\"sql\":\"SELECT pg_sleep(1) AS s\",\"refresh_minutes\":5}]'::jsonb,
  'published', now() FROM dashboards WHERE slug = '$RSLUG';
UPDATE dashboards SET current_version_id = (SELECT v.id FROM dashboard_versions v WHERE v.dashboard_id = dashboards.id AND v.version = 1) WHERE slug = '$RSLUG';
COMMIT;"
echo "$SQL" | psqlc >/dev/null || fail "15 raw insert" "psql failed"
raw_orders() { [ -n "$(vjson "$RSLUG" 1 | snapf orders refreshed_at)" ]; }
raw_rejected() { [ "$(vjson "$RSLUG" 1 | snapf r error)" = "query rejected by the read-only guardrail" ]; }
wait_until "15 seed and refresh orders" 90 raw_orders
wait_until "15 guardrail rejection" 90 raw_rejected
expect "15 raw page" "$(dget "/d/$RSLUG/" "?t=$RAWT")" 200
expect "15 raw frame" "$(dget "/d/$RSLUG/frame" "?t=$RAWT")" 200
case "$(hdr content-security-policy)" in *"script-src 'sha256-"*"'; style-src"*) ;; *) fail "15 csp" "expected only the prelude hash in script-src" ;; esac
expect "15 raw data orders" "$(dget "/d/$RSLUG/data/orders" "?t=$RAWT")" 200
"$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["rows"][0]["n"] > 0' "$W/b" || fail "15 orders" "rows[0].n not > 0"
expect "15 raw data r" "$(dget "/d/$RSLUG/data/r" "?t=$RAWT")" 200
"$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["error"]' "$W/b" || fail "15 r" "error not set"
ok "15 raw hand insert"

# ---- 16 resilience
if [ "${RESILIENCE:-0}" = "1" ]; then
  R0="$(vjson "$SLUG" 1 | snapf invoiced_by_customer refreshed_at)"
  MSYS_NO_PATHCONV=1 docker restart "$PGC" >/dev/null
  pg_back() { MSYS_NO_PATHCONV=1 docker exec "$PGC" pg_isready -U agent -d agent_memory && [ "$(curl -sS -o /dev/null -w '%{http_code}' "$API_URL/health")" = 200 ]; }
  wait_until "16 postgres back" 120 pg_back
  VID="$(echo "SELECT v.id FROM dashboard_versions v JOIN dashboards d ON d.id = v.dashboard_id WHERE d.slug = '$SLUG' AND v.version = 1;" | psqlc)"
  [ -n "$VID" ] || fail "16 version id" "not found"
  echo "UPDATE dashboard_snapshots SET next_refresh_at = now() WHERE version_id = $VID;" | psqlc >/dev/null
  advanced() {
    local r1; r1="$(vjson "$SLUG" 1 | snapf invoiced_by_customer refreshed_at)"
    "$PY" -c '
import sys
from datetime import datetime
f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
sys.exit(0 if f(sys.argv[2]) > f(sys.argv[1]) else 1)
' "$R0" "$r1"
  }
  wait_until "16 loop alive after restart" 120 advanced
  ! MSYS_NO_PATHCONV=1 docker logs --since 5m "$APP" 2>&1 | grep -q "Task exception was never retrieved" || fail "16 logs" "unretrieved task exception"
  ok "16 resilience"
else
  echo "SKIP 16 resilience (set RESILIENCE=1)"
fi

# ---- 17 logs carry no token
LP="leakprobe$RUN"
LETTERS=(a b c d e f g h i j k l)
form() { # form INDEX(1-6) PROBE
  case "$1" in
    1) echo "//d/$SLUG/?t=$2" ;;
    2) echo "/d/$SLUG%3Ft=$2" ;;
    3) echo "/d/$SLUG%3ft=$2" ;;
    4) echo "/d/$SLUG%253Ft=$2" ;;
    5) echo "/./d/$SLUG/?t=$2" ;;
    6) echo "/x/../d/$SLUG/?t=$2" ;;
  esac
}
for i in 1 2 3 4 5 6; do
  v="$LP${LETTERS[$((i-1))]}"
  expect "17 origin probe $i" "$(rawget "$DASH_URL$(form "$i" "$v")")" 404
  expect "17 origin probe $i body" "$(tr -d '\r\n' < "$W/b")" "Not found"
  expect "17 origin probe $i nosniff" "$(hdr x-content-type-options)" nosniff
  expect "17 origin probe $i referrer" "$(hdr referrer-policy)" no-referrer
  expect "17 origin probe $i coop" "$(hdr cross-origin-opener-policy)" same-origin
  expect "17 origin probe $i cache" "$(hdr cache-control)" no-store
  expect "17 origin probe $i nosniff count" "$(grep -ci '^x-content-type-options:' "$W/h" || true)" 1
  g="$LP${LETTERS[$((i+5))]}"
  expect "17 gateway probe $i" "$(rawget "$API_URL$(form "$i" "$g")")" 404
done
sleep 2
MSYS_NO_PATHCONV=1 docker logs "$DASHC" > "$W/dash.log" 2>&1 || true
MSYS_NO_PATHCONV=1 docker logs "$APP" > "$W/app.log" 2>&1 || true
for name in T T2 P RAWT; do
  val="${!name}"
  [ "$(grep -cF -- "$val" "$W/dash.log" || true)" = 0 ] || fail "17 logs" "$name found in dashboards log"
  [ "$(grep -cF -- "$val" "$W/app.log" || true)" = 0 ] || fail "17 logs" "$name found in app log"
done
for L in "${LETTERS[@]}"; do
  [ "$(grep -cF -- "$LP$L" "$W/dash.log" || true)" = 0 ] || fail "17 logs" "probe $L found in dashboards log"
  [ "$(grep -cF -- "$LP$L" "$W/app.log" || true)" = 0 ] || fail "17 logs" "probe $L found in app log"
done
for lf in dash app; do
  grep -qF "//d/$SLUG/?REDACTED" "$W/$lf.log" || fail "17 logs" "$lf log lacks masked double-slash form"
  grep -qF "/d/$SLUG?REDACTED" "$W/$lf.log" || fail "17 logs" "$lf log lacks masked encoded-query form"
done
grep -qF "?REDACTED" "$W/dash.log" || fail "17 logs" "dashboards access log lacks ?REDACTED"
ok "17 logs token-free"

echo "PASS dashboard serve walkthrough ($SLUG)"
