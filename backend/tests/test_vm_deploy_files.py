"""Phase 9 DEPLOY-01 static checks over the client-VM deployment files (no Docker, no network)."""

import json
import re
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]
VM = APP_ROOT / "docker-compose.vm.yml"
BASE = APP_ROOT / "docker-compose.yml"
LOCAL = APP_ROOT / "docker-compose.local.yml"
DF = APP_ROOT / "dashboards" / "Dockerfile"
NGINX = APP_ROOT / "dashboards" / "nginx.conf"
VENDOR = APP_ROOT / "dashboards" / "vendor.json"
TMPL = APP_ROOT / "deploy" / "Caddyfile.tmpl"
GITATTR = APP_ROOT / ".gitattributes"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


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


def _load(p: Path):
    return yaml.load(_text(p), Loader=_Loader)


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# ---- docker-compose.vm.yml ----

def test_vm_services_are_only_ui_and_dashboards():
    d = _load(VM)
    assert set(d["services"]) == {"ui", "dashboards"}
    assert "extends:" not in _code(_text(VM))


def test_vm_ui_service():
    ui = _load(VM)["services"]["ui"]
    assert ui["build"]["context"] == "./frontend"
    assert ui["container_name"] == "agentic-ui"
    assert ui["restart"] == "unless-stopped"
    assert ui["ports"] == ["127.0.0.1:8090:80"]
    assert "depends_on" not in ui
    assert ui["deploy"]["resources"]["limits"]["memory"] == "128M"


def test_vm_dashboards_service():
    ds = _load(VM)["services"]["dashboards"]
    assert ds["build"]["context"] == "./dashboards"
    assert ds["container_name"] == "agentic-dashboards"
    assert ds["restart"] == "unless-stopped"
    assert ds["network_mode"] == "host"
    assert "ports" not in ds and "depends_on" not in ds
    env = ds["environment"]
    assert env["IC_LISTEN"] == "127.0.0.1:8091"
    assert env["IC_GATEWAY"].startswith("http://127.0.0.1:")
    assert "${APP_PORT:-8080}" in env["IC_GATEWAY"]


def test_vm_dashboards_healthcheck_probes_loopback_8091():
    served = json.loads(_text(VENDOR))["libraries"][0]["served_at"]
    test = _load(VM)["services"]["dashboards"]["healthcheck"]["test"]
    assert isinstance(test, list)
    assert test[-1] == "http://127.0.0.1:8091" + served


def test_vm_build_contexts_match_local_overlay():
    vm, loc = _load(VM)["services"], _load(LOCAL)["services"]
    for s in ("ui", "dashboards"):
        assert vm[s]["build"]["context"] == loc[s]["build"]["context"]


def test_vm_dashboards_env_declared_in_dockerfile_and_nginx():
    env = _load(VM)["services"]["dashboards"]["environment"]
    df, ng = _text(DF), _text(NGINX)
    for key in env:
        assert re.search(r"^ENV .*\b%s=" % key, df, re.M), key
        assert "${%s}" % key in ng, key


def test_vm_no_docker_sock_and_no_wildcard_bind():
    t = _text(VM)
    assert "/var/run/docker.sock" not in _code(t)
    assert "0.0.0.0" not in _code(t)


def test_base_compose_has_no_app_ui_dashboards_whatsapp():
    assert not ({"app", "ui", "dashboards", "whatsapp"} & set(_load(BASE)["services"]))


def test_vm_header_rules_and_lf():
    raw = VM.read_bytes()
    t = raw.decode("utf-8")
    assert "NEVER add `app`" in t
    assert "NEVER combine this file with docker-compose.local.yml" in t
    assert b"\r" not in raw


# ---- deploy/Caddyfile.tmpl and .gitattributes ----

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


def render(tmpl: str, **values) -> str:
    for k, v in values.items():
        tmpl = tmpl.replace("__%s__" % k, v)
    return tmpl


def _app():
    return _block(_text(TMPL), "\n__APP_SITE__ {")


def _dash():
    return _block(_text(TMPL), "\n__DASH_SITE__ {")


def test_tmpl_placeholders():
    t = _text(TMPL)
    for p in ("__APP_SITE__", "__DASH_SITE__", "__APP_PORT__", "__TLS__"):
        assert p in t
    assert not re.search(r"\{(APP_SITE|DASH_SITE|APP_PORT|TLS)\}", t)


def test_tmpl_exactly_two_sites():
    lines = [l for l in _text(TMPL).splitlines() if l.rstrip().endswith("{") and not l.startswith((" ", "\t", "#"))]
    assert sorted(l.strip() for l in lines) == ["__APP_SITE__ {", "__DASH_SITE__ {"]


def test_tmpl_app_site_routes():
    b = _app()
    assert "__TLS__" in b
    assert "request_header -X-IC-Dashboards-Proxy" in b
    assert "@gateway path /api/* /health /dashboard /webhook/*" in b.splitlines() or \
        any(l.strip() == "@gateway path /api/* /health /dashboard /webhook/*" for l in b.splitlines())
    assert "reverse_proxy 127.0.0.1:__APP_PORT__" in _block(b, "handle @gateway {")
    d = _block(b, "handle /d/* {")
    assert "respond 404" in d and "reverse_proxy" not in d
    assert "reverse_proxy 127.0.0.1:8090" in _block(b, "\thandle {")
    assert b.index("handle @gateway") < b.index("handle /d/*") < b.index("\thandle {")


