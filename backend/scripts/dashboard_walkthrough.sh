#!/usr/bin/env bash
# Curl-only walkthrough of the dashboard ops API (DASH-02, DASH-03, DASH-04):
# create, publish, draft without moving the pointer, publish, roll back, rotate
# the view token and list, with the ops key and no agent involved.
# Usage: BASE_URL=http://localhost:9031 OPS_KEY=... bash backend/scripts/dashboard_walkthrough.sh
# Optional: PERSON_ID=<existing people.id> to avoid creating a person.
set -euo pipefail

B="${BASE_URL:-http://localhost:8080}"
K="${OPS_KEY:-}"
[ -n "$K" ] || { echo "FAIL setup: OPS_KEY is required"; exit 1; }
SLUG="walkthrough-$(date +%s)"
W="$(mktemp -d)"
command -v cygpath >/dev/null 2>&1 && W="$(cygpath -m "$W")"
trap 'rm -rf "$W"' EXIT

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import json" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "FAIL setup: no working python3/python found for JSON parsing"; exit 1; }

LIBHASH="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="

fail() { echo "FAIL $1: ${2:-}"; exit 1; }

# api METHOD PATH [BODYFILE]: prints the body, fails on non-2xx.
api() {
  local m="$1" p="$2" f="${3:-}" out code
  out="$W/resp.$$"
  if [ -n "$f" ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$m" "$B$p" -H "x-ops-key: $K" -H 'Content-Type: application/json' --data-binary "@$f")
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$m" "$B$p" -H "x-ops-key: $K" -H 'Content-Type: application/json')
  fi
  case "$code" in
    2*) cat "$out" ;;
    *) echo "HTTP $code $m $p: $(head -c 300 "$out")" >&2; return 1 ;;
  esac
}

# code_of METHOD PATH [BODYFILE]: prints only the HTTP status.
code_of() {
  local m="$1" p="$2" f="${3:-}"
  if [ -n "$f" ]; then
    curl -sS -o /dev/null -w '%{http_code}' -X "$m" "$B$p" -H "x-ops-key: $K" -H 'Content-Type: application/json' --data-binary "@$f"
  else
    curl -sS -o /dev/null -w '%{http_code}' -X "$m" "$B$p" -H "x-ops-key: $K" -H 'Content-Type: application/json'
  fi
}

# jget a.b.0.c: read JSON on stdin, print the field (JSON words for bool/null/containers).
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

expect() { [ "$2" = "$3" ] || fail "$1" "expected '$3' got '$2'"; }

# body FILE JSON-LITERAL: write a request body (validated JSON).
body() { "$PY" -c 'import json,sys; sys.stdout.write(json.dumps(json.loads(sys.argv[1])))' "$2" > "$1"; }

# mkjson FILE TITLE NOTE INLINE_JS: a dashboard version with the Phase 1 library line plus one inline script.
mkjson() {
  "$PY" - "$1" "$2" "$3" "$4" "$LIBHASH" <<'PYEOF'
import json, sys
path, title, note, js, lib = sys.argv[1:6]
code = (
    '<!doctype html><html><head><meta charset="utf-8"><title>' + title + '</title>'
    '<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" integrity="' + lib + '"></script></head>'
    '<body><div id="app"></div><script>' + js + '</script></body></html>'
)
q = [{"id": "invoiced_by_customer",
      "sql": "SELECT c.name AS customer, SUM(i.total) AS invoiced FROM invoices i JOIN customers c ON c.id = i.customer_id GROUP BY c.name ORDER BY 2 DESC LIMIT 20",
      "refresh_minutes": 15}]
with open(path, "w") as fh:
    json.dump({"title": title, "change_note": note, "code": code, "queries": q}, fh)
PYEOF
}

pubbody() { body "$W/pub.json" "{\"version\": $1, \"decided_by\": $PID, \"note\": \"walkthrough\"}"; }

# ---- 1 health and auth
expect "1 health" "$(curl -sS -o /dev/null -w '%{http_code}' "$B/health")" 200
expect "1 auth" "$(curl -sS -o /dev/null -w '%{http_code}' "$B/api/dashboards")" 401

# ---- 2 person
if [ -n "${PERSON_ID:-}" ]; then
  PID="$PERSON_ID"
else
  body "$W/person.json" '{"name": "Walkthrough Operator", "role": "operator"}'
  PID=$(api POST /api/people "$W/person.json" | jget created) || fail "2 person" "create failed"
fi
[ -n "$PID" ] || fail "2 person" "no person id"

# ---- 3 save v1
mkjson "$W/v1.json" "Invoiced by customer" "first cut" "document.getElementById('app').textContent = 'v1';"
R=$(api POST "/api/dashboards/$SLUG/versions" "$W/v1.json") || fail "3 save v1"
expect "3 version" "$(echo "$R" | jget version)" 1
expect "3 status" "$(echo "$R" | jget status)" draft
expect "3 created" "$(echo "$R" | jget created)" true
D=$(api GET "/api/dashboards/$SLUG") || fail "3 detail"
expect "3 pointer null" "$(echo "$D" | jget dashboard.current_version)" null

