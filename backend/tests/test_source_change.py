"""Phase 15 (SRC-04, SRC-07): proposal validation, the card payload and the source_change executor."""
import json
import math

import pytest

from app import actionsvc, sourcesvc, sourcetools as st
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _cols(*names):
    return [{"name": n, "type": "int"} for n in names]


def _pt(table, est=1000, pk=("id",), cands=(), cols=("id", "updated_at", "note")):
    return {"schema": "erp", "table": table, "est_rows": est, "est_mb": 2.0, "pk": list(pk),
            "columns": _cols(*cols), "cursor_candidates": list(cands), "sample": []}


PROBE = {"id": 10, "snapshot": {"tables": [
    _pt("stock_moves", 500_000, cands=[{"column": "updated_at", "indexed": True}]),
    _pt("suppliers", 5000, cands=[{"column": "updated_at", "indexed": False}]),
    _pt("shipments", 8000), _pt("carriers", 12), _pt("big", 900_000), _pt("nokey", 50, pk=()),
    _pt("Mixed", 5)]}}
TEST = {"id": 11, "snapshot": {"tables": [
    {"table": t, "status": "ok", "rows": 5} for t in
    ("stock_moves", "suppliers", "shipments", "big", "nokey", "Mixed", "carriers")]}}
CRED = {"handle": "cred:erp", "name": "erp", "kind": "mysql",
        "display": {"host": "db.example", "port": 3306, "database": "erp", "user": "ro"},
        "read_only": True, "verify_status": "ok"}
CFG = {"target": "cred:erp", "tables": [
    {"table": "stock_moves", "cursor": "updated_at"},
    {"table": "suppliers", "cursor": "updated_at", "reconcile": True},
    {"table": "shipments"}, {"table": "carriers"}]}
SCHED = {"interval_minutes": 60, "window": [22, 6]}


def v(name="erp_my", config=None, schedule=SCHED, modes=None, cred=CRED, probe=PROBE, test=TEST):
    return sourcesvc.validate_proposal(name, CFG if config is None else config, schedule,
                                       modes if modes is not None else {"carriers": "live"},
                                       cred, probe, test, get_settings())


def test_payload_contract():
    facts, err = v()
    assert err is None
    p = sourcesvc.build_payload(facts)
    t = {x["warehouse_table"]: x for x in p["tables"]}
    assert t["stock_moves"]["merge_key"] == ["id"] and t["stock_moves"]["cursor_indexed"] is True
    assert t["suppliers"]["reconcile"] is True and t["suppliers"]["cursor"] == "updated_at"
    assert t["shipments"]["cursor"] is None and t["carriers"]["mode"] == "live"
    assert p["estimated"]["rows"] == 500_000 + 5000 + 8000
    assert p["estimated"]["backfill_runs"] == math.ceil(513_000 / get_settings().sync_max_rows_per_run)
    assert p["credential"] == "cred:erp" and p["freshness_minutes"] == 180
    assert p["warehouse_schema"] == "src_erp_my"
    assert p["display"] == {"host": "db.example", "port": 3306, "database": "erp"}
    assert p["schema_policy"] == {"new_columns": "evolve", "type_change": "fail table"}
    assert "password" not in json.dumps(p)
    assert p["_summary"] == "Connect mysql source erp_my: 3 import, 1 live tables"


def test_unindexed_cursor_warning_text():
    facts, _ = v()
    assert ("cursor updated_at on suppliers is not indexed: each sync scans the table on the source; "
            "prefer an indexed column or an off-hours window") in facts["warnings"]


@pytest.mark.parametrize("kw,frag", [
    (dict(name="Bad"), "Source names"),
    (dict(cred=None), "unknown credential"),
    (dict(cred={**CRED, "verify_status": "failed"}), "not verified"),
    (dict(probe=None), "call probe_source first"),
    (dict(test=None), "no test copy"),
    (dict(test={"id": 9, "snapshot": TEST["snapshot"]}), "no test copy"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "zzz"}]}), "not in the latest probe"),
    (dict(config={"target": "cred:erp", "tables": ["carriers", "carriers"]}, modes={}), "listed twice"),
    (dict(modes={"carriers": "stream"}), "must be import or live"),
    (dict(modes={"carriers": "live"}, cred={**CRED, "read_only": False}),
     "live tables need a read-only login; this login can write"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "stock_moves", "cursor": "nope"}]}),
     "not a column"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "nokey", "cursor": "updated_at"}]}),
     "no primary key"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "big"}]}), "large tables need a cursor column"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "big", "cursor": "updated_at",
                                                    "reconcile": True}]}), "reconcile is only"),
    (dict(config={"target": "cred:erp", "tables": [{"table": "nokey", "reconcile": True}]}),
     "reconcile needs a primary key"),
    (dict(config={**CFG, "backfill_start": "last tuesday"}), "backfill_start"),
    (dict(schedule={"interval_minutes": 1}), "interval_minutes"),
    (dict(config={"target": "cred:erp", "tables": [{"table": f"t{i}"} for i in range(201)]}), "at most 200"),
])
def test_refusals(kw, frag):
    facts, err = v(**kw)
    assert facts is None and frag in err


