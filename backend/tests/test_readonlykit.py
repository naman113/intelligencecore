"""Unit tests for the Phase 11 read-only role kit (GOLIVE-03). No database needed."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import asyncpg
import pytest

from app import readonlykit as rk

ROOT = Path(__file__).resolve().parents[2]
AGENTCTL = ROOT / "deploy" / "agentctl"
KIT = ROOT / "deploy" / "readonly-role.sql"

PACK = {
    "meta": {"schemas_in_scope": ["public", "billing"]},
    "hide_tables": [
        {"ref": "business_db.public.api_tokens", "recommended_action": "revoke_select"},
        {"ref": "business_db.public.sessions", "recommended_action": "hide"},
        {"ref": "business_db.billing.cards", "recommended_action": "revoke_select"},
        {"ref": "business_db.public.api_tokens", "recommended_action": "revoke_select"},
    ],
}


def test_parse_pack():
    assert rk.parse_pack(PACK) == (["public", "billing"], ["public.api_tokens", "billing.cards"])


def test_parse_pack_bad_ref():
    with pytest.raises(ValueError, match=r"hide_tables\[1\]"):
        rk.parse_pack({"hide_tables": [
            {"ref": "business_db.public.a", "recommended_action": "hide"},
            {"ref": "warehouse:public.x"}]})
    with pytest.raises(ValueError, match=r"hide_tables\[0\]"):
        rk.parse_pack({"hide_tables": [{"ref": "business_db.public"}]})


def test_parse_pack_no_meta():
    assert rk.parse_pack({"hide_tables": []}) == ([], [])


def test_parse_pack_bad_identifier_in_ref():
    with pytest.raises(ValueError):
        rk.parse_pack({"hide_tables": [
            {"ref": 'business_db.public.a"b', "recommended_action": "revoke_select"}]})


@pytest.mark.parametrize("s", ["public", "Orders_2024", "a$b"])
def test_ident_ok(s):
    assert rk.validate_ident(s) == s


@pytest.mark.parametrize("s", ["public;drop", 'a"b', "x'y", "", "a" * 64])
def test_ident_bad(s):
    with pytest.raises(ValueError):
        rk.validate_ident(s)


@pytest.mark.parametrize("s", ["IC", "ic-ro", "1ic"])
def test_role_bad(s):
    with pytest.raises(ValueError):
        rk.validate_role(s)


def test_role_ok():
    assert rk.validate_role("intelligence_corner")


@pytest.mark.parametrize("s", ["30s", "45000", "500ms", "2min"])
def test_timeout_ok(s):
    assert rk.validate_timeout(s) == s


@pytest.mark.parametrize("s", ["30 s", "0x10", ";"])
def test_timeout_bad(s):
    with pytest.raises(ValueError):
        rk.validate_timeout(s)


def test_render():
    out = rk.render("intelligence_corner", ["public"], ["public.api_tokens"], "30s", "pw-abc'def")
    lines = out.splitlines()
    assert lines[0] == "SET ic_ro.role = 'intelligence_corner';"
    assert lines[1] == "SET ic_ro.schemas = 'public';"
    assert lines[2] == "SET ic_ro.revoke = 'public.api_tokens';"
    assert lines[3] == "SET ic_ro.statement_timeout = '30s';"
    assert lines[4] == "SET ic_ro.password = 'pw-abc''def';"
    assert out.endswith(KIT.read_bytes().decode("utf-8"))


def test_render_no_password_and_bad_password():
    assert "SET ic_ro.password" not in "".join(rk.render("r", ["public"], [], "30s", "").splitlines()[:5])
    for bad in ("a\nb", "a\x00b"):
        with pytest.raises(ValueError):
            rk.render("r", ["public"], [], "30s", bad)


def test_render_rejects_injection():
    with pytest.raises(ValueError):
        rk.render("r", ["public;drop"], [], "30s", "")
    with pytest.raises(ValueError):
        rk.render("r", ["public"], ["public.t'x"], "30s", "")


def test_build_url():
    u = rk.build_url("postgresql://admin:secret@db.internal:5432/app", "intelligence_corner", "p@ss")
    assert u == "postgresql://intelligence_corner:p%40ss@db.internal:5432/app"
    u = rk.build_url("postgresql://admin:secret@db.internal:5432/app", "ic", "p", host="replica.internal:6432")
    assert u == "postgresql://ic:p@replica.internal:6432/app"
    assert "admin" not in u and "secret" not in u


def test_build_url_keeps_query():
    adm = ("postgresql://admin:secret@db.internal:5432/app?sslmode=require&sslrootcert=/etc/ssl/ca.pem"
           "&application_name=ic&options=-c search_path=a&connect_timeout=5&user=x&password=y")
    u = rk.build_url(adm, "ic", "p")
    assert u.startswith("postgresql://ic:p@db.internal:5432/app?")
    assert "sslmode=require" in u and "sslrootcert=%2Fetc%2Fssl%2Fca.pem" in u
    assert "options=-c%20search_path%3Da" in u and "+" not in u
    for gone in ("connect_timeout", "user=", "password=", "admin", "secret"):
        assert gone not in u
    q = "postgresql://admin:secret@db:5432/app?sslmode=require&host=h&port=1"
    assert "host=h" in rk.build_url(q, "ic", "p")
    r = rk.build_url(q, "ic", "p", host="replica:6432")
    assert "host=" not in r and "port=" not in r and "sslmode=require" in r
    assert r.startswith("postgresql://ic:p@replica:6432/app")


def test_kit_text():
    t = KIT.read_text(encoding="utf-8")
    for needle in ("DO $kit$", "default_transaction_read_only", "statement_timeout",
                   "REVOKE ALL ON TABLE", "COMMENT ON ROLE", "format('%I", "NOINHERIT",
                   "pg_rewrite", "pg_depend", "WITH RECURSIVE", "relkind IN ('v', 'm')", "revoked view",
                   "aclexplode", "REVOKE ALL ON SCHEMA", "out of scope"):
        assert needle in t
    code = "\n".join(ln for ln in t.splitlines() if not ln.lstrip().startswith("--"))
    assert "ALTER DEFAULT PRIVILEGES" not in code.upper()
    assert "TO PUBLIC" not in code.upper()


def test_cli_render(monkeypatch, capsys):
    monkeypatch.setenv("IC_RO_PASSWORD", "pw1")
    rc = rk.main(["render", "--role", "r1", "--schemas", "public", "--revoke", "public.t", "--timeout", "30s"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SET ic_ro.role = 'r1';" in out and "SET ic_ro.password = 'pw1';" in out


def test_cli_render_pack_merge(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.delenv("IC_RO_PASSWORD", raising=False)
    f = tmp_path / "pack.json"
    f.write_text(json.dumps(PACK))
    assert rk.main(["render", "--pack", str(f), "--schemas", "extra", "--revoke", "x.y"]) == 0
    out = capsys.readouterr().out
    assert "SET ic_ro.schemas = 'public,billing,extra';" in out
    assert "SET ic_ro.revoke = 'public.api_tokens,billing.cards,x.y';" in out


def test_cli_validation_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("IC_RO_PASSWORD", raising=False)
    assert rk.main(["render", "--role", "BAD"]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_url(monkeypatch, capsys):
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://admin:secret@h:5432/app")
    monkeypatch.setenv("IC_RO_PASSWORD", "pw")
    assert rk.main(["url", "--role", "r1", "--host", "rep:6432"]) == 0
    assert capsys.readouterr().out.strip() == "postgresql://r1:pw@rep:6432/app"
    monkeypatch.delenv("IC_RO_PASSWORD")
    assert rk.main(["url", "--role", "r1"]) == 2


def test_no_secret_flags():
    src = (ROOT / "backend" / "app" / "readonlykit.py").read_text(encoding="utf-8")
    assert 'add_argument("--password' not in src and 'add_argument("--admin' not in src


# ── check_role with a scripted fake connection ──────────────────────────

class _Tx:
    def __init__(self, conn):
        self.c = conn

    async def __aenter__(self):
        self.c.log.append("BEGIN")
        return self

    async def __aexit__(self, et, ev, tb):
        self.c.log.append("ROLLBACK")
        return False


class FakeConn:
    def __init__(self, **o):
        self.o = dict(super_=False, bypass=False, read_all=False, write_all=False, n_write=0,
                      create_schemas=[], create_ok=False, ro="on", st="30s",
                      comment="Intelligence Corner read-only role. revoke_select: public.api_tokens",
                      exists=True, tbl_priv=False, col_priv=False, select_ok=False, dep_views=[])
        self.o.update(o)
        self.log = []
        self.dep_args = []

    def transaction(self):
        return _Tx(self)

    async def fetchrow(self, q, *a):
        o = self.o
        if "rolsuper" in q:
            return {"rolsuper": o["super_"], "rolbypassrls": o["bypass"],
                    "rolcreaterole": False, "rolcreatedb": False}
        if "pg_read_all_data" in q:
            return {"r": o["read_all"], "w": o["write_all"]}
        if "has_table_privilege($1" in q:
            return {"t": o["tbl_priv"], "c": o["col_priv"]}
        raise AssertionError(q)

    async def fetchval(self, q, *a):
        o = self.o
        if "count(*)" in q:
            return o["n_write"]
        if "SHOW default_transaction_read_only" in q:
            return o["ro"]
        if "SHOW statement_timeout" in q:
            return o["st"]
        if "shobj_description" in q:
            return o["comment"]
        if "to_regclass" in q:
            return "x" if o["exists"] else None
        raise AssertionError(q)

    async def fetch(self, q, *a):
        if "pg_rewrite" in q:
            self.dep_args.append(a)
            return self.o["dep_views"]
        if "has_schema_privilege" in q:
            return [{"nspname": n} for n in self.o["create_schemas"]]
        return [{"name": "public.customers"}]

    async def execute(self, q, *a):
        self.log.append(q)
        if q.startswith("CREATE TABLE"):
            if not self.o["create_ok"]:
                raise asyncpg.InsufficientPrivilegeError("permission denied for schema public")
        if q.startswith("SELECT 1 FROM"):
            if not self.o["select_ok"]:
                raise asyncpg.InsufficientPrivilegeError("permission denied for table api_tokens")


async def test_check_role_good():
    c = FakeConn()
    ok, d = await rk.check_role(c)
    assert ok, d
    assert d.startswith("role is read-only")
    assert "revoked tables unreadable: public.api_tokens" in d and "sample tables:" in d
    i_rw = c.log.index("SET TRANSACTION READ WRITE")
    assert c.log.index("CREATE TABLE public._ic_write_probe (id int)") > i_rw


async def test_check_role_writable_count():
    ok, d = await rk.check_role(FakeConn(n_write=3))
    assert not ok and d.startswith("ROLE CAN WRITE") and "3" in d


async def test_check_role_create_probe_succeeds():
    ok, d = await rk.check_role(FakeConn(create_ok=True))
    assert not ok and "ROLE CAN WRITE" in d


async def test_check_role_create_schema():
    ok, d = await rk.check_role(FakeConn(create_schemas=["public"]))
    assert not ok and "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in d


@pytest.mark.parametrize("k", ["super_", "bypass", "read_all", "write_all"])
async def test_check_role_privileged(k):
    ok, _ = await rk.check_role(FakeConn(**{k: True}))
    assert not ok


async def test_check_role_revoked_still_readable():
    ok, d = await rk.check_role(FakeConn(tbl_priv=True))
    assert not ok and "still readable" in d and "(check grants to PUBLIC or role memberships)" in d
    ok, d = await rk.check_role(FakeConn(select_ok=True))
    assert not ok and "still readable" in d


async def test_check_role_revoked_missing_is_warning():
    ok, d = await rk.check_role(FakeConn(exists=False))
    assert ok and "warning" in d


async def test_check_role_warnings_only():
    ok, d = await rk.check_role(FakeConn(ro="off", st="0"))
    assert ok and "warning" in d
    ok, d = await rk.check_role(FakeConn(comment=None))
    assert ok and "no revoke list recorded on the role (run agentctl readonly-role)" in d


async def test_check_role_bad_ident_never_interpolated():
    c = FakeConn(comment='x. revoke_select: public.t";drop table x;--')
    ok, d = await rk.check_role(c)
    assert not ok
    assert not any("drop table" in s for s in c.log)


def _dv(readable, rel="public.token_report"):
    return {"rel": rel, "root": "public.api_tokens", "readable": readable}


async def test_check_role_readable_view():
    ok, d = await rk.check_role(FakeConn(dep_views=[_dv(True)]))
    assert not ok and d.startswith("ROLE IS NOT SAFE")
    assert "public.token_report reads revoked public.api_tokens and is still readable" in d
    assert "re-run agentctl readonly-role, which revokes views over revoked tables" in d


async def test_check_role_readable_view_and_write():
    ok, d = await rk.check_role(FakeConn(dep_views=[_dv(True)], n_write=2))
    assert not ok and d.startswith("ROLE CAN WRITE") and "public.token_report" in d


async def test_check_role_unreadable_views_listed():
    ok, d = await rk.check_role(FakeConn(dep_views=[_dv(False), _dv(False, "reporting.token_report2")]))
    assert ok, d
    assert "revoked tables unreadable: public.api_tokens" in d
    assert "views over revoked tables unreadable: public.token_report, reporting.token_report2" in d


async def test_check_role_no_views_unchanged():
    ok, d = await rk.check_role(FakeConn())
    assert ok and "views over revoked tables" not in d


async def test_check_role_dep_query_param_shape():
    c = FakeConn(comment="x. revoke_select: public.api_tokens, bad;name, public.gone")
    await rk.check_role(c)
    assert c.dep_args == [(['"public"."api_tokens"', '"public"."gone"'],)]
    c = FakeConn(exists=False)
    await rk.check_role(c)
    assert c.dep_args == []


async def test_check_role_no_comment_no_dep_query():
    c = FakeConn(comment=None)
    await rk.check_role(c)
    assert c.dep_args == []


async def test_business_db_delegates(monkeypatch):
    from app import configservice as cs
    c = FakeConn()
    closed = []

    async def fake_connect(url, timeout=0):
        return c

    async def fake_close():
        closed.append(1)

    c.close = fake_close
    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    class S:
        tenant_database_url = "postgresql://u:p@h/db"
        business_database_url = ""

    monkeypatch.setattr(cs, "get_settings", lambda: S())
    assert await cs._test_business_db() == await rk.check_role(FakeConn())
    assert closed
    S.tenant_database_url = ""
    assert await cs._test_business_db() == (False, "no business DB URL set")


# ── agentctl static checks ───────────────────────────────────────────────

def _ctl():
    return AGENTCTL.read_text(encoding="utf-8")


def _arm():
    t = _ctl()
    a = t.index("# Phase 11: GOLIVE-03 read-only role kit")
    return t[a:t.index("# end Phase 11 readonly-role")]


def test_agentctl_arm():
    t = _ctl()
    for needle in ("readonly-role)", "ADMIN_DATABASE_URL", "read -rsp", "ON_ERROR_STOP=1", "--data-binary @-",
                   "Phase 11: GOLIVE-03"):
        assert needle in t
    arm = _arm()
    for needle in ("-e PGPASSWORD", "-e PGHOST", "--rotate-password", "pgenv", "RO_PW_MODE"):
        assert needle in arm
    usage = [ln for ln in t.splitlines() if "usage: agentctl start" in ln]
    assert usage and "--rotate-password" in usage[0]


_VALREF = re.compile(r"\$\{?(ADMIN_DATABASE_URL|IC_RO_PASSWORD)")
_ALLOWED = (
    re.compile(r"^\s*(export\s+)?(ADMIN_DATABASE_URL|IC_RO_PASSWORD)="),
    re.compile(r"^\s*(if|elif)?\s*\[\[\s+-[nz]\s"),
    re.compile(r"^\s*read -rsp "),
)


def test_agentctl_no_secret_argv():
    # Phase 11 gap closure (WR-05): every line that uses the VALUE of the admin URL or the role
    # password must be an assignment/export, an emptiness test or a prompt. Never a command arg.
    for ln in _ctl().splitlines():
        if ln.lstrip().startswith("#"):
            continue
        assert 'psql "$' not in ln and "$PSQL \"$" not in ln and "psql postgres" not in ln, ln
        if _VALREF.search(ln):
            assert any(r.match(ln) for r in _ALLOWED), ln


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_agentctl_syntax():
    assert subprocess.run(["bash", "-n", str(AGENTCTL)], capture_output=True).returncode == 0


def test_check_role_membership_probe_guards_missing_predefined_roles():
    # CR-03: pg_read_all_data / pg_write_all_data do not exist before PG14.
    import inspect
    from app import readonlykit
    src = inspect.getsource(readonlykit.check_role)
    assert "FROM pg_roles WHERE rolname='pg_read_all_data'" in src
    assert "FROM pg_roles WHERE rolname='pg_write_all_data'" in src


# ── pgenv (Phase 11 gap closure, WR-05) ──

def test_pg_env_basic():
    assert rk.pg_env("postgresql://admin:ADMINPW_CANARY_123@db.internal:6432/appdb") == {
        "PGHOST": "db.internal", "PGPORT": "6432", "PGUSER": "admin",
        "PGPASSWORD": "ADMINPW_CANARY_123", "PGDATABASE": "appdb"}


def test_pg_env_percent_decoded():
    e = rk.pg_env("postgresql://a%40b:p%40ss%3Aw@h/d%20b")
    assert (e["PGUSER"], e["PGPASSWORD"], e["PGDATABASE"]) == ("a@b", "p@ss:w", "d b")


def test_pg_env_schemes():
    assert rk.pg_env("postgres://u@h/d")["PGHOST"] == "h"
    assert rk.pg_env("postgresql+asyncpg://u@h/d")["PGHOST"] == "h"
    with pytest.raises(ValueError):
        rk.pg_env("mysql://x@h/d")


def test_pg_env_missing_parts_omitted():
    assert rk.pg_env("postgresql://h/d") == {"PGHOST": "h", "PGDATABASE": "d"}


def test_pg_env_query_params():
    assert rk.pg_env("postgresql://u@h/d?sslmode=require")["PGSSLMODE"] == "require"
    assert rk.pg_env("postgresql://u@h/d?application_name=x&connect_timeout=5")["PGAPPNAME"] == "x"
    assert rk.pg_env("postgresql://u@h/d?host=other")["PGHOST"] == "other"
    with pytest.raises(ValueError, match="foo") as ei:
        rk.pg_env("postgresql://u:ADMINPW_CANARY_123@h/d?foo=1")
    assert "ADMINPW_CANARY_123" not in str(ei.value)


def test_pg_env_control_chars_and_port():
    with pytest.raises(ValueError):
        rk.pg_env("postgresql://u:a%0Ab@h/d")
    with pytest.raises(ValueError):
        rk.pg_env("postgresql://u:a%00b@h/d")
    with pytest.raises(ValueError, match="port") as ei:
        rk.pg_env("postgresql://u:ADMINPW_CANARY_123@h:notaport/d")
    assert "ADMINPW_CANARY_123" not in str(ei.value)


def test_pg_env_errors_hide_secret():
    for url in ("mysql://u:ADMINPW_CANARY_123@h/d", "postgresql://u:ADMINPW_CANARY_123@[bad/d"):
        with pytest.raises(ValueError) as ei:
            rk.pg_env(url)
        assert "ADMINPW_CANARY_123" not in str(ei.value)


def test_pg_env_keys_cover_output():
    e = rk.pg_env("postgresql://u:p@h:1/d?sslmode=require&sslrootcert=/x&options=-c%20a=b")
    assert set(e) <= set(rk.PG_ENV_KEYS)


def test_pgenv_cli(monkeypatch, capsys):
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://u:p%40w@h:5433/d?sslmode=require")
    assert rk.main(["pgenv"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PGDATABASE=d", "PGHOST=h", "PGPASSWORD=p@w", "PGPORT=5433", "PGSSLMODE=require", "PGUSER=u"]


def test_pgenv_cli_errors(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    assert rk.main(["pgenv"]) == 2
    assert "error:" in capsys.readouterr().err
    monkeypatch.setenv("ADMIN_DATABASE_URL", "mysql://u:SECRETX@h/d")
    assert rk.main(["pgenv"]) == 2
    c = capsys.readouterr()
    assert "SECRETX" not in c.err and "SECRETX" not in c.out


# ── agentctl readonly-role: argv-dump stub tests (Phase 11 gap closure, WR-05 / WR-04) ──

import os
import sys
from urllib.parse import urlsplit

ADMIN_PW = "ADMINPW_CANARY_123"
RO_PW = "ROPW_CANARY_456"
ADMIN_URL = f"postgresql://admin:{ADMIN_PW}@127.0.0.1:5999/nodb"

_STUB = r"""#!/usr/bin/env bash
D="$(dirname "$0")"
echo "$*" >> "$D/argv.log"
if [[ "${PGPASSWORD:-}" == "ADMINPW_CANARY_123" && "${PGHOST:-}" == "127.0.0.1" && "${PGPORT:-}" == "5999" ]]; then
  echo env-ok >> "$D/env.log"
