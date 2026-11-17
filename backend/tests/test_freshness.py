"""Phase 17 (CONS-02, CONS-03): pure freshness rules, hand-built facts, no database."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import freshness, guardrails
from app.freshness import FactCache, Freshness

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("UTC")


def _row(schema, table, *, mode="import", kind="postgres", schedule=None, last=None,
         backfill=True, pending=None, source="business_db"):
    return {"warehouse_schema": schema, "warehouse_table": table, "source_name": source,
            "source_kind": kind,
            "schedule": {"interval_minutes": 60, "window": None} if schedule is None else schedule,
            "mode": mode, "pending_mode": pending, "status": "active",
            "backfill_done": backfill, "last_success_at": last}


def _cache(rows=(), models=(), search_path=("public",)):
    c = FactCache(search_path=list(search_path), tz=TZ, loaded_at=NOW)
    for r in rows:
        c.add_table(r)
    for m in models:
        c.add_model(m)
    return c


def test_relations_of_drops_ctes():
    sql = ("WITH x AS (SELECT 1) SELECT * FROM x JOIN public.sales_orders s ON true "
           "JOIN customers c ON true")
    assert freshness.relations_of(sql) == {("public", "sales_orders"), ("", "customers")}


def test_relations_of_refuses_write():
    with pytest.raises(guardrails.SQLGuardrailError):
        freshness.relations_of("INSERT INTO t VALUES (1)")


def test_clock_kind():
    assert freshness.clock_kind("SELECT count(*) FROM t WHERE ts > now() - interval '1 day'") == "clock"
    assert freshness.clock_kind("SELECT 1 FROM t WHERE d >= CURRENT_DATE - 7") == "date"
    assert freshness.clock_kind("SELECT 1 FROM t") == "none"
    assert freshness.clock_kind("SELECT clock_timestamp()") == "clock"


def test_stale_after_empty_and_plain():
    t = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    assert freshness.stale_after({}, t, TZ, 60) is None
    got = freshness.stale_after({"interval_minutes": 60, "window": None}, t, TZ, 60)
    assert got == t + timedelta(minutes=120)


def test_stale_after_window_no_overnight_flag():
    t = datetime(2026, 10, 26, 21, 30, tzinfo=UTC)
    got = freshness.stale_after({"interval_minutes": 60, "window": [6, 22]}, t, TZ, 60)
    assert got == datetime(2026, 10, 27, 7, 0, tzinfo=UTC)


def test_import_stale_and_as_of():
    last = NOW - timedelta(hours=5)
    f = freshness.resolve({("public", "t")}, _cache([_row("public", "t", last=last)]), NOW, warehouse=True)
    assert f.stale and f.stale_tables == ("public.t",) and f.as_of == last
    assert f.import_reads == ("public.t",) and f.live_reads == ()


def test_import_fresh():
    last = NOW - timedelta(minutes=30)
    f = freshness.resolve({("public", "t")}, _cache([_row("public", "t", last=last)]), NOW, warehouse=True)
    assert not f.stale and f.as_of == last and f.inputs_synced_at == last


def test_backfill_or_never_synced_is_stale():
    c = _cache([_row("public", "a", last=NOW, backfill=False), _row("public", "b", last=None)])
    assert freshness.resolve({("public", "a")}, c, NOW, warehouse=True).stale
    assert freshness.resolve({("public", "b")}, c, NOW, warehouse=True).stale


def test_live_csv_unknown_never_stale():
    old = NOW - timedelta(days=30)
    c = _cache([_row("public", "l", mode="live"),
                _row("src_csv", "c", kind="csv", schedule={}, last=old)])
    f = freshness.resolve({("public", "l"), ("src_csv", "c"), ("zz", "nope")}, c, NOW, warehouse=True)
    assert not f.stale
    assert "public.l" in f.live_reads and "zz.nope" in f.live_reads


def test_live_as_of_is_now():
    f = freshness.resolve({("public", "l")}, _cache([_row("public", "l", mode="live")]), NOW, warehouse=True)
    assert f.as_of == NOW and f.inputs_synced_at is None


def test_two_imports_as_of_older_inputs_newer():
    a, b = NOW - timedelta(minutes=50), NOW - timedelta(minutes=10)
    c = _cache([_row("public", "a", last=a), _row("public", "b", last=b)])
    f = freshness.resolve({("public", "a"), ("public", "b")}, c, NOW, warehouse=True)
    assert f.as_of == a and f.inputs_synced_at == b


def test_unqualified_uses_search_path():
    last = NOW - timedelta(minutes=5)
    c = _cache([_row("public", "sales_orders", last=last)])
    f = freshness.resolve({("", "sales_orders")}, c, NOW, warehouse=True)
    assert f.reads == ("public.sales_orders",) and f.import_reads == ("public.sales_orders",)


def test_mixed_case_canonical_spelling():
    last = NOW - timedelta(hours=9)
    c = _cache([_row("public", "Order", last=last)], search_path=["Public"])
    for rel in (("public", "order"), ("", "order")):
        f = freshness.resolve({rel}, c, NOW, warehouse=True)
        assert f.reads == ("public.Order",)
        assert f.import_reads == ("public.Order",) and f.stale_tables == ("public.Order",)


def test_fold_collision_prefers_lowercase():
    last = NOW - timedelta(minutes=5)
    c = _cache([_row("public", "Order", last=last), _row("public", "order", last=last)])
    f = freshness.resolve({("public", "order")}, c, NOW, warehouse=True)
    assert f.reads == ("public.order",)
    c2 = _cache([_row("public", "order", last=last), _row("public", "Order", last=last)])
    assert freshness.resolve({("public", "order")}, c2, NOW, warehouse=True).reads == ("public.order",)


def test_pending_import_on_live_counts_live():
    c = _cache([_row("public", "t", mode="live", pending="import", last=None, backfill=False)])
    f = freshness.resolve({("public", "t")}, c, NOW, warehouse=True)
    assert f.live_reads == ("public.t",) and not f.stale


def test_view_model_recurses():
    last = NOW - timedelta(hours=6)
    c = _cache([_row("src_shop", "orders", last=last)],
               models=[{"relation": "model.v_orders", "object_kind": "view",
                        "derived_sql": "SELECT * FROM src_shop.orders", "updated_at": NOW}])
    f = freshness.resolve({("model", "v_orders")}, c, NOW, warehouse=True)
    assert f.stale and f.stale_tables == ("src_shop.orders",) and f.as_of == last


def test_matview_as_of_is_min():
    inp = NOW - timedelta(minutes=5)
    upd = NOW - timedelta(minutes=40)
    c = _cache([_row("src_shop", "orders", last=inp)],
               models=[{"relation": "model.m_orders", "object_kind": "matview",
                        "derived_sql": "SELECT * FROM src_shop.orders", "updated_at": upd}])
    f = freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True)
    assert f.as_of == upd and not f.stale


def test_model_cycle_is_safe():
    c = _cache(models=[
        {"relation": "model.a", "object_kind": "view", "derived_sql": "SELECT * FROM model.b", "updated_at": NOW},
        {"relation": "model.b", "object_kind": "view", "derived_sql": "SELECT * FROM model.a", "updated_at": NOW}])
    f = freshness.resolve({("model", "a")}, c, NOW, warehouse=True)
    assert not f.stale


def test_no_warehouse_all_live():
    f = freshness.resolve({("public", "t")}, _cache(), NOW, warehouse=False)
    assert not f.stale and not f.failed and f.as_of == NOW and f.live_reads == ("public.t",)


def test_summary_is_short():
    last = NOW - timedelta(hours=5)
    f = freshness.resolve({("public", "t")}, _cache([_row("public", "t", last=last)]), NOW, warehouse=True)
    assert "public.t" in f.summary() and len(f.summary()) <= 300
    assert f.as_of_iso.startswith("2026-10-26")


async def test_for_sql_failed_facts(monkeypatch):
    monkeypatch.setattr(freshness.warehouse, "warehouse_mode", lambda: True)

    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(freshness, "load_facts", boom)
    f = await freshness.for_sql("SELECT * FROM public.t", now=NOW)
    assert f.failed and not f.stale and f.as_of == NOW and f.live_reads == ("public.t",)


async def test_for_sql_unparseable(monkeypatch):
    monkeypatch.setattr(freshness.warehouse, "warehouse_mode", lambda: True)
    f = await freshness.for_sql("DELETE FROM t", now=NOW)
    assert f.failed and f.reads == () and not f.stale


async def test_for_sql_uses_cache(monkeypatch):
    monkeypatch.setattr(freshness.warehouse, "warehouse_mode", lambda: True)
    last = NOW - timedelta(hours=8)
    f = await freshness.for_sql("SELECT 1 FROM public.t", cache=_cache([_row("public", "t", last=last)]), now=NOW)
    assert f.stale and not f.failed


async def test_for_sql_no_warehouse(monkeypatch):
    monkeypatch.setattr(freshness.warehouse, "warehouse_mode", lambda: False)
    f = await freshness.for_sql("SELECT 1 FROM public.t", now=NOW)
    assert not f.failed and f.live_reads == ("public.t",)


# -- Quick 027 (audit #1): a matview older than its synced inputs is stale ----

def _mv(upd, sql="SELECT * FROM src_shop.orders", name="m_orders"):
    return {"relation": f"model.{name}", "object_kind": "matview", "derived_sql": sql, "updated_at": upd}


def test_q027_matview_stale_when_inputs_synced_after_refresh_beyond_threshold():
    inp = NOW - timedelta(minutes=5)
    upd = NOW - timedelta(hours=5)
    c = _cache([_row("src_shop", "orders", last=inp)], models=[_mv(upd)])
    f = freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True)
    assert f.stale and f.stale_tables == ("model.m_orders",)
    assert f.as_of == upd and f.last_sync["model.m_orders"] == upd
    assert "Stale data: model.m_orders" in f.summary()


def test_q027_matview_within_threshold_not_stale():
    c = _cache([_row("src_shop", "orders", last=NOW - timedelta(minutes=5))],
               models=[_mv(NOW - timedelta(minutes=70))])
    assert not freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True).stale


def test_q027_matview_refreshed_after_sync_recovers():
    inp = NOW - timedelta(minutes=30)
    c = _cache([_row("src_shop", "orders", last=inp)], models=[_mv(NOW - timedelta(minutes=1))])
    assert not freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True).stale


def test_q027_matview_inputs_not_synced_since_refresh_not_stale():
    # Inputs older than the refresh: nothing new to refresh from.
    c = _cache([_row("src_shop", "orders", last=NOW - timedelta(minutes=30))],
               models=[_mv(NOW - timedelta(minutes=10))])
    assert not freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True).stale


def test_q027_matview_over_live_or_csv_inputs_never_stale():
    c = _cache([_row("src_csv", "c", kind="csv", schedule={}, last=NOW - timedelta(minutes=1)),
                _row("public", "l", mode="live")],
               models=[_mv(NOW - timedelta(days=9), "SELECT * FROM src_csv.c", "m_csv"),
                       _mv(NOW - timedelta(days=9), "SELECT * FROM public.l", "m_live")])
    assert not freshness.resolve({("model", "m_csv"), ("model", "m_live")}, c, NOW, warehouse=True).stale


def test_q027_view_over_stale_matview_is_stale():
    c = _cache([_row("src_shop", "orders", last=NOW - timedelta(minutes=5))],
               models=[_mv(NOW - timedelta(hours=5)),
                       {"relation": "model.v_top", "object_kind": "view",
                        "derived_sql": "SELECT * FROM model.m_orders", "updated_at": NOW}])
    f = freshness.resolve({("model", "v_top")}, c, NOW, warehouse=True)
    assert f.stale and "model.m_orders" in f.stale_tables


def test_q027_matview_stale_sets_reads_unchanged():
    c = _cache([_row("src_shop", "orders", last=NOW - timedelta(minutes=5))],
               models=[_mv(NOW - timedelta(hours=5))])
    f = freshness.resolve({("model", "m_orders")}, c, NOW, warehouse=True)
    assert f.import_reads == ("src_shop.orders",)  # reads/import_reads stay real tables only