# ---- 4 publish v1
pubbody 1
R=$(api POST "/api/dashboards/$SLUG/publish" "$W/pub.json") || fail "4 publish v1"
expect "4 version" "$(echo "$R" | jget version)" 1
[ -n "$(echo "$R" | jget action_id)" ] || fail "4 action_id"

# ---- 5 save v2, pointer stays on v1
mkjson "$W/v2.json" "Invoiced by customer" "second cut" "document.getElementById('app').textContent = 'v2';"
R=$(api POST "/api/dashboards/$SLUG/versions" "$W/v2.json") || fail "5 save v2"
expect "5 version" "$(echo "$R" | jget version)" 2
expect "5 status" "$(echo "$R" | jget status)" draft
D=$(api GET "/api/dashboards/$SLUG") || fail "5 detail"
expect "5 pointer unchanged" "$(echo "$D" | jget dashboard.current_version)" 1

# ---- 6 publish v2
pubbody 2
R=$(api POST "/api/dashboards/$SLUG/publish" "$W/pub.json") || fail "6 publish v2"
expect "6 from_version" "$(echo "$R" | jget from_version)" 1
V1=$(api GET "/api/dashboards/$SLUG/versions/1") || fail "6 get v1"
V2=$(api GET "/api/dashboards/$SLUG/versions/2") || fail "6 get v2"
expect "6 v1 retired" "$(echo "$V1" | jget status)" retired
expect "6 v2 published" "$(echo "$V2" | jget status)" published
NH=$(echo "$V2" | jget script_hashes | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)))')
expect "6 v2 hash count" "$NH" 2
expect "6 lib hash first" "$(echo "$V2" | jget script_hashes.0)" "$LIBHASH"
[ "$(echo "$V2" | jget script_hashes.1)" != "$(echo "$V1" | jget script_hashes.1)" ] || fail "6 inline hash" "inline hash did not change"

# ---- 7 publish of a non-draft is refused
pubbody 1
expect "7 republish v1" "$(code_of POST "/api/dashboards/$SLUG/publish" "$W/pub.json")" 409

# ---- 8 rollback to v1
body "$W/rb.json" "{\"version\": 1, \"decided_by\": $PID, \"note\": \"walkthrough rollback\"}"
R=$(api POST "/api/dashboards/$SLUG/rollback" "$W/rb.json") || fail "8 rollback"
expect "8 version" "$(echo "$R" | jget version)" 1
expect "8 from_version" "$(echo "$R" | jget from_version)" 2
D=$(api GET "/api/dashboards/$SLUG") || fail "8 detail"
expect "8 current" "$(echo "$D" | jget dashboard.current_version)" 1
V2=$(api GET "/api/dashboards/$SLUG/versions/2") || fail "8 get v2"
expect "8 v2 retired" "$(echo "$V2" | jget status)" retired

# ---- 9 error paths
expect "9 rollback current" "$(code_of POST "/api/dashboards/$SLUG/rollback" "$W/rb.json")" 409
body "$W/rb99.json" "{\"version\": 99, \"decided_by\": $PID}"
expect "9 rollback 99" "$(code_of POST "/api/dashboards/$SLUG/rollback" "$W/rb99.json")" 404
body "$W/bad.json" "{\"version\": true, \"decided_by\": $PID}"
expect "9 publish bool" "$(code_of POST "/api/dashboards/$SLUG/publish" "$W/bad.json")" 422

# ---- 10 rotate token and list
OLD=$(echo "$D" | jget dashboard.view_token)
R=$(api POST "/api/dashboards/$SLUG/rotate-token") || fail "10 rotate"
NEW=$(echo "$R" | jget view_token)
[ "$NEW" != "$OLD" ] || fail "10 rotate" "token unchanged"
expect "10 token length" "${#NEW}" 43
L=$(api GET "/api/dashboards?limit=500") || fail "10 list"
echo "$L" | grep -q "$SLUG" || fail "10 list" "slug missing"
if echo "$L" | grep -q view_token; then fail "10 list" "list leaks view_token"; fi
echo "token rotated (length ${#NEW})"

# ---- 11 audit rows
A=$(api GET "/api/actions?status=executed&limit=200") || fail "11 actions"
echo "$A" | "$PY" -c '
import json, sys
slug, pid = sys.argv[1], str(sys.argv[2])
rows = json.load(sys.stdin)
got = []
for r in rows:
    if r.get("kind") != "publish_dashboard":
        continue
    p = r["payload"]
    if isinstance(p, str):
        p = json.loads(p)
    if p.get("slug") != slug:
        continue
    if r.get("status") != "executed" or str(r.get("decided_by")) != pid:
        print("bad action row", r.get("id")); sys.exit(1)
    got.append((p["op"], p["version"]))
want = sorted([("publish", 1), ("publish", 2), ("rollback", 1)])
if sorted(got) != want:
    print("actions mismatch", sorted(got)); sys.exit(1)
' "$SLUG" "$PID" || fail "11 audit" "publish_dashboard actions wrong"

echo "PASS dashboard walkthrough ($SLUG)"
