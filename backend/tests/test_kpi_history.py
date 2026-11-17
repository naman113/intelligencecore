"""Tests for the kpihistory seam (the one module that writes and reads metric_history).

Writer helpers are covered here; reader and route tests are appended by plan 05-03."""

import json
import logging
from datetime import date
from decimal import Decimal

import pytest

from app import kpihistory
from app.kpihistory import _num, pick_value, record


# ── writer (05-01) ──

@pytest.mark.parametrize("v", [None, True, False, "abc", float("nan"), "Infinity",
                               Decimal("NaN")])
def test_num_rejects(v):
    assert _num(v) is None


def test_num_accepts():
    assert _num(Decimal("12.5")) == 12.5
    assert _num("73") == 73.0
    assert _num(73) == 73.0


def test_pick_value_cases():
    assert pick_value({"column": "n"}, {"n": 73}) == ("n", 73.0)
    assert pick_value({"column": "n"}, {"n": None}) == ("n", None)
    assert pick_value({"column": "last_day"},
                      {"last_day": "2026-10-01", "orders": 12}) == ("last_day", None)
    assert pick_value({"column": "x"}, {"label": "a"}) == ("x", None)
    assert pick_value({}, {"a": "z", "b": 4}) == ("b", 4.0)
    assert pick_value(None, {"b": 4}) == ("b", 4.0)


class _Pool:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    async def execute(self, sql, *args):
        if self.raises:
            raise RuntimeError("boom")
        self.calls.append((sql, args))


async def test_record_inserts_one_row():
    pool = _Pool()
    ok = await record(pool, 7, {"type": "change", "column": "n"},
                      {"n": Decimal("73"), "d": date(2026, 10, 24)})
    assert ok is True
    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    assert "INSERT INTO metric_history" in sql and "$4::jsonb" in sql
    assert args[:3] == (7, "n", 73.0)
    assert json.loads(args[3]) == {"n": "73", "d": "2026-10-24"}


async def test_record_is_fail_soft(caplog):
    with caplog.at_level(logging.WARNING, logger="kpihistory"):
        ok = await record(_Pool(raises=True), 9, {"column": "n"}, {"n": 1})
    assert ok is False
    recs = [r for r in caplog.records if r.name == "kpihistory"]
    assert len(recs) == 1 and "9" in recs[0].getMessage()


def test_module_does_not_import_guardrails():
    src = open(kpihistory.__file__, encoding="utf-8").read()
    assert "guardrails" not in "\n".join(
        l for l in src.splitlines() if l.startswith(("from", "import")))


# ── reader (05-03) ──

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace


def _dt(day, hour=9):
    return datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)


class SeriesPool:
    """Routes queries by SQL substring; records every (sql, args)."""

    def __init__(self, watch=None, n=0, metrics=None, raw=None, buckets=None,
                 latest=None, watches=None):
        self.watch = watch
        self.stats = {"n": n, "metrics": metrics or []}
        self.raw = raw or []
        self.buckets = buckets or []
        self.latest = latest
        self.watches = watches or []
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM watches WHERE id" in sql:
            return self.watch
        if "ORDER BY observed_at DESC LIMIT 1" in sql:
            return self.latest
        if "count(*)" in sql:
            return self.stats
        return None

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "date_trunc" in sql:
            return self.buckets
        if "FROM metric_history" in sql:
            return self.raw
        if "FROM watches ORDER BY id" in sql:
            return self.watches
        return []


def _watch(rule='{"type": "change", "column": "orders"}'):
    return {"id": 7, "title": "Orders", "kind": "kpi", "status": "active",
            "trigger_rule": rule}


def _install(monkeypatch, pool, tz="UTC"):
    async def gp():
        return pool
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    monkeypatch.setattr(kpihistory, "get_settings",
                        lambda: SimpleNamespace(timezone=tz))


@pytest.mark.parametrize("inp,exp", [(None, 30), ("abc", 30), (0, 1), (-5, 1),
                                     (999, 400), ("7", 7), (30, 30)])
def test_clamp_days(inp, exp):
    assert kpihistory.clamp_days(inp) == exp


@pytest.mark.parametrize("n,d,exp", [(200, 30, "raw"), (201, 7, "hour"),
                                     (201, 8, "day"), (2880, 30, "day"),
                                     (201, 120, "day"), (201, 121, "week"),
                                     (201, 400, "week")])
def test_choose_resolution(n, d, exp):
    assert kpihistory.choose_resolution(n, d) == exp


