"""Quick 043: Railway plumbing. FalkorDB password (C5), IC_ARTIFACTS_DIR (C4), the
frontend nginx template (C1) and the gateway Dockerfile (C3). No Docker needed."""

import importlib
import re
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]
CONT = chr(92) + chr(10)          # a Dockerfile line continuation


# ---- C5: FalkorDB password ---------------------------------------------------

class _Stop(Exception):
    pass


async def _driver_kwargs(monkeypatch, password):
    """Run MemoryService._connect against a recording FalkorDriver stand-in."""
    from graphiti_core.driver import falkordb_driver
    from app import config
    from app.memoryservice import MemoryService

    seen = {}

    def fake(**kw):
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(falkordb_driver, "FalkorDriver", fake)
    s = config.get_settings()
    monkeypatch.setattr(s, "memory_graph_enabled", True, raising=False)
    monkeypatch.setattr(s, "falkordb_host", "fk.test", raising=False)
    monkeypatch.setattr(s, "falkordb_port", 6379, raising=False)
    monkeypatch.setattr(s, "falkordb_password", password, raising=False)
    svc = MemoryService()
    assert await svc._connect(s) is False        # fail-soft: _Stop is swallowed
    return seen


@pytest.mark.asyncio
async def test_password_passed_when_set(monkeypatch):
    seen = await _driver_kwargs(monkeypatch, "s3cret")
    assert seen == {"host": "fk.test", "port": "6379", "password": "s3cret"}


@pytest.mark.asyncio
async def test_call_identical_when_unset(monkeypatch):
    seen = await _driver_kwargs(monkeypatch, "")
    assert seen == {"host": "fk.test", "port": "6379"}       # no password key at all


def test_password_is_env_only_not_in_registry():
    from app import configservice
    from app.config import Settings
    assert Settings().falkordb_password == ""
    assert "falkordb_password" not in configservice.REGISTRY


