"""Phase 4 SERVE-04 static checks over the dashboards origin files (no Docker)."""

import json
import re
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]
DASH = APP_ROOT / "dashboards"
NGINX = DASH / "nginx.conf"
DOCKERFILE = DASH / "Dockerfile"
COMPOSE = APP_ROOT / "docker-compose.local.yml"
GITATTR = APP_ROOT / ".gitattributes"
VENDOR = DASH / "vendor.json"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    """Return the body of the nginx block opened by the line starting `header`."""
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


def test_resolver_and_variable_gateway():
    t = _text(NGINX)
    assert "resolver ${NGINX_LOCAL_RESOLVERS} valid=10s ipv6=${IC_RESOLVER_IPV6};" in t
    assert "set $gateway ${IC_GATEWAY};" in t
    assert "upstream" not in re.sub(r"#.*", "", t)


def test_single_proxy_pass_in_d_block():
    t = _text(NGINX)
    code = re.sub(r"#.*", "", t)
    assert code.count("proxy_pass") == 1
    assert "proxy_pass $gateway;" in _block(t, "location /d/ {")


def test_lib_location_alias_and_headers():
    blk = _block(_text(NGINX), "location ^~ /d/_lib/ {")
    assert "alias /usr/share/nginx/d_lib/;" in blk
    assert 'add_header X-Content-Type-Options "nosniff"' in blk
    assert 'add_header Referrer-Policy "no-referrer"' in blk


def test_d_block_headers_and_methods():
    blk = _block(_text(NGINX), "location /d/ {")
    assert 'proxy_set_header X-IC-Dashboards-Proxy "1";' in blk
    assert "proxy_set_header Host $http_host;" in blk
    assert "limit_except GET" in blk


def test_catch_all_404_and_server_tokens():
    t = _text(NGINX)
    assert "return 404;" in _block(t, "location / {")
    assert "server_tokens off;" in t


def test_log_directives():
    t = _text(NGINX)
    assert "error_log /var/log/nginx/error.log crit;" in t
    assert "access_log /var/log/nginx/access.log ic_dash;" in t


def test_log_format_has_no_token_bearing_variables():
    t = _text(NGINX)
    m = re.search(r"log_format ic_dash(.*?);", t, re.S)
    assert m
    body = m.group(1)
    assert "$ic_log_uri" in body
    for bad in ("request", "request_uri", "args", "query_string", "http_referer"):
        assert not re.search(r"\$" + bad + r"\b", body), bad
    assert not re.search(r"\$arg_", body)


def test_map_masks_whole_query_string():
    t = _text(NGINX)
    m = re.search(r"map \$request_uri \$ic_log_uri \{(.*?)\n\}", t, re.S)
    assert m
    assert "?REDACTED" in m.group(1)
    assert "[A-Za-z0-9_./-]" in m.group(1)


def _map(name, source="$request_uri"):
    """(entries, default) of the nginx map `map <source> $<name> {`."""
    body = _block(_text(NGINX), f"map {source} ${name} {{")
    entries, default = [], None
    for ln in body.splitlines():
        ln = ln.strip()
        m = re.match(r'"~(.*)"\s+(.+);$', ln)
        if m:
            entries.append((re.compile(m.group(1).replace("(?<", "(?P<")), m.group(2).strip('"')))
        elif ln.startswith("default"):
            default = ln[len("default"):].strip().rstrip(";")
    return entries, default


def _apply_map(entries, default, uri):
    for rx, val in entries:
        m = rx.search(uri)
        if m:
            return re.sub(r"\$\{(\w+)\}", lambda g: m.group(g.group(1)), val)
    return uri if default == "$request_uri" else default


def test_log_map_allowlist_masks_bypass_forms():
    e, d = _map("ic_log_uri")
    cases = {
        "/d/sales/?t=SECRET": "/d/sales/?REDACTED",
        "//d/sales/?t=PROBE": "//d/sales/?REDACTED",
        "/d/sales%3Ft=PROBE": "/d/sales?REDACTED",
        "/d/sales%3ft=PROBE": "/d/sales?REDACTED",
        "/d/sales%253Ft=PROBE": "/d/sales?REDACTED",
        "/d/sales/t=PROBE": "/d/sales/t?REDACTED",
        "/x/../d/sales/?t=PROBE": "/x/../d/sales/?REDACTED",
        "/d/_lib/chart.js-4.5.1/chart.umd.min.js": "/d/_lib/chart.js-4.5.1/chart.umd.min.js",
        "/d/sales/": "/d/sales/",
    }
    for uri, want in cases.items():
        got = _apply_map(e, d, uri)
        assert got == want, uri
        assert "PROBE" not in got and "SECRET" not in got