@pytest.mark.parametrize("tz,exp", [("Asia/Kolkata", "Asia/Kolkata"),
                                    ("Bogus/Zone", "UTC"), ("", "UTC"),
                                    (None, "UTC")])
def test_tz(monkeypatch, tz, exp):
    monkeypatch.setattr(kpihistory, "get_settings",
                        lambda: SimpleNamespace(timezone=tz))
    assert kpihistory._tz() == exp


async def test_series_unknown_watch(monkeypatch):
    pool = SeriesPool(watch=None)
    _install(monkeypatch, pool)
    assert await kpihistory.series(999) is None
    assert not any("metric_history" in s for s, _ in pool.calls)


async def test_series_raw(monkeypatch):
    rows = [{"t": _dt(1), "metric": "orders", "value": 1.0}, {"t": _dt(2), "metric": "orders", "value": 2.0},
            {"t": _dt(3), "metric": "orders", "value": None}]
    pool = SeriesPool(watch=_watch(), n=3, metrics=["orders"], raw=rows,
                      latest={"observed_at": _dt(3), "metric": "orders",
                              "value": None, "observation": '{"orders": null}'})
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    json.dumps(out)
    assert out["resolution"] == "raw" and out["days"] == 30
    assert out["points"] == [{"t": r["t"].isoformat(), "metric": "orders", "value": r["value"]}
                             for r in rows]
    assert "T" in out["points"][0]["t"]
    assert out["latest"]["observation"] == {"orders": None}
    assert out["metric"] == "orders" and out["metrics"] == ["orders"]


async def test_series_bucketed(monkeypatch):
    buckets = [{"t": _dt(1), "metric": "orders", "value": 5.0, "min": 1.0, "max": 9.0, "n": 96}]
    pool = SeriesPool(watch=_watch(), n=2880, metrics=["orders"], buckets=buckets)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    json.dumps(out)
    assert out["resolution"] == "day"
    sql, args = [c for c in pool.calls if "date_trunc" in c[0]][0]
    assert args == (7, 30, "day", "UTC")
    p = out["points"][0]
    assert set(p) == {"t", "metric", "value", "min", "max", "n"} and isinstance(p["n"], int)
    assert out["latest"] is None
    assert out["metric"] == "orders"


async def test_series_clamps_days(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=0)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 0)
    assert out["days"] == 1
    assert any(args[:2] == (7, 1) for _, args in pool.calls if len(args) >= 2)
    assert (await kpihistory.series(7, 9999))["days"] == 400


async def test_series_metric_falls_back_to_trigger(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=0)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    assert out["latest"] is None and out["metric"] == "orders"
    assert out["points"] == [] and out["metrics"] == []


async def test_series_observation_truncated(monkeypatch):
    big = json.dumps({"x": "a" * 3000})
    pool = SeriesPool(watch=_watch(), n=1, raw=[],
                      latest={"observed_at": _dt(3), "metric": "orders",
                              "value": 1.0, "observation": big})
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    assert out["latest"]["observation"] == {"_truncated": True}


async def test_series_metrics_list(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=0, metrics=["a", "b"])
    _install(monkeypatch, pool)
    assert (await kpihistory.series(7, 30))["metrics"] == ["a", "b"]


async def test_series_bad_timezone(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=500,
                      buckets=[{"t": _dt(1), "metric": "orders", "value": 1.0, "min": 1.0,
                                "max": 1.0, "n": 1}])
    _install(monkeypatch, pool, tz="Bogus/Zone")
    out = await kpihistory.series(7, 30)
    assert out["timezone"] == "UTC"
    assert [c for c in pool.calls if "date_trunc" in c[0]][0][1][3] == "UTC"


async def test_list_watches_brief(monkeypatch):
    pool = SeriesPool(watches=[{"id": 1, "title": "t", "kind": "kpi",
                                "status": "active"}])
    _install(monkeypatch, pool)
    out = await kpihistory.list_watches_brief(limit=50)
    assert out == [{"id": 1, "title": "t", "kind": "kpi", "status": "active"}]
    assert pool.calls[-1][1] == (50,)


# ── route (05-03) ──

from fastapi import HTTPException

from app import main


async def test_route_returns_series(monkeypatch):
    def mk():
        return SeriesPool(watch=_watch(), n=1, metrics=["orders"],
                          raw=[{"t": _dt(1), "metric": "orders", "value": 3.0}])
    _install(monkeypatch, mk())
    expected = await kpihistory.series(7, 30)
    _install(monkeypatch, mk())
    assert await main.watch_history(7, 30) == expected


