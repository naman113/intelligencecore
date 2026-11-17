"""Phase 14 WH-01/WH-02 static and script checks: warehouse image, compose layouts, password generator, agentctl helpers."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

APP = Path(__file__).resolve().parents[2]
DOCKERFILE = APP / "infra" / "warehouse" / "Dockerfile"
BASE = APP / "docker-compose.yml"
LOCAL = APP / "docker-compose.local.yml"
VM = APP / "docker-compose.vm.yml"
ENSURE = APP / "deploy" / "ensure-warehouse-secrets.sh"
AGENTCTL = APP / "deploy" / "agentctl"
KEYS = ("WAREHOUSE_ADMIN_PASSWORD", "WH_OWNER_PASSWORD", "WH_LOADER_PASSWORD", "WH_READER_PASSWORD")


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


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _load(p: Path):
    return yaml.load(_text(p), Loader=_Loader)


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# ---- Dockerfile ----

def test_dockerfile_pins_base_and_packages():
    c = _code(_text(DOCKERFILE))
    assert c.count("FROM pgvector/pgvector:0.8.7-pg16-bookworm@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a") == 1
    assert "postgresql-16-mysql-fdw=2.9.3-" in c and "postgresql-16-tds-fdw=2.0.5-" in c
    assert "--no-install-recommends" in c and "rm -rf /var/lib/apt/lists/*" in c


def test_dockerfile_server_flags():
    c = _code(_text(DOCKERFILE))
    for flag in ("shared_preload_libraries=pg_stat_statements", "pg_stat_statements.track_utility=off",
                 "log_min_error_statement=panic", "log_statement=none", "shared_buffers=256MB",
                 "effective_cache_size=768MB", "work_mem=32MB", "maintenance_work_mem=128MB",
                 "max_parallel_workers_per_gather=2", "max_connections=50", "random_page_cost=1.1"):
        assert flag in c, flag


# ---- compose ----

def test_base_compose_warehouse():
    d = _load(BASE)
    w = d["services"]["warehouse"]
    assert w["build"]["context"] == "./infra/warehouse"
    assert w["image"] == "agentic-warehouse:16" and w["container_name"] == "agentic-warehouse"
    assert w["restart"] == "unless-stopped"
    assert w["environment"]["POSTGRES_USER"] == "wh_admin"
    assert w["environment"]["POSTGRES_PASSWORD"] == "${WAREHOUSE_ADMIN_PASSWORD:-}"
    assert w["environment"]["POSTGRES_DB"] == "warehouse"
    assert w["ports"] == ["127.0.0.1:5434:5432"]
    assert "whdata:/var/lib/postgresql/data" in w["volumes"]
    assert "host.docker.internal:host-gateway" in w["extra_hosts"]
    assert "pg_isready -U wh_admin -d warehouse" in " ".join(w["healthcheck"]["test"])
    assert w["deploy"]["resources"]["limits"] == {"cpus": "1.0", "memory": "1536M"}
    assert "whdata" in d["volumes"] and not (d["volumes"]["whdata"] or {}).get("name")
    for name, svc in d["services"].items():
        assert "warehouse" not in (svc.get("depends_on") or {}), name
    # M2 sync: Milestone 1 pinned the image (was the floating pg16 tag); still not touched by Phase 14
    assert d["services"]["postgres"]["image"] == "pgvector/pgvector:0.8.7-pg16-bookworm"
    assert d["services"]["postgres"]["ports"] == ["127.0.0.1:5433:5432"]


def test_local_overlay_warehouse():
    d = _load(LOCAL)
    w = d["services"]["warehouse"]
    assert w["ports"] == ["127.0.0.1:15434:5432"]
    assert w["deploy"]["resources"]["limits"] == {"cpus": "1.0", "memory": "1536M"}
    assert w["deploy"]["resources"]["reservations"] == {"memory": "128M"}
    assert d["services"]["app"]["environment"]["WAREHOUSE_URL"] == "postgresql://warehouse:5432/warehouse"


def test_vm_overlay_untouched():
    assert set(_load(VM)["services"]) == {"ui", "dashboards"}


def test_no_warehouse_port_binds_all_interfaces():
    for p in (BASE, LOCAL):
        for port in (_load(p)["services"].get("warehouse") or {}).get("ports", []):
            assert str(port).startswith("127.0.0.1:"), port


# ---- ensure-warehouse-secrets.sh ----

needs_tools = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("openssl")), reason="bash/openssl missing")


def _run(env_file: Path):
    return subprocess.run(["bash", str(ENSURE), str(env_file)], capture_output=True, text=True)


@needs_tools
def test_ensure_adds_all_keys(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nB=2")
    r = _run(f)
    assert r.returncode == 0, r.stderr
    t = f.read_text()
    assert t.startswith("A=1\nB=2\n")
    for k in KEYS:
        assert re.search(rf"^{k}=[0-9a-f]{{48}}$", t, re.M), k
        assert f"{k} added" in r.stdout
        val = re.search(rf"^{k}=(.*)$", t, re.M).group(1)
        assert val not in r.stdout and val not in r.stderr


@needs_tools
def test_ensure_second_run_changes_nothing(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\n")
    _run(f)
    before = f.read_bytes()
    r = _run(f)
    assert f.read_bytes() == before
    assert r.stdout.count("kept") == 4


@needs_tools
def test_ensure_fills_empty_in_place(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nWH_READER_PASSWORD=\nZ=9\n")
    _run(f)
    lines = f.read_text().splitlines()
    assert lines[0] == "A=1" and lines[2] == "Z=9"
    assert re.fullmatch(r"WH_READER_PASSWORD=[0-9a-f]{48}", lines[1])


@needs_tools
def test_ensure_never_overwrites_and_missing_file_fails(tmp_path):
    f = tmp_path / ".env"
    f.write_text("WH_OWNER_PASSWORD=keepme\n")
    _run(f)
    assert "WH_OWNER_PASSWORD=keepme\n" in f.read_text()
    assert f.read_text().count("WH_OWNER_PASSWORD=") == 1
    assert _run(tmp_path / "nope").returncode != 0


# ---- call sites ----

def test_call_sites():
    inst = _text(APP / "install.sh")
    assert inst.count("ensure-warehouse-secrets.sh") >= 2
    assert inst.count("WAREHOUSE_URL=postgresql://127.0.0.1:5434/warehouse") == 1
    assert _text(APP / "setup.sh").count("ensure-warehouse-secrets.sh") == 1
    assert "ensure-warehouse-secrets.sh" in _text(AGENTCTL)
    ps = _text(APP / "deploy" / "local.ps1")
    assert ps.count("Ensure-EnvSecret") >= 2 and "WH_READER_PASSWORD" in ps
    for f in ("install.sh", "setup.sh", "deploy/agentctl"):
        assert not re.search(r"set_env (WH_|WAREHOUSE_ADMIN)", _text(APP / f)), f
    env = _text(APP / ".env.example")
    for k in KEYS + ("WAREHOUSE_URL",):
        assert re.search(rf"^{k}=$", env, re.M), k


@needs_tools
@pytest.mark.parametrize("rel", ["deploy/ensure-warehouse-secrets.sh", "install.sh", "setup.sh", "deploy/agentctl"])
def test_bash_syntax(rel):
    r = subprocess.run(["bash", "-n", str(APP / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- agentctl compose helpers ----

def _helpers() -> str:
    t = _text(AGENTCTL)
    a = t.index("# Phase 14: WH-01 compose helpers")
    b = t.index("# end Phase 14 compose helpers")
    return t[a:b]


def _run_helpers(tmp_path, *, services: str, build_ok: bool, wh_url: str | None, call: str):
    (tmp_path / ".env").write_text(f"WAREHOUSE_URL={wh_url}\n" if wh_url else "OPS_KEY=x\n")
    log = tmp_path / "calls.log"
    script = f"""set -euo pipefail