def test_canonical_map_accepts_and_rejects():
    e, d = _map("ic_d_canonical")
    for uri in ("/d/", "/d/sales", "/d/sales/", "/d/sales?t=x", "/d/sales/?t=x",
                "/d/sales/?t=a%2Db", "/d/sales/v/2/?p=x", "/d/sales/data/orders?t=x",
                "/d/sales/v/2/data/orders?t=x", "/d/sales/v/2"):
        assert _apply_map(e, d, uri) == "1", uri
    for uri in ("//d/sales/?t=x", "/d//sales/", "/d/sales//", "/d/sales%3Ft=x",
                "/d/sales%2Fv/1/", "/./d/sales/", "/d/./sales/", "/x/../d/sales/",
                "/d/sales/../other/", "/d/sales/%2e%2e/", "/d/sales/data/orders.json?t=x"):
        assert _apply_map(e, d, uri) == "0", uri


def _if_block():
    d = _block(_text(NGINX), "location /d/ {")
    assert "default_type text/plain;" in d
    return _block(d, "if ($ic_d_canonical = 0) {")


def test_d_block_noncanonical_uniform_404():
    from app.dashboards_serve import BASE_HEADERS
    blk = _if_block()
    assert 'return 404 "Not found\\n";' in blk
    assert "proxy_pass" not in blk
    for k, v in BASE_HEADERS.items():
        assert blk.count(f'add_header {k} "{v}" always;') == 1, k


def test_nginx_404_parity_with_gateway():
    from app.dashboards_serve import not_found
    m = re.search(r'return 404 "(.*)";', _if_block())
    assert m
    assert m.group(1).replace("\\n", "\n").encode() == not_found().body


def test_no_carriage_returns():
    for p in (NGINX, DOCKERFILE):
        assert b"\r" not in p.read_bytes(), p.name


def test_dockerfile_instructions():
    lines = [ln.strip() for ln in _text(DOCKERFILE).splitlines() if not ln.strip().startswith("#")]
    code = "\n".join(lines)
    assert "FROM nginx:alpine" in code
    assert "COPY nginx.conf /etc/nginx/templates/default.conf.template" in code
    assert "COPY vendor/ /usr/share/nginx/d_lib/" in code
    assert "EXPOSE 80" in code
    for ln in lines:
        if ln.startswith("COPY"):
            assert "vendor.json" not in ln and "README" not in ln
            assert not re.match(r"COPY\s+\.\s", ln)


def test_healthcheck_matches_vendor_pin():
    served = json.loads(_text(VENDOR))["libraries"][0]["served_at"]
    m = re.search(r"HEALTHCHECK.*?http://127\.0\.0\.1(/\S+)", _text(DOCKERFILE), re.S)
    assert m and m.group(1) == served


def test_alias_mapping_matches_vendor_files():
    for lib in json.loads(_text(VENDOR))["libraries"]:
        rel = lib["served_at"].replace("/d/_lib/", "vendor/", 1)
        assert (DASH / rel).is_file(), rel


def test_gitattributes_phase4_block():
    t = _text(GITATTR).replace("\r\n", "\n")
    assert "# Phase 4" in t
    assert re.search(r"# Phase 4[^\n]*\ndashboards/nginx\.conf text eol=lf", t)
    assert t.index("# Phase 1: vendored dashboard libraries") < t.index("# Phase 4")
    assert "dashboards/vendor/** binary" in t

# ---- compose ---------------------------------------------------------------

class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_Loader.add_constructor("!override", _tag)
_Loader.add_constructor("!reset", _tag)


def _compose():
    return yaml.load(_text(COMPOSE), Loader=_Loader)


def test_compose_dashboards_service():
    d = _compose()["services"]["dashboards"]
    assert d["build"]["context"] == "./dashboards"
    assert d["container_name"] == "agentic-dashboards"
    assert d["restart"] == "unless-stopped"
    assert "app" in d["depends_on"]
    assert d["ports"] == ["127.0.0.1:8091:80"]
    assert d["deploy"]["resources"]["limits"]["memory"] == "128M"
    assert "networks" not in d


def test_compose_other_ports_unchanged():
    s = _compose()["services"]
    assert s["app"]["ports"] == ["127.0.0.1:8080:8080"]
    assert s["ui"]["ports"] == ["0.0.0.0:8090:80"]


def test_no_public_binding_of_gateway_port():
    for name, svc in _compose()["services"].items():
        for p in svc.get("ports", []) or []:
            parts = str(p).split(":")
            if parts[-1] == "8080":
                assert parts[0] == "127.0.0.1", (name, p)


def test_compose_phase4_comment_precedes_block():
    t = _text(COMPOSE)
    i = t.index("  dashboards:\n")
    assert "Phase 4" in t[:i].rsplit("\n\n", 1)[-1]


# ---- Phase 9 (DEPLOY-02) ----------------------------------------------------

def _code(p):
    return "\n".join(ln for ln in _text(p).splitlines() if not ln.strip().startswith("#"))


def _dockerfile_env():
    m = re.search(r"^ENV\s+((?:.*\\\n)*.*)$", _code(DOCKERFILE), re.M)
    assert m
    flat = m.group(1).replace("\\\n", " ")
    return dict(kv.split("=", 1) for kv in flat.split())