async def test_route_unknown_watch_404(monkeypatch):
    _install(monkeypatch, SeriesPool(watch=None))
    with pytest.raises(HTTPException) as e:
        await main.watch_history(5, 30)
    assert e.value.status_code == 404 and e.value.detail == "watch not found"


@pytest.mark.parametrize("days", [0, 401])
async def test_route_days_out_of_range_422(monkeypatch, days):
    called = []

    async def rec(*a, **k):
        called.append(a)
    monkeypatch.setattr(kpihistory, "series", rec)
    with pytest.raises(HTTPException) as e:
        await main.watch_history(7, days)
    assert e.value.status_code == 422 and not called


def test_route_default_days_is_plain_30():
    assert inspect.signature(main.watch_history).parameters["days"].default == 30


def test_route_registered_with_ops_key():
    routes = [r for r in main.app.routes
              if getattr(r, "path", "") == "/api/watches/{watch_id}/history"
              and "GET" in getattr(r, "methods", set())]
    assert routes
    assert main._require_ops_key in [d.dependency for d in routes[0].dependencies]


async def test_series_points_carry_metric_per_point(monkeypatch):
    """CR-01: a recompiled watch must not splice metrics into one unlabelled line."""
    rows = [{"t": _dt(1), "metric": "sum_amount", "value": 48000.0},
            {"t": _dt(2), "metric": "n_orders", "value": 73.0}]
    pool = SeriesPool(watch=_watch(), n=2, metrics=["sum_amount", "n_orders"], raw=rows)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    assert [p["metric"] for p in out["points"]] == ["sum_amount", "n_orders"]
    assert any("GROUP BY 1, 2" in s or "metric," in s for s, _ in pool.calls)


# ── rounding and timestamps (05-06) ──

@pytest.mark.parametrize("inp,exp", [
    (123.45678901234567, 123.457), (0.000123456789, 0.000123457),
    (-1234.56789, -1234.57), (48000.0, 48000.0), (2.5, 2.5), (0, 0.0),
    (12345678.9, 12345679.0), (None, None)])
def test_round_value(inp, exp):
    got = kpihistory._f(inp)
    if exp is None:
        assert got is None
    else:
        assert got == exp


def test_iso_drops_microseconds():
    dt = datetime(2026, 10, 1, 9, 0, 0, 123456, tzinfo=timezone.utc)
    assert kpihistory._iso(dt) == "2026-10-01T09:00:00+00:00"


async def test_series_trims_microseconds_and_rounds(monkeypatch):
    t1 = datetime(2026, 10, 1, 9, 0, 0, 111111, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 2, 9, 0, 0, 222222, tzinfo=timezone.utc)
    rows = [{"t": t1, "metric": "orders", "value": 123.45678901234567},
            {"t": t2, "metric": "orders", "value": 2.5}]
    pool = SeriesPool(watch=_watch(), n=2, metrics=["orders"], raw=rows,
                      latest={"observed_at": t2, "metric": "orders",
                              "value": 123.45678901234567, "observation": "{}"})
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    assert [p["t"] for p in out["points"]] == [
        r["t"].replace(microsecond=0).isoformat() for r in rows]
    assert [p["value"] for p in out["points"]] == [123.457, 2.5]
    assert "." not in out["latest"]["t"]
    assert out["latest"]["value"] == 123.457


async def test_series_under_budget_unchanged(monkeypatch):
    buckets = [{"t": _dt(1), "metric": "orders", "value": 5.0, "min": 1.0, "max": 9.0, "n": 96}]
    pool = SeriesPool(watch=_watch(), n=2880, metrics=["orders"], buckets=buckets)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    dt_calls = [c for c in pool.calls if "date_trunc" in c[0]]
    assert len(dt_calls) == 1 and dt_calls[0][1] == (7, 30, "day", "UTC")
    assert out["resolution"] == "day"
    assert "min" in out["points"][0] and "max" in out["points"][0]
    assert len(out) == 11


async def test_series_metric_prefers_value_column(monkeypatch):
    rule = '{"type": "threshold", "column": "stale", "op": ">", "value": 0, "value_column": "open"}'
    pool = SeriesPool(watch=_watch(rule=rule), n=0)
    _install(monkeypatch, pool)
    out = await kpihistory.series(7, 30)
    assert out["latest"] is None and out["metric"] == "open"