def test_password_env_var(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("FALKORDB_PASSWORD", "abc")
    assert Settings().falkordb_password == "abc"


def test_no_other_falkordb_connection_point():
    """Every FalkorDriver( / FalkorDB( construction in app and scripts passes the password."""
    hits = []
    for base in ("app", "scripts"):
        for p in (APP_ROOT / "backend" / base).rglob("*.py"):
            for m in re.finditer(r"\b(FalkorDriver|FalkorDB)\(", p.read_text(encoding="utf-8")):
                hits.append(p.name)
    assert sorted(hits) == ["graph_diag.py", "memoryservice.py"]
    for name in set(hits):
        p = next((APP_ROOT / "backend").rglob(name))
        assert "falkordb_password" in p.read_text(encoding="utf-8"), name


# ---- C4: IC_ARTIFACTS_DIR ----------------------------------------------------

def _reload_sandbox():
    import app.sandbox as sb
    return importlib.reload(sb)


def test_artifacts_dir_env_moves_root(monkeypatch, tmp_path):
    default = APP_ROOT / "artifacts"
    try:
        monkeypatch.delenv("IC_ARTIFACTS_DIR", raising=False)
        assert _reload_sandbox().ARTIFACTS_ROOT == default
        monkeypatch.setenv("IC_ARTIFACTS_DIR", "")
        assert _reload_sandbox().ARTIFACTS_ROOT == default       # empty = unset
        monkeypatch.setenv("IC_ARTIFACTS_DIR", str(tmp_path / "vol"))
        assert _reload_sandbox().ARTIFACTS_ROOT == tmp_path / "vol"
    finally:
        monkeypatch.delenv("IC_ARTIFACTS_DIR", raising=False)
        _reload_sandbox()


# ---- C1: frontend nginx template ---------------------------------------------

def _code(p):
    return "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def _env(dockerfile):
    text = dockerfile.read_text(encoding="utf-8").replace(CONT, " ")
    m = re.search(r"^ENV\s+(.*)$", text, re.M)
    return dict(kv.split("=", 1) for kv in m.group(1).split())


@pytest.mark.parametrize("sub", ["frontend", "dashboards"])
def test_nginx_template_defaults_render_as_before(sub):
    conf = _code(APP_ROOT / sub / "nginx.conf")
    env = _env(APP_ROOT / sub / "Dockerfile")
    assert env["IC_GATEWAY"] == "http://app:8080"
    assert env["IC_RESOLVER_IPV6"] == "off"
    assert env["NGINX_ENTRYPOINT_LOCAL_RESOLVERS"] == "1"
    out = conf.replace("${NGINX_LOCAL_RESOLVERS}", "127.0.0.11")
    out = out.replace("${IC_RESOLVER_IPV6}", env["IC_RESOLVER_IPV6"])
    out = out.replace("${IC_GATEWAY}", env["IC_GATEWAY"])
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in out
    assert "set $gateway http://app:8080;" in out
    assert "${IC_RESOLVER" not in out and "${NGINX" not in out


def test_frontend_dockerfile_is_a_fail_closed_template():
    code = _code(APP_ROOT / "frontend" / "Dockerfile")
    assert "COPY nginx.conf /etc/nginx/templates/default.conf.template" in code
    assert "RUN rm -f /etc/nginx/conf.d/default.conf" in code
    assert _env(APP_ROOT / "frontend" / "Dockerfile")["NGINX_ENVSUBST_FILTER"] == \
        "^(IC_|NGINX_LOCAL_RESOLVERS$)"


# ---- C3: gateway Dockerfile --------------------------------------------------

def test_gateway_dockerfile():
    t = (APP_ROOT / "deploy" / "railway" / "gateway.Dockerfile").read_text(encoding="utf-8")
    code = _code(APP_ROOT / "deploy" / "railway" / "gateway.Dockerfile")
    assert "COPY skills_library /app/skills_library" in code
    assert "COPY deploy/readonly-role.sql /app/deploy/readonly-role.sql" in code
    assert "docker-ce-cli" not in code and "download.docker.com" not in code
    assert '"--host", ""' in code
    assert "0.0.0.0" not in code
    assert "docker-ce-cli" in (APP_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert t.count("FROM ") == 1


# ---- sandbox over a public https URL (separate Railway project) ---------------

@pytest.mark.asyncio
async def test_remote_sandbox_posts_to_https_url_with_bearer(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import httpx
    import app.config as config
    from app import sandbox

    seen = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            seen["verify_kw"] = kw.get("verify", True)     # httpx default is to verify TLS

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            seen["url"], seen["auth"] = url, headers["Authorization"]
            raise httpx.ConnectError("stub")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    ns = SimpleNamespace(sandbox_url="https://sandbox.example.up.railway.app/", sandbox_token="tok",
                         sandbox_memory="384m", sandbox_cpus="0.5", sandbox_pids=256)
    monkeypatch.setattr(config, "get_settings", lambda: ns)
    out = await sandbox._run_remote(1, "print(1)")
    assert seen["url"] == "https://sandbox.example.up.railway.app/run"
    assert seen["auth"] == "Bearer tok" and seen["verify_kw"] is True
    assert out.startswith("SANDBOX ERROR: cannot reach")


@pytest.mark.parametrize("sub", ["frontend", "dashboards"])
def test_empty_resolv_conf_falls_back_to_dockers_dns(sub):
    """An empty resolv.conf makes 15-local-resolvers.envsh export an empty value and nginx
    refuse to start (the VM dashboards container is host-networked). 16-... restores 127.0.0.11."""
    code = _code(APP_ROOT / sub / "Dockerfile")
    assert "16-resolver-fallback.envsh" in code
    assert "NGINX_LOCAL_RESOLVERS=127.0.0.11" in code
    assert "chmod +x /docker-entrypoint.d/16-resolver-fallback.envsh" in code
