"""get_kpi_history is the agents' door to the same kpihistory.series the ops
route serves. Parity is structural (the tool json.dumps the route's value) and
pinned here."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import kpihistory, playbooks
from app import main
from app.agents import graph


def _dt(day, hour=9):
    return datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)


class SeriesPool:
    """Routes queries by SQL substring (same shape as test_kpi_history.py)."""

    def __init__(self, watch=None, n=0, metrics=None, raw=None, buckets=None,
                 latest=None, watches=None, buckets_by_res=None):
        self.buckets_by_res = buckets_by_res
        self.resolutions = []
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
            self.resolutions.append(args[2])
            if self.buckets_by_res is not None:
                return self.buckets_by_res.get(args[2], [])
            return self.buckets
        if "FROM metric_history" in sql:
            return self.raw
        if "FROM watches ORDER BY id" in sql:
            return self.watches
        return []


def _watch():
    return {"id": 7, "title": "Orders", "kind": "kpi", "status": "active",
            "trigger_rule": '{"type": "change", "column": "orders"}'}


def _install(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    monkeypatch.setattr(kpihistory, "get_settings",
                        lambda: SimpleNamespace(timezone="UTC"))


async def test_tool_matches_route_raw(monkeypatch):
    rows = [{"t": _dt(1), "metric": "orders", "value": 1.0}, {"t": _dt(2), "metric": "orders", "value": 2.5},
            {"t": _dt(3), "metric": "orders", "value": None}]
    pool = SeriesPool(watch=_watch(), n=3, metrics=["orders"], raw=rows,
                      latest={"observed_at": _dt(3), "metric": "orders",
                              "value": None, "observation": '{"orders": null}'})
    _install(monkeypatch, pool)
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 30})
    assert json.loads(res) == await main.watch_history(7, 30)


async def test_tool_matches_route_bucketed(monkeypatch):
    buckets = [{"t": _dt(1), "metric": "orders", "value": 5.0, "min": 1.0, "max": 9.0, "n": 96}]
    pool = SeriesPool(watch=_watch(), n=2880, metrics=["orders"], buckets=buckets)
    _install(monkeypatch, pool)
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 30})
    assert json.loads(res) == await main.watch_history(7, 30)


async def test_tool_not_found_lists_watches(monkeypatch):
    pool = SeriesPool(watch=None, watches=[
        {"id": 3, "title": "South orders (30d)", "kind": "kpi", "status": "active"}])
    _install(monkeypatch, pool)
    res = await graph.get_kpi_history.ainvoke({"watch_id": 999})
    assert res.startswith("NOT FOUND: no watch #999")
    assert "South orders (30d)" in res


async def test_tool_error_is_a_string(monkeypatch):
    async def gp():
        raise RuntimeError("db down")
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    monkeypatch.setattr(kpihistory, "get_settings",
                        lambda: SimpleNamespace(timezone="UTC"))
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 30})
    assert res.startswith("TOOL ERROR: RuntimeError")


async def test_tool_clamps_days(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=0)
    _install(monkeypatch, pool)
    big = json.loads(await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 9999}))
    low = json.loads(await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 0}))
    assert big["days"] == 400 and low["days"] == 1


async def test_tool_output_under_budget(monkeypatch):
    # Worst realistic case: 7 days of hourly buckets (169) with 4-decimal values
    # and a latest observation just under the echo cap.
    base = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
    buckets = [{"t": base + timedelta(hours=i), "metric": "orders", "value": 12345.6789 + i,
                "min": 10000.1234, "max": 99999.9876, "n": 4}
               for i in range(169)]
    obs = json.dumps({"x": "a" * (kpihistory.OBSERVATION_MAX - 20)})
    pool = SeriesPool(watch=_watch(), n=676, metrics=["orders"], buckets=buckets,
                      latest={"observed_at": base, "metric": "orders",
                              "value": 1.0, "observation": obs})
    _install(monkeypatch, pool)
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 7})
    assert len(res) < 20000
    assert len(json.loads(res)["points"]) == 169


async def test_tool_output_under_budget_raw_200(monkeypatch):
    base = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    rows = [{"t": base + timedelta(hours=i), "metric": "orders", "value": 12345.6789 + i}
            for i in range(200)]
    pool = SeriesPool(watch=_watch(), n=200, metrics=["orders"], raw=rows,
                      latest={"observed_at": base, "metric": "orders",
                              "value": 1.0,
                              "observation": json.dumps({"x": "a" * 900})})
    _install(monkeypatch, pool)
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 30})
    assert len(res) < 20000 and len(json.loads(res)["points"]) == 200


async def test_tool_logs_call(monkeypatch):
    pool = SeriesPool(watch=_watch(), n=0)
    _install(monkeypatch, pool)
    seen = []

    async def rec(name, args, result, start, refused=False):
        seen.append((name, args))
    monkeypatch.setattr(graph, "_log_tool", rec)
    await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 5})
    assert len(seen) == 1 and seen[0][0] == "get_kpi_history"
    assert seen[0][1]["watch_id"] == 7 and seen[0][1]["days"] == 5


def test_tool_is_invocable_tool():
    assert graph.get_kpi_history.name == "get_kpi_history"
    assert graph.get_kpi_history.coroutine is not None


# ── registrations (05-04) ──

def test_registry_resolves_tool():
    assert graph._tool_registry()["get_kpi_history"] is graph.get_kpi_history


def test_superset_supervisor_and_data_only():
    s = graph.AGENT_TOOL_SUPERSET
    assert "get_kpi_history" in s["supervisor"] and "get_kpi_history" in s["data"]
    for k in ("analysis", "document", "drawing", "reflection"):
        assert "get_kpi_history" not in s[k]
    sup = s["supervisor"]
    assert sup.index("get_document") == sup.index("search_documents") + 1


def test_playbook_registry_grant_gated():
    reg = playbooks._playbook_registry()
    assert reg["get_kpi_history"] is graph.get_kpi_history
    assert graph.get_kpi_history not in [
        reg[t] for t in ["send_reminder", "notify_owner"] if t in reg]
    assert graph.get_kpi_history in [
        reg[t] for t in ["get_kpi_history"] if t in reg]


async def test_agents_page_lists_tool(monkeypatch):
    async def none():
        return {}
    monkeypatch.setattr(graph, "get_config_overrides", none)
    entries = {e["name"]: e for e in await graph.registry()}
    for name in ("Supervisor", "Data Agent"):
        assert "get_kpi_history" in [t["name"] for t in entries[name]["tools"]]


# ── realistic worst-case budget (05-06) ──

M19 = "avg_order_value_30d"
M40 = "average_order_value_per_customer_last_30"
assert len(M40) == 40
TITLE120 = ("Average order value per customer " * 4)[:120]
BASE_US = datetime(2026, 10, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)


def _v(i):
    return 123.45678901234567 + i / 7


def _raw_rows(metric, n):
    return [{"t": BASE_US + timedelta(minutes=15 * i, microseconds=7 * i),
             "metric": metric, "value": _v(i)} for i in range(n)]


def _buckets(metric, n, step, nn):
    return [{"t": BASE_US.replace(microsecond=0) + step * i, "metric": metric,
             "value": _v(i), "min": 100.12345678901234 + i / 3,
             "max": 199.98765432109876 + i / 9, "n": nn} for i in range(n)]


def _big_latest(metric):
    return {"observed_at": BASE_US + timedelta(days=2, microseconds=654321),
            "metric": metric, "value": _v(0),
            "observation": json.dumps({metric: _v(0), "note": "x" * 900})}


def _watch_m(metric):
    return {"id": 7, "title": TITLE120, "kind": "kpi", "status": "active",
            "trigger_rule": json.dumps({"type": "change", "column": metric})}


async def _call(days):
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": days})
    print("len", len(res))
    assert len(res) < 20000
    return json.loads(res)


async def test_budget_raw_200_realistic_stays_raw(monkeypatch):
    pool = SeriesPool(watch=_watch_m(M19), n=200, metrics=[M19],
                      raw=_raw_rows(M19, 200), latest=_big_latest(M19))
    _install(monkeypatch, pool)
    out = await _call(30)
    assert out["resolution"] == "raw" and len(out["points"]) == 200
    assert all("." not in p["t"] for p in out["points"])
    assert "." not in out["latest"]["t"]
    assert all(p["value"] == float(f"{p['value']:.6g}") for p in out["points"])
    assert pool.resolutions == []


async def test_budget_raw_200_long_metric_steps_up(monkeypatch):
    pool = SeriesPool(watch=_watch_m(M40), n=200, metrics=[M40],
                      raw=_raw_rows(M40, 200), latest=_big_latest(M40),
                      buckets_by_res={"hour": _buckets(M40, 50, timedelta(hours=1), 4)})
    _install(monkeypatch, pool)
    out = await _call(30)
    assert out["resolution"] == "hour" and pool.resolutions == ["hour"]
    assert len(out["points"]) == 50
    assert all("min" in p and "max" in p for p in out["points"])


def _hourly40_pool(metric):
    return SeriesPool(
        watch=_watch_m(metric), n=676, metrics=[metric], latest=_big_latest(metric),
        buckets_by_res={"hour": _buckets(metric, 169, timedelta(hours=1), 4),
                        "day": _buckets(metric, 8, timedelta(days=1), 84)})


async def test_budget_hourly_169_long_metric_steps_to_day(monkeypatch):
    pool = _hourly40_pool(M40)
    _install(monkeypatch, pool)
    out = await _call(7)
    assert out["resolution"] == "day" and pool.resolutions == ["hour", "day"]
    assert len(out["points"]) == 8
    assert all("min" in p and "max" in p for p in out["points"])
    assert out["latest"]["observation"] == {"_truncated": True}


async def test_budget_hourly_169_mid_metric_keeps_hour(monkeypatch):
    pool = _hourly40_pool(M19)
    _install(monkeypatch, pool)
    out = await _call(7)
    assert out["resolution"] == "hour" and len(out["points"]) == 169
    assert all("min" not in p and "max" not in p for p in out["points"])
    assert pool.resolutions == ["hour"]


async def test_budget_thins_as_last_resort(monkeypatch):
    pool = SeriesPool(watch=_watch_m(M40), n=60000, metrics=[M40],
                      latest=_big_latest(M40),
                      buckets_by_res={"week": _buckets(M40, 600, timedelta(weeks=1), 100)})
    _install(monkeypatch, pool)
    out = await _call(400)
    assert out["resolution"] == "week" and pool.resolutions == ["week"]
    assert len(out["points"]) == 150
    assert out["points"][-1]["t"] == (
        BASE_US.replace(microsecond=0) + timedelta(weeks=599)).isoformat()
    ts = [p["t"] for p in out["points"]]
    assert ts == sorted(set(ts))


async def test_budget_parity_after_step_up(monkeypatch):
    _install(monkeypatch, _hourly40_pool(M40))
    res = await graph.get_kpi_history.ainvoke({"watch_id": 7, "days": 7})
    assert json.loads(res) == await main.watch_history(7, 7)