def test_tmpl_dash_site():
    b = _dash()
    assert "__TLS__" in b
    assert "request_header -X-IC-Dashboards-Proxy" in b
    assert re.findall(r"reverse_proxy .*", b) == ["reverse_proxy 127.0.0.1:8091"]
    assert "__APP_PORT__" not in b and "8090" not in b


def test_tmpl_no_log_no_encode_strip_twice():
    c = _code(_text(TMPL))
    assert not re.search(r"^\s*log\b", c, re.M)
    assert "encode" not in c
    assert c.count("request_header -X-IC-Dashboards-Proxy") == 2


def test_tmpl_renders_clean():
    out = render(_text(TMPL), APP_SITE="app.example.test", DASH_SITE="dash.example.test", APP_PORT="8080", TLS="")
    assert "__" not in out
    assert "@gateway path /api/* /health /dashboard /webhook/*" in out


def test_tmpl_lf_and_gitattributes():
    assert b"\r" not in TMPL.read_bytes()
    lines = _text(GITATTR).splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("# Phase 9"))
    assert lines[i + 1] == "deploy/Caddyfile.tmpl text eol=lf"
    assert i > next(n for n, l in enumerate(lines) if l.startswith("# Phase 4"))


# ---- install.sh ----
INSTALL = APP_ROOT / "install.sh"
AGENTCTL = APP_ROOT / "deploy" / "agentctl"


def _noncomment(text: str) -> list[str]:
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def test_install_required_tokens():
    t = _text(INSTALL)
    for tok in (
        "docker-compose.yml:docker-compose.vm.yml", "COMPOSE_PATH_SEPARATOR", "DASHBOARDS_PUBLIC_URL",
        "set_env", "--tls-internal", "--app-host", "--dash-host", "--render-caddyfile",
        "deploy/Caddyfile.tmpl", "caddy validate", "iproute2",
    ):
        assert tok in t, tok


def test_install_render_exit_before_root_check():
    t = _text(INSTALL)
    assert t.index("RENDER_ONLY") < t.index("EUID")
    assert t.index("exit 0") < t.index("$EUID")


def test_install_compose_build_and_systemd_lines():
    nc = _noncomment(_text(INSTALL))
    assert any("docker compose up -d --build" in l for l in nc)
    pre = [l for l in nc if l.startswith("ExecStartPre=")]
    assert any("--no-build" in l for l in pre)
    assert "ExecStartPre=-/usr/bin/docker compose -f ${REPO_DIR}/docker-compose.yml up -d" in pre
    starts = [l for l in nc if l.startswith("ExecStart=") and "agentctl backup" not in l]  # Phase 11: backup timer unit has its own ExecStart
    assert len(starts) == 1 and "--host 127.0.0.1 --port ${APP_PORT}" in starts[0]


def test_install_no_local_overlay_no_wildcard_no_inline_proxy():
    nc = "\n".join(_noncomment(_text(INSTALL)))
    assert "docker-compose.local.yml" not in nc
    assert "0.0.0.0" not in nc
    assert "reverse_proxy 127.0.0.1:${APP_PORT}" not in nc


def test_install_keeps_two_secrets_and_meta_keys():
    t = _text(INSTALL)
    assert "Admin key:" in t and "Webhook key:" in t
    for k in ("APP_URL=", "DASH_URL=", "DOMAIN=", "IP_ONLY=", "APP_PORT=", "SITE="):
        assert f'echo "{k}${{' in t and 'deploy/.installmeta' in t


def test_install_lf():
    assert b"\r" not in INSTALL.read_bytes()


# ---- deploy/agentctl ----
def _case(text: str, label: str) -> str:
    lines = text.splitlines()
    i = next(n for n, l in enumerate(lines) if re.match(rf"^\s{{2}}{label}\)", l))
    out = [lines[i]]
    for l in lines[i + 1:]:
        out.append(l)
        if l.strip() == ";;":
            break
    return "\n".join(out)


def test_agentctl_rebuilds():
    t = _text(AGENTCTL)
    for label in ("update", "rollback", "resume"):
        b = _case(t, label)
        assert "docker compose up -d --build" in b, label
        assert not re.search(r"docker compose up -d(?! --build)", b), label


def test_agentctl_rollback_guard_before_checkout():
    b = _case(_text(AGENTCTL), "rollback")
    assert b.index("cat-file -e") < b.index("git checkout")
    assert b.index("docker-compose.vm.yml") < b.index("git checkout")


def test_agentctl_status_and_url():
    t = _text(AGENTCTL)
    s = _case(t, "status")
    for tok in ("127.0.0.1:8090", "127.0.0.1:8091/d/", "GATEWAY UNREACHABLE", "not provisioned", "public listeners"):
        assert tok in s, tok
    assert "ss -H -ltn" in t
    u = _case(t, "url")
    for tok in ("/dashboard", "/webhook/whatsapp", "DASH_URL"):
        assert tok in u, tok
    assert "docker image prune -f" in _case(t, "update")


def test_agentctl_clean():
    t = _text(AGENTCTL)
    assert "docker-compose.local.yml" not in "\n".join(_noncomment(t))
    assert b"\r" not in AGENTCTL.read_bytes()


def test_backend_python_pinned_to_dockerfile_minor():
    """A fresh VM has no compiler: uv must not pick a newer Python that lacks wheels (found by 09-vm-check)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pin = (root / ".python-version").read_text().strip()
    assert pin == "3.12"
    assert "python:3.12" in (root / "Dockerfile").read_text()


def test_tmpl_both_sites_handle_errors():
    # WR-01: keeps Caddy's default error logger from writing URIs/X-Ops-Key on 5xx.
    assert "handle_errors {" in _app()
    assert "handle_errors {" in _dash()