def test_live_only_needs_no_test():
    facts, err = v(config={"target": "cred:erp", "tables": ["carriers"]}, modes={"carriers": "live"}, test=None)
    assert err is None and facts["test_id"] is None and facts["estimated"]["backfill_runs"] == 0


def _ms(est, read_only=True):
    cred = {**CRED, "kind": "mssql", "read_only": read_only}
    probe = {"id": 10, "snapshot": {"tables": [_pt("lookup", est)]}}
    return v(config={"target": "cred:erp", "tables": ["lookup"]}, modes={"lookup": "live"}, cred=cred,
             probe=probe, test=None)


def test_mssql_small_lookup_live_accepted():
    assert _ms(100)[1] is None


def test_mssql_large_table_refused():
    from app import whsources
    assert _ms(10_000_000)[1] == whsources.MSSQL_LIVE_REFUSAL


def test_mssql_missing_est_rows_refused():
    from app import whsources
    assert _ms(None)[1] == whsources.MSSQL_LIVE_REFUSAL


def test_mssql_writable_login_refused():
    assert "read-only login" in _ms(100, read_only=False)[1]


# -- tool + executor ---------------------------------------------------------
class Pool:
    def __init__(self):
        self.pending = None
        self.sql = []

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        if "INSERT INTO actions" in sql:
            return None if self.pending else 77
        if "SELECT id FROM actions" in sql:
            return self.pending
        return 1


@pytest.fixture
def tool_env(monkeypatch):
    pool = Pool()

    async def gp():
        return pool

    async def log_tool(*a, **k):
        pass

    async def handles():
        return [CRED]

    async def lp(t):
        return PROBE

    async def lt(n, t):
        return TEST

    async def nosrc(n):
        return None
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    monkeypatch.setattr(st, "get_pool", gp)
    monkeypatch.setattr(st, "_log_tool", log_tool)
    monkeypatch.setattr(st.sourcevault, "list_handles", handles)
    monkeypatch.setattr(sourcesvc, "latest_probe", lp)
    monkeypatch.setattr(sourcesvc, "latest_test", lt)
    monkeypatch.setattr(sourcesvc, "get_source", nosrc)
    return pool


async def test_propose_source_writes_card(tool_env):
    out = await st.propose_source.ainvoke({"name": "erp_my", "source_config": CFG, "schedule": SCHED,
                                           "modes": {"carriers": "live"}})
    assert out.startswith("Proposed source_change action #77")


async def test_propose_source_pending_refused(tool_env):
    tool_env.pending = 70
    out = await st.propose_source.ainvoke({"name": "erp_my", "source_config": CFG})
    assert out == "REFUSED: a source_change for erp_my is already waiting for approval (#70)"


async def test_propose_source_existing_name(tool_env, monkeypatch):
    async def gs(n):
        return {"id": 1}
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    assert "already exists" in await st.propose_source.ainvoke({"name": "erp_my", "source_config": CFG})


