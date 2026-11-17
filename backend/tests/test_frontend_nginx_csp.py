"""Phase 12 (WR-12): the ops UI nginx sends one CSP everywhere; Caddy guards the legacy console."""

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
NGINX = (APP_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
CADDY = (APP_ROOT / "deploy" / "Caddyfile.tmpl").read_text(encoding="utf-8")

CSP_RE = re.compile(r'add_header Content-Security-Policy "([^"]*)" always;')
LOC_RE = re.compile(r"^[ 	]*location\s+([^{]+?)\s*\{([^}]*)\}", re.M)


def _block(text: str, header: str) -> str:
    start = text.index(header)
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    raise AssertionError("unbalanced braces")


def test_server_and_every_add_header_location_carry_the_headers():
    server = _block(NGINX, "server {")
    server_level = LOC_RE.sub("", server)
    assert CSP_RE.search(server_level)
    assert 'add_header Referrer-Policy "no-referrer" always;' in server_level
    assert 'add_header X-Content-Type-Options "nosniff" always;' in server_level
    checked = 0
    for m in LOC_RE.finditer(server):
        name, blk = m.group(1), m.group(2)
        if "add_header" not in blk:
            continue
        checked += 1
        assert CSP_RE.search(blk), name
        assert 'add_header Referrer-Policy "no-referrer" always;' in blk, name
        assert 'add_header X-Content-Type-Options "nosniff" always;' in blk, name
    assert checked >= 3  # manifest, /assets/, /


def test_policy_content_and_identical_everywhere():
    found = CSP_RE.findall(NGINX)
    assert len(found) >= 4 and len(set(found)) == 1
    pol = found[0]
    for need in ("frame-ancestors 'none'", "script-src 'self'", "img-src 'self' data:",
                 "frame-src 'none'", "object-src 'none'", "base-uri 'none'"):
        assert need in pol
    script = [d for d in pol.split(";") if d.strip().startswith("script-src")][0]
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script


def test_caddy_legacy_console_header_policy():
    app = _block(CADDY, "__APP_SITE__")
    assert "@legacy_console path /dashboard" in app
    blk = _block(app, "header @legacy_console")
    assert "defer" in blk
    assert "Content-Security-Policy \"frame-ancestors 'none'; object-src 'none'; base-uri 'none'\"" in blk
    assert 'Referrer-Policy "no-referrer"' in blk
    assert 'X-Content-Type-Options "nosniff"' in blk
    assert "script-src" not in blk
    assert app.index("header @legacy_console") < app.index("handle @gateway")
    assert "@legacy_console" not in _block(CADDY, "__DASH_SITE__")