REPO_DIR='{tmp_path.as_posix()}'
docker() {{
  echo "docker $*" >> '{log.as_posix()}'
  if [[ "$1 $2" == "compose config" ]]; then printf '%s\\n' {services}; return 0; fi
  if [[ "$*" == *"--build warehouse"* ]]; then return {0 if build_ok else 1}; fi
  return 0
}}
{_helpers()}
{call}
echo "FAILED=${{P14_WH_UP_FAILED:-unset}}"
"""
    s = tmp_path / "t.sh"
    s.write_text(script, newline="\n") if False else s.write_bytes(script.encode())
    r = subprocess.run(["bash", str(s)], capture_output=True, text=True)
    return r, (log.read_text() if log.exists() else "")


@needs_tools
def test_wh_up_failure_outside_warehouse_mode_only_warns(tmp_path):
    r, _ = _run_helpers(tmp_path, services="app postgres warehouse", build_ok=False, wh_url=None, call="_p14_wh_up")
    assert r.returncode == 0, r.stderr
    assert "WARNING:" in r.stdout and "FAILED=unset" in r.stdout


@needs_tools
def test_wh_up_failure_in_warehouse_mode_sets_flag(tmp_path):
    r, _ = _run_helpers(tmp_path, services="app warehouse", build_ok=False,
                        wh_url="postgresql://127.0.0.1:5434/warehouse", call="_p14_wh_up")
    assert r.returncode == 0, r.stderr
    assert "FAILED=1" in r.stdout


@needs_tools
def test_wh_up_skips_when_no_warehouse_service(tmp_path):
    r, log = _run_helpers(tmp_path, services="app postgres", build_ok=True, wh_url=None, call="_p14_wh_up")
    assert r.returncode == 0, r.stderr
    assert "up -d --build warehouse" not in log


@needs_tools
def test_core_services_excludes_warehouse(tmp_path):
    r, _ = _run_helpers(tmp_path, services="app warehouse postgres", build_ok=True, wh_url=None,
                        call="echo CORE=$(_p14_core_services | tr '\\n' ' ')")
    assert r.returncode == 0, r.stderr
    core = re.search(r"CORE=(.*)", r.stdout).group(1)
    assert "warehouse" not in core and "app" in core and "postgres" in core


def _arm(t: str, label: str) -> str:
    i = t.index(f"\n  {label})")
    j = t.index("\n    ;;", i)
    return t[i:j]


@pytest.mark.parametrize("label", ["update", "rollback", "resume"])
def test_agentctl_arm_order(label):
    b = _arm(_text(AGENTCTL), label)
    core = b.index("docker compose up -d --build $(_p14_core_services)")
    up = b.index("_p14_wh_up")
    restart = b.index("sudo systemctl restart agentic-api")
    assert core < up < restart
    chk = b.index('if [[ "${P14_WH_UP_FAILED:-0}" == 1 ]]; then')
    assert chk > restart and "exit 1" in b[chk:]