@pytest.fixture
def exec_env(monkeypatch):
    calls = {"prov": [], "enq": [], "sql": [], "pstatus": "ok"}

    class T:
        async def __aenter__(s):
            return None

        async def __aexit__(s, *a):
            return False

    class Conn:
        def transaction(self):
            return T()

        async def fetchrow(self, sql, *a):
            return None

        async def fetchval(self, sql, *a):
            calls["sql"].append(sql)
            return 5

        async def execute(self, sql, *a):
            calls["sql"].append(sql)

    class Acq:
        async def __aenter__(s):
            return Conn()

        async def __aexit__(s, *a):
            return False

    class P:
        def acquire(self):
            return Acq()

        async def fetchval(self, sql, *a):
            return 31

        async def fetchrow(self, sql, *a):
            return {"id": a[0], "snapshot": PROBE["snapshot"] if a[0] == 10 else TEST["snapshot"],
                    "kind": "probe" if a[0] == 10 else "test", "target": "cred:erp", "name": "erp_my"}

        async def fetch(self, sql, *a):
            return [{"id": TEST["id"], "snapshot": TEST["snapshot"]}]

        async def execute(self, sql, *a):
            calls["sql"].append(sql)

    async def gp():
        return P()

    async def handles():
        return [CRED]

    async def prov(name):
        calls["prov"].append(name)
        return {"status": calls["pstatus"]}

    async def enq(sid, kind):
        calls["enq"].append((sid, kind))
        return 1
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    from app import sourcevault, syncsched, whsources
    monkeypatch.setattr(sourcevault, "list_handles", handles)
    monkeypatch.setattr(whsources, "provision", prov)
    monkeypatch.setattr(syncsched, "enqueue_now", enq)
    return calls


def _payload():
    facts, _ = v()
    return sourcesvc.build_payload(facts)


async def test_executor_success(exec_env):
    assert await sourcesvc.apply_source_change(_payload()) is True
    assert exec_env["prov"] == ["erp_my"] and exec_env["enq"] == [(5, "backfill")]
    assert any("INSERT INTO sources" in s for s in exec_env["sql"])
    assert any("INSERT INTO context_sources" in s for s in exec_env["sql"])


async def test_executor_recomputes_estimates(exec_env):
    p = _payload()
    p["estimated"]["rows"] = 1
    p["tables"][0]["est_rows"] = 1
    assert await sourcesvc.apply_source_change(p) is True


async def test_executor_provision_failure_pauses(exec_env):
    exec_env["pstatus"] = "error"
    assert await sourcesvc.apply_source_change(_payload()) is False
    assert any("status='paused'" in s for s in exec_env["sql"]) and not exec_env["enq"]


async def test_executor_invalid_payload(exec_env):
    p = _payload()
    p["tables"][0]["cursor"] = "nope"
    assert await sourcesvc.apply_source_change(p) is False
    assert await sourcesvc.apply_source_change({"name": "x"}) is False
    assert not exec_env["prov"]


class _ApprovePool:
    def __init__(self):
        self.claimed = False

    async def fetchrow(self, sql, *a):
        if "SET status = 'approved'" in sql:
            if self.claimed:
                return None
            self.claimed = True
            return {"id": a[0], "kind": "source_change", "payload": json.dumps({"name": "x"})}
        if "SET status = 'rejected'" in sql:
            return {"id": a[0], "kind": "source_change", "payload": "{}"}

    async def execute(self, sql, *a):
        pass


async def test_approve_invalid_ends_failed_and_once(monkeypatch):
    pool, seen = _ApprovePool(), []

    async def gp():
        return pool

    async def apply(payload):
        seen.append(payload)
        return await real(payload)
    real = sourcesvc.apply_source_change

    async def fake_pool():
        return pool
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "get_pool", fake_pool)
    monkeypatch.setattr(sourcesvc, "apply_source_change", apply)
    assert (await actionsvc.approve(3))["status"] == "failed"
    assert "error" in await actionsvc.approve(3)
    assert len(seen) == 1


async def test_reject_leaves_no_source(monkeypatch):
    pool, seen = _ApprovePool(), []

    async def gp():
        return pool

    async def apply(payload):
        seen.append(1)
        return True
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "apply_source_change", apply)
    assert await actionsvc.reject(3) == {"rejected": 3} and not seen


def test_source_change_branch_present():
    import inspect
    src = inspect.getsource(actionsvc._execute)
    assert 'kind == "source_change"' in src and src.index("source_change") < src.index("no live executor")


async def test_merged_test_unions_several_test_calls():
    """CR-03: tests made in separate calls all count towards the proposal."""
    class P:
        async def fetch(self, sql, *a):
            return [{"id": 11, "snapshot": {"tables": [{"table": "a", "status": "ok"}]}},
                    {"id": 12, "snapshot": {"tables": [{"table": "b", "status": "ok"}]}}]
    m = await sourcesvc.merged_test(P(), "erp_my", "cred:erp", 10)
    assert m["id"] == 12 and {t["table"] for t in m["snapshot"]["tables"]} == {"a", "b"}

    class Empty:
        async def fetch(self, sql, *a):
            return []
    assert await sourcesvc.merged_test(Empty(), "erp_my", "cred:erp", 10) is None


