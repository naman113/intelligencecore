"""Quick 028: WR-03 (MySQL live cap, cheap probe), WR-04 (TLS never silently dropped), WR-11 (sync egress network)."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from app import configservice, modeflip, sourcealerts, sourcesvc, sourcevault, syncrunner, whsources, guardrails
from app.config import get_settings
from app.warehouse import DEFAULT_ROLES

from tests.test_syncrunner import FakeProc, fake  # noqa: F401  (fixture helpers)
from tests.test_whsources import FakeConn

APP = Path(__file__).resolve().parents[2]


# ── WR-04: the pure TLS rule ────────────────────────────────────────────────
@pytest.mark.parametrize("kind,opts,asks", [
    ("mysql", {"ssl": True}, True), ("mysql", {"ssl": False}, False), ("mysql", {}, False),
    ("mysql", None, False),
    ("mssql", {}, True), ("mssql", None, True), ("mssql", {"encrypt": "yes"}, True),
    ("mssql", {"encrypt": "strict"}, True), ("mssql", {"encrypt": "no"}, False),
    ("mssql", {"encrypt": "no", "trust_server_certificate": True}, False),
    ("postgres", {"sslmode": "require"}, False)])
def test_tls_requested(kind, opts, asks):
    assert whsources.tls_requested(kind, opts) is asks


def test_live_tls_refusal_text():
    assert whsources.live_tls_refusal("mysql", {"ssl": True}) == whsources.LIVE_TLS_REFUSAL["mysql"]
    assert whsources.live_tls_refusal("mssql", {}) == whsources.LIVE_TLS_REFUSAL["mssql"]
    assert whsources.live_tls_refusal("mysql", {}) is None
    assert whsources.live_tls_refusal("postgres", {"sslmode": "require"}) is None
    for t in whsources.LIVE_TLS_REFUSAL.values():
        assert "use import" in t and "encrypt" in t.lower() or "ssl" in t.lower()


def test_public_view_tls_flag(monkeypatch):
    from app import secretbox
    monkeypatch.setattr(secretbox, "decrypt_aad",
                        lambda aad, blob: json.dumps({"options": {"ssl": True}}))
    row = {"name": "m", "kind": "mysql", "display": {}, "secret": "x"}
    assert sourcevault.public_view(row)["tls"] is True
    row = dict(row, kind="mssql")
    monkeypatch.setattr(secretbox, "decrypt_aad", lambda aad, blob: json.dumps({"options": {"encrypt": "no"}}))
    assert sourcevault.public_view(row)["tls"] is False
    assert "password" not in json.dumps(sourcevault.public_view(row))

    def boom(aad, blob):
        raise ValueError("no key")
    monkeypatch.setattr(secretbox, "decrypt_aad", boom)
    assert sourcevault.public_view(row)["tls"] is None


# ── WR-04 / WR-03 in validate_proposal ──────────────────────────────────────
from tests.test_source_change import CRED, _pt, v  # noqa: E402


def _live(kind="mysql", est=50, **cred_kw):
    cred = {**CRED, "kind": kind, **cred_kw}
    probe = {"id": 10, "snapshot": {"tables": [_pt("lookup", est)]}}
    return v(config={"target": "cred:erp", "tables": ["lookup"]}, modes={"lookup": "live"},
             cred=cred, probe=probe, test=None)


def test_mysql_small_live_ok():
    assert _live(est=50)[1] is None


def test_mysql_large_live_refused():
    assert _live(est=10_000_000)[1] == whsources.MYSQL_LIVE_REFUSAL
    assert "mysql_live_max_rows" in whsources.MYSQL_LIVE_REFUSAL


def test_mysql_unknown_size_live_refused():
    assert _live(est=None)[1] == whsources.MYSQL_LIVE_REFUSAL


def test_mysql_cap_setting():
    s = get_settings()
    assert s.mysql_live_max_rows == 10_000
    assert configservice.REGISTRY["mysql_live_max_rows"]["group"] == "tuning"
    assert whsources.live_rows_allowed("mysql", s.mysql_live_max_rows)
    assert not whsources.live_rows_allowed("mysql", s.mysql_live_max_rows + 1)
    assert not whsources.live_rows_allowed("mysql", None)
    assert whsources.live_rows_allowed("mssql", 5) and not whsources.live_rows_allowed("mssql", None)
    assert whsources.live_rows_allowed("postgres", None)


def test_mysql_tls_live_refused():
    assert _live(tls=True)[1] == whsources.LIVE_TLS_REFUSAL["mysql"]
    assert _live(tls=False)[1] is None


def test_mssql_tls_live_refused():
    assert _live("mssql", tls=True)[1] == whsources.LIVE_TLS_REFUSAL["mssql"]
    assert _live("mssql", tls=False)[1] is None


def test_tls_does_not_block_import():
    cred = {**CRED, "tls": True}
    facts, err = v(modes={}, cred=cred)
    assert err is None


# ── mode flips ──────────────────────────────────────────────────────────────
from tests.test_mode_flip import cat_table, check, row, source  # noqa: E402


def test_flip_mysql_big_and_tls_refused():
    r = row(1, "orders", mode="import", est_rows=50)
    s = source([r], name="my", kind="mysql")
    cat = {"tables": [cat_table("orders", rows=50)]}
    facts, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=("mysql_fdw",))
    assert err is None
    cat = {"tables": [cat_table("orders", rows=10 ** 7)]}
    _, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=("mysql_fdw",))
    assert whsources.MYSQL_LIVE_REFUSAL in err
    cat = {"tables": [cat_table("orders", rows=50)]}
    _, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=("mysql_fdw",), tls=True)
    assert whsources.LIVE_TLS_REFUSAL["mysql"] in err
    s2 = source([r], name="ms", kind="mssql")
    _, err = check(s2, [{"table": "orders", "mode": "live"}], cat, has_ext=("tds_fdw",), tls=True)
    assert whsources.LIVE_TLS_REFUSAL["mssql"] in err


# ── provisioning is the authority: it has the full credential ───────────────
PROBE = {"tables": [{"schema": "ops", "table": "carriers", "est_rows": 8,
                     "columns": [{"name": "id", "type": "int(11)"}]}]}
TBL = [{"remote_schema": "ops", "remote_table": "carriers", "warehouse_table": "carriers", "mode": "live"}]
MY = {"kind": "mysql", "host": "h", "port": 3306, "database": "ops", "user": "u", "password": "p"}


async def test_provision_mysql_tls_refused_no_fdw_objects():
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mysql(conn, "my", dict(MY, options={"ssl": True}), TBL, PROBE, DEFAULT_ROLES)
    assert res["unlinked"] == ["carriers: " + whsources.LIVE_TLS_REFUSAL["mysql"]]
    assert res["created"] == 0
    assert not any("CREATE FOREIGN" in s or "CREATE VIEW" in s for s in conn.stmts)


async def test_provision_mysql_no_tls_still_works(monkeypatch):
    async def up(*a, **k):
        pass
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mysql(conn, "my", dict(MY, options={"ssl": False}), TBL, PROBE, DEFAULT_ROLES)
    assert res["created"] == 1 and not res["unlinked"]


async def test_provision_mysql_big_refused():
    probe = {"tables": [dict(PROBE["tables"][0], est_rows=10 ** 7)]}
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mysql(conn, "my", dict(MY, options={}), TBL, probe, DEFAULT_ROLES)
    assert res["unlinked"] == ["carriers: " + whsources.MYSQL_LIVE_REFUSAL] and res["created"] == 0


async def test_provision_mssql_default_encrypt_refused():
    ms = dict(MY, kind="mssql", port=1433, database="erp")
    probe = {"tables": [{"schema": "dbo", "table": "carriers", "est_rows": 8,
                         "columns": [{"name": "id", "type": "int"}]}]}
    t = [dict(TBL[0], remote_schema="dbo")]
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mssql(conn, "ms", ms, t, probe, DEFAULT_ROLES)
    assert res["unlinked"] == ["carriers: " + whsources.LIVE_TLS_REFUSAL["mssql"]]
    assert not any("CREATE FOREIGN" in s for s in conn.stmts)


async def test_remote_live_relation_refuses_tls():
    conn = FakeConn({"pg_extension": 1})
    with pytest.raises(ValueError, match="TLS"):
        await whsources._remote_live_relation(
            conn, "my", TBL[0], dict(MY, options={"ssl": True}), PROBE, DEFAULT_ROLES, "mysql")
    with pytest.raises(ValueError, match="mysql_live_max_rows"):
        probe = {"tables": [dict(PROBE["tables"][0], est_rows=10 ** 7)]}
        await whsources._remote_live_relation(conn, "my", TBL[0], dict(MY, options={}), probe,
                                              DEFAULT_ROLES, "mysql")


# ── WR-03: the probe ────────────────────────────────────────────────────────
def _src(tables):
    return {"id": 2, "name": "crm", "kind": "mysql", "status": "active", "tables": tables}


def _lv(name, est=None):
    return {"mode": "live", "status": "active", "warehouse_table": name, "est_rows": est}


async def test_probe_live_timeout_is_unknown(monkeypatch):
    async def run(sql):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    assert await sourcealerts.probe_live(_src([_lv("a", 5)])) is None


async def test_probe_live_wait_for_timeout_is_unknown(monkeypatch):
    async def run(sql):
        await asyncio.sleep(5)
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    monkeypatch.setattr(sourcealerts, "LIVE_PROBE_TIMEOUT", 0.05)
    assert await sourcealerts.probe_live(_src([_lv("a", 5)])) is None


async def test_probe_live_unreachable_still_true(monkeypatch):
    async def run(sql):
        raise guardrails.SourceUnreachable()
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    assert await sourcealerts.probe_live(_src([_lv("a", 5)])) is True


async def test_probe_live_picks_smallest_table(monkeypatch):
    seen = []

    async def run(sql):
        seen.append(sql)
        return [{"?column?": 1}]
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    s = _src([_lv("big", 9000), _lv("tiny", 3), _lv("unknown", None), _lv("mid", 400)])
    assert await sourcealerts.probe_live(s) is False
    assert seen == ['SELECT 1 FROM "src_crm"."tiny" LIMIT 1']
    seen.clear()
    await sourcealerts.probe_live(_src([_lv("zeta"), _lv("alpha")]))
    assert seen == ['SELECT 1 FROM "src_crm"."alpha" LIMIT 1']


# ── WR-11: the sync egress network ──────────────────────────────────────────
def _egress_script(fake, nets):
    """nets: {name: (internal, role)}"""
    def fn(a):
        if a[:2] == ["docker", "inspect"]:
            return FakeProc(out=json.dumps({n: {} for n in nets}).encode())
        if a[:3] == ["docker", "network", "inspect"]:
            internal, role = nets[a[-1]]
            if "Labels" in " ".join(a):
                return FakeProc(out=role.encode())
            return FakeProc(out=str(internal).lower().encode())
        return None
    fake.script.append(fn)


@pytest.mark.parametrize("cmd", ["check", "probe", "test", "sync"])
def test_network_for_db_picks_egress_not_default(fake, monkeypatch, cmd):
    monkeypatch.setattr(get_settings(), "sync_network", "")
    _egress_script(fake, {"proj_default": (False, ""), "proj_sync_egress": (False, "egress"),
                          "proj_sync_internal": (True, "")})
    assert asyncio.run(syncrunner.network_for(cmd)) == "proj_sync_egress"


def test_network_for_db_never_falls_back_to_default(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "")
    _egress_script(fake, {"proj_default": (False, ""), "proj_sync_internal": (True, "")})
    with pytest.raises(ValueError, match="sync egress network"):
        asyncio.run(syncrunner.network_for("sync"))


def test_network_for_db_internal_egress_label_ignored(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "")
    _egress_script(fake, {"proj_sync_egress": (True, "egress")})
    with pytest.raises(ValueError):
        asyncio.run(syncrunner.network_for("sync"))


def test_network_for_override_wins(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "ic_p25_syncegress")
    assert asyncio.run(syncrunner.network_for("sync")) == "ic_p25_syncegress"
    assert not fake.calls


def test_network_for_csv_unchanged(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_csv_network", "")
    _egress_script(fake, {"proj_default": (False, ""), "proj_sync_egress": (False, "egress"),
                          "proj_sync_internal": (True, "")})
    assert asyncio.run(syncrunner.network_for("csv")) == "proj_sync_internal"


def test_compose_sync_egress_network():
    d = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))
    egress = d["networks"]["sync_egress"]
    assert not egress.get("internal")
    assert egress["labels"]["ic.sync.role"] == "egress"
    members = [n for n, s in d["services"].items()
               if "sync_egress" in (s.get("networks") or [])]
    assert members == ["warehouse"]


def test_harness_stack_sync_egress():
    p = APP.parent / "deploy" / "stack.yml"
    if not p.exists():
        pytest.skip("harness checkout not next to the app")
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    net = d["networks"]["syncegress"]
    assert not net.get("internal") and net["labels"]["ic.sync.role"] == "egress"
    assert "SYNC_NETWORK" not in d["services"]["app"]["environment"]
    assert "syncegress" in d["services"]["warehouse"]["networks"]
    assert "syncegress" not in d["services"]["app"]["networks"]