def test_p9_template_variables():
    t = _text(NGINX)
    assert "listen ${IC_LISTEN};" in t
    assert "set $gateway ${IC_GATEWAY};" in t
    assert "resolver ${NGINX_LOCAL_RESOLVERS} valid=10s ipv6=${IC_RESOLVER_IPV6};" in t
    for name in re.findall(r"\$\{(\w+)\}", _code(NGINX)):
        if re.search(r"[A-Z]", name):
            assert name in ("IC_LISTEN", "IC_GATEWAY", "IC_RESOLVER_IPV6",
                            "NGINX_LOCAL_RESOLVERS"), name


def test_p9_rendered_defaults():
    env = _dockerfile_env()
    out = _text(NGINX).replace("${IC_LISTEN}", env["IC_LISTEN"]).replace(
        "${IC_GATEWAY}", env["IC_GATEWAY"]).replace(
        "${IC_RESOLVER_IPV6}", env["IC_RESOLVER_IPV6"]).replace(
        "${NGINX_LOCAL_RESOLVERS}", "127.0.0.11")    # what resolv.conf gives in compose
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in out      # Quick 043: as before
    assert "listen 80;" in out
    assert "set $gateway http://app:8080;" in out
    assert "${IC_" not in out
    assert env["NGINX_ENVSUBST_FILTER"] == "^(IC_|NGINX_LOCAL_RESOLVERS$)"
    assert env["NGINX_ENTRYPOINT_LOCAL_RESOLVERS"] == "1"


def test_p9_realip():
    c = _code(NGINX)
    assert re.findall(r"set_real_ip_from\s+(\S+);", c) == ["127.0.0.1", "::1"]
    assert "real_ip_header X-Forwarded-For;" in c
    assert "real_ip_recursive on" not in c


def test_p9_gzip():
    c = _code(NGINX)
    for d in ("gzip on;", "gzip_vary on;", "gzip_proxied any;", "gzip_min_length 256;"):
        assert d in c
    m = re.search(r"gzip_types([^;]*);", c)
    assert m
    for ty in ("application/json", "application/javascript", "text/javascript",
               "text/css", "text/plain"):
        assert ty in m.group(1).split()


def test_p9_zones_and_status():
    c = _code(NGINX)
    assert "limit_req_zone $binary_remote_addr zone=ic_d:10m rate=20r/s;" in c
    assert "limit_req_zone $ic_d_data_key zone=ic_d_data:10m rate=10r/s;" in c
    assert "limit_req_status 429;" in c
    assert "error_page 429 @ic_429;" in c


def test_p9_limit_req_placement():
    t = _text(NGINX)
    d = _block(t, "location /d/ {")
    assert "limit_req zone=ic_d burst=60 nodelay;" in d
    assert "limit_req zone=ic_d_data burst=50 nodelay;" in d
    assert _code(NGINX).count("proxy_pass $gateway;") == 1
    assert "limit_req" not in _block(t, "location ^~ /d/_lib/ {")


def test_p9_data_key_map():
    e, d = _map("ic_d_data_key", source="$uri")
    assert d == '""'
    for uri in ("/d/sales/data/orders", "/d/sales/v/2/data/orders"):
        assert _apply_map(e, d, uri) == "$binary_remote_addr", uri
    for uri in ("/d/", "/d/sales/", "/d/sales/v/2/", "/d/_lib/chart.js-4.5.1/chart.umd.min.js"):
        assert _apply_map(e, d, uri) == '""', uri


def test_p9_429_location():
    from app.dashboards_serve import BASE_HEADERS
    blk = _block(_text(NGINX), "location @ic_429 {")
    assert "default_type text/plain;" in blk
    assert 'return 429 "Too many requests\\n";' in blk
    assert "proxy_pass" not in blk
    for k, v in BASE_HEADERS.items():
        assert blk.count(f'add_header {k} "{v}" always;') == 1, k


def test_p9_comment_and_error_log():
    t = _text(NGINX)
    assert "# Phase 9" in t
    assert "error_log /var/log/nginx/error.log crit;" in t


def test_p9_dockerfile():
    code = _code(DOCKERFILE)
    env = _dockerfile_env()
    assert env["IC_LISTEN"] == "80" and env["IC_GATEWAY"] == "http://app:8080"
    assert "RUN rm -f /etc/nginx/conf.d/default.conf" in code
    assert "COPY nginx.conf /etc/nginx/templates/default.conf.template" in code
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" not in code
    assert not re.search(r"^(CMD|ENTRYPOINT)\b", code, re.M)
    assert "HEALTHCHECK --interval=30s --timeout=3s --start-period=5s" in code


def test_p12_lib_cors_for_opaque_frame():
    t = _text(NGINX)
    blk = _block(t, "location ^~ /d/_lib/ {")
    assert 'add_header Access-Control-Allow-Origin "*" always;' in blk
    assert 'add_header Cross-Origin-Resource-Policy "same-origin" always;' in blk
    assert "Access-Control-Allow-Credentials" not in t