# Phase 15 gap 2 (WR-14): remote vs warehouse names
def _mixed_probe():
    return {"id": 10, "snapshot": {"tables": [
        {**_pt("OrderLines", 100, pk=("OrderLineID",), cols=("OrderLineID", "UpdatedAt"),
               cands=[{"column": "UpdatedAt", "indexed": True}])},
        _pt("LookupCodes", 10), _pt("order_lines", 5), _pt("Order_Lines", 5), _pt("orders", 5)]}}


MIXED_TEST = {"id": 11, "snapshot": {"tables": [
    {"table": "order_lines", "remote_table": "OrderLines", "status": "ok"},
    {"table": "orders", "status": "ok"},
    {"table": "order_lines_2", "remote_table": "order_lines", "status": "ok"}]}}


def _vm(config, modes=None, probe=None, test=MIXED_TEST):
    return sourcesvc.validate_proposal("erp_my", config, SCHED, modes or {}, CRED,
                                       probe or _mixed_probe(), test, get_settings())


def test_mixed_case_import_and_live():
    cfg = {"target": "cred:erp", "tables": [{"table": "OrderLines", "cursor": "UpdatedAt"}, "LookupCodes"]}
    # OrderLines base order_lines collides with the exact remote order_lines -> _2
    facts, err = _vm(cfg, {"LookupCodes": "live"})
    assert err is None
    t = {x["remote_table"]: x for x in facts["tables"]}
    assert t["OrderLines"]["warehouse_table"] == "order_lines_2" and t["OrderLines"]["merge_key"] == ["OrderLineID"]
    assert t["LookupCodes"]["warehouse_table"] == "lookup_codes" and t["LookupCodes"]["mode"] == "live"


def test_modes_by_either_name():
    cfg = {"target": "cred:erp", "tables": ["LookupCodes"]}
    a, _ = _vm(cfg, {"LookupCodes": "live"}, test=None)
    b, _ = _vm(cfg, {"lookup_codes": "live"}, test=None)
    assert a["tables"] == b["tables"] and a["tables"][0]["mode"] == "live"


def test_legacy_test_row_without_remote_table():
    facts, err = _vm({"target": "cred:erp", "tables": ["orders"]})
    assert err is None and facts["tables"][0]["warehouse_table"] == "orders"


def test_collisions_get_distinct_names():
    cfg = {"target": "cred:erp", "tables": ["order_lines", "OrderLines", "Order_Lines"]}
    test = {"id": 11, "snapshot": {"tables": [
        {"table": n, "remote_table": r, "status": "ok"} for n, r in
        (("order_lines", "order_lines"), ("order_lines_2", "OrderLines"), ("order_lines_3", "Order_Lines"))]}}
    facts, err = _vm(cfg, test=test)
    assert err is None
    names = [t["warehouse_table"] for t in facts["tables"]]
    assert names == ["order_lines", "order_lines_2", "order_lines_3"] and len(set(names)) == 3


def test_long_name_refused():
    long = "L" * 70
    probe = {"id": 10, "snapshot": {"tables": [_pt(long)]}}
    facts, err = _vm({"target": "cred:erp", "tables": [long]}, {long: "live"}, probe=probe, test=None)
    assert facts is None and "63 bytes" in err


def test_revalidate_stability():
    cfg = {"target": "cred:erp", "tables": [{"table": "OrderLines", "cursor": "UpdatedAt"}, "LookupCodes"]}
    facts, err = _vm(cfg, {"LookupCodes": "live"})
    assert err is None
    p = sourcesvc.build_payload(facts)
    tabs = [{"table": t["remote_table"], "schema": t["remote_schema"], "cursor": t["cursor"],
             "merge_key": t["merge_key"] or None, "reconcile": t["reconcile"], "mode": t["mode"]}
            for t in p["tables"]]
    again, err = _vm({"target": "cred:erp", "tables": tabs, "freshness_minutes": p["freshness_minutes"]})
    assert err is None
    assert [t["warehouse_table"] for t in again["tables"]] == [t["warehouse_table"] for t in facts["tables"]]


def test_no_cannot_connect_text():
    import pathlib
    assert "it cannot be connected yet" not in pathlib.Path(sourcesvc.__file__).read_text(encoding="utf-8")