fi
IN="$(cat)"
if [[ "$IN" == *"FROM pg_roles WHERE rolname"* && "$IN" != *'DO $kit$'* ]]; then
  printf '%s\n' "${STUB_ROLE_EXISTS:-}"
else
  printf '%s' "$IN" > "$D/kit.sql"
fi
"""
_DOCKER = _STUB.replace('echo "$*" >> "$D/argv.log"', 'echo "$*" >> "$D/argv.log"')
_UV = r"""#!/usr/bin/env bash
shift 3
exec "$STUB_PY" "$@"
"""

_BASH_OK = bool(shutil.which("bash") and shutil.which("openssl"))


@pytest.fixture
def stub(tmp_path):
    if not _BASH_OK:
        pytest.skip("needs bash and openssl")
    if " " in str(tmp_path):
        pytest.skip("tmp_path has a space")
    d = tmp_path
    for name, body in (("psqlstub", _STUB), ("uvshim", _UV)):
        (d / name).write_bytes(body.replace("\r\n", "\n").encode())
    (d / "bin").mkdir()
    (d / "bin" / "docker").write_bytes(_DOCKER.encode())
    for f in (d / "psqlstub", d / "uvshim", d / "bin" / "docker"):
        f.chmod(0o755)
    # the docker stub logs next to itself
    (d / "bin" / "docker").write_bytes(
        _DOCKER.replace('D="$(dirname "$0")"', f'D="{d.as_posix()}"').encode())

    def run(args, docker=False, **env):
        e = {k: v for k, v in os.environ.items()
             if k not in ("ADMIN_DATABASE_URL", "IC_RO_PASSWORD", "PSQL") and not k.startswith("PG")}
        e["UV_BIN"] = (d / "uvshim").as_posix()
        e["STUB_PY"] = Path(sys.executable).as_posix()
        e["STUB_ROLE_EXISTS"] = ""
        if docker:
            e["PATH"] = (d / "bin").as_posix() + os.pathsep + e["PATH"]
        else:
            e["PSQL"] = (d / "psqlstub").as_posix()
        e.update(env)
        for f in ("argv.log", "env.log", "kit.sql"):
            (d / f).unlink(missing_ok=True)
        return subprocess.run(["bash", AGENTCTL.as_posix(), "readonly-role", *args], env=e, input="",
                              capture_output=True, text=True, timeout=120)

    run.dir = d
    return run


def _log(stub, name):
    f = stub.dir / name
    return f.read_text() if f.exists() else ""


def test_stub_new_role_prints_url_no_argv_secrets(stub):
    r = stub([], ADMIN_DATABASE_URL=ADMIN_URL, IC_RO_PASSWORD=RO_PW)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith(f"postgresql://intelligence_corner:{RO_PW}@127.0.0.1:5999/nodb")
    argv = _log(stub, "argv.log")
    assert argv and ADMIN_PW not in argv and RO_PW not in argv and "postgresql://" not in argv
    assert "env-ok" in _log(stub, "env.log")
    assert f"SET ic_ro.password = '{RO_PW}';" in _log(stub, "kit.sql")


def test_stub_generated_password_for_absent_role(stub):
    r = stub([], ADMIN_DATABASE_URL=ADMIN_URL)
    assert r.returncode == 0, r.stderr
    pw = urlsplit(r.stdout.strip()).password
    assert re.fullmatch(r"[0-9a-f]{48}", pw)
    assert f"SET ic_ro.password = '{pw}';" in _log(stub, "kit.sql")
    assert "password changed" not in r.stderr


def test_stub_kept_password(stub):
    r = stub([], ADMIN_DATABASE_URL=ADMIN_URL, STUB_ROLE_EXISTS="1")
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert "password is unchanged" in r.stderr
    assert not re.search(r"(?m)^SET ic_ro.password", _log(stub, "kit.sql"))


def test_stub_rotate_password(stub):
    r = stub(["--rotate-password"], ADMIN_DATABASE_URL=ADMIN_URL, STUB_ROLE_EXISTS="1")
    assert r.returncode == 0, r.stderr
    pw = urlsplit(r.stdout.strip()).password
    assert re.fullmatch(r"[0-9a-f]{48}", pw)
    assert f"SET ic_ro.password = '{pw}';" in _log(stub, "kit.sql")
    assert pw not in _log(stub, "argv.log")
    assert "password changed" in r.stderr


def test_stub_bad_role_exits_before_psql(stub):
    r = stub(["--role", "x;drop"], ADMIN_DATABASE_URL=ADMIN_URL)
    assert r.returncode == 2
    assert not (stub.dir / "argv.log").exists()


def test_stub_bad_admin_url_never_echoed(stub):
    r = stub([], ADMIN_DATABASE_URL="mysql://u:BADURL_CANARY@h/d")
    assert r.returncode == 2
    assert "BADURL_CANARY" not in r.stdout + r.stderr
    assert not (stub.dir / "argv.log").exists()


def test_stub_print_sql_needs_no_admin_url(stub):
    r = stub(["--print-sql"])
    assert r.returncode == 0, r.stderr
    assert "DO $kit$" in r.stdout


@pytest.mark.skipif(shutil.which("psql") is not None, reason="a host psql exists; the docker fallback is not used")
def test_stub_docker_fallback_forwards_names_only(stub):
    r = stub([], docker=True, ADMIN_DATABASE_URL=ADMIN_URL, IC_RO_PASSWORD=RO_PW)
    assert r.returncode == 0, r.stderr
    argv = _log(stub, "argv.log")
    assert "-e PGPASSWORD" in argv and "-e PGHOST" in argv
    assert ADMIN_PW not in argv and RO_PW not in argv
    assert "env-ok" in _log(stub, "env.log")
