#!/usr/bin/env bash
# Phase 11: GOLIVE-02. Add SETTINGS_ENCRYPTION_KEY to a .env if missing; NEVER overwrite.
# The gateway encrypts secret settings with this key. Changing or losing it makes
# stored secrets undecryptable (re-enter them on Setup). It must never be put in
# backups. Usage: ensure-settings-key.sh <path-to-.env>
set -euo pipefail
F="${1:-}"
[[ -n "$F" && -f "$F" ]] || { echo "no such .env: ${F}" >&2; exit 1; }
if grep -qE '^SETTINGS_ENCRYPTION_KEY=.+' "$F"; then
  chmod 600 "$F" 2>/dev/null || true
  echo "SETTINGS_ENCRYPTION_KEY kept"; exit 0
fi
KEY="$(openssl rand -hex 32 | tr -d '\r\n')"
if grep -qE '^SETTINGS_ENCRYPTION_KEY=$' "$F"; then
  TMP="$(mktemp "$(dirname "$F")/.envkey.XXXXXX")"
  K="$KEY" awk '/^SETTINGS_ENCRYPTION_KEY=$/ { print "SETTINGS_ENCRYPTION_KEY=" ENVIRON["K"]; next } { print }' "$F" > "$TMP"
  mv "$TMP" "$F"
else
  [[ -z "$(tail -c1 "$F")" ]] || echo >> "$F"
  echo "SETTINGS_ENCRYPTION_KEY=${KEY}" >> "$F"
fi
chmod 600 "$F" 2>/dev/null || true
echo "SETTINGS_ENCRYPTION_KEY added"
