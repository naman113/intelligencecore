"""Phase 13.1 plan 03: nightly forecast scheduling, pipeline and zero-LLM proofs."""
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import forecasts

UTC = timezone.utc


def _settings(monkeypatch, tz="Asia/Kolkata", hour=4):
    monkeypatch.setattr(forecasts, "get_settings",
                        lambda: types.SimpleNamespace(timezone=tz, forecast_hour=hour))


def test_next_slot_evening(monkeypatch):
    _settings(monkeypatch)
    r = forecasts.next_slot(datetime(2026, 10, 25, 15, 0, tzinfo=UTC))
    assert r == datetime(2026, 10, 26, 4, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert r.utcoffset().total_seconds() == 0


def test_next_slot_skips_close_slot(monkeypatch):
    _settings(monkeypatch)
    r = forecasts.next_slot(datetime(2026, 10, 25, 22, 0, tzinfo=UTC))
    assert r == datetime(2026, 10, 27, 4, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_next_slot_after_normal_run(monkeypatch):
    _settings(monkeypatch)
    run = datetime(2026, 10, 26, 4, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    r = forecasts.next_slot(run)
    assert r == datetime(2026, 10, 27, 4, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.parametrize("now", [datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
                                 datetime(2027, 3, 27, 12, 0, tzinfo=UTC)])
def test_next_slot_dst(monkeypatch, now):
    _settings(monkeypatch, tz="Europe/London")
    r = forecasts.next_slot(now).astimezone(ZoneInfo("Europe/London"))
    assert (r.hour, r.minute) == (4, 0)
    r2 = forecasts.next_slot(forecasts.next_slot(now)).astimezone(ZoneInfo("Europe/London"))
    assert (r2.hour, r2.minute) == (4, 0)


def test_bad_timezone_and_hour(monkeypatch):
    _settings(monkeypatch, tz="Nowhere/Land", hour=30)
    assert forecasts.forecast_hour() == 23
    r = forecasts.next_slot(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert r == datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    _settings(monkeypatch, tz="UTC", hour=-1)
    assert forecasts.forecast_hour() == 0


def test_in_night_window(monkeypatch):
    _settings(monkeypatch, tz="UTC", hour=4)
    assert forecasts.in_night_window(datetime(2026, 1, 1, 4, 0, tzinfo=UTC))
    assert forecasts.in_night_window(datetime(2026, 1, 1, 9, 59, tzinfo=UTC))
    assert not forecasts.in_night_window(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    assert not forecasts.in_night_window(datetime(2026, 1, 1, 3, 59, tzinfo=UTC))
    _settings(monkeypatch, tz="UTC", hour=22)
    assert forecasts.in_night_window(datetime(2026, 1, 2, 3, 0, tzinfo=UTC))
    assert not forecasts.in_night_window(datetime(2026, 1, 2, 4, 0, tzinfo=UTC))


def test_tail_len():
    assert forecasts.tail_len(1) == 24
    assert forecasts.tail_len(30) == 120
    assert forecasts.tail_len(90) == 310


def test_setting_registered():
    from app import configservice
    from app.config import Settings
    assert Settings.model_fields["forecast_hour"].default == 4
    assert configservice.REGISTRY["forecast_hour"]["group"] == "tuning"


# -- pipeline with a fake pool and a fake sandbox ---------------------------

import json  # noqa: E402
from datetime import date  # noqa: E402

import asyncpg  # noqa: E402

from app import guardrails, sandbox  # noqa: E402

DEF = {"id": 7, "name": "W", "sql": "select 1", "grain": "week", "horizon": 3,
       "season": 0, "kind": "flow"}
SERIES = [(date(2026, 1, 5), 1.0), (date(2026, 1, 12), None), (date(2026, 1, 19), 3.0)]


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log

    def transaction(self):
        return _Tx()

    async def executemany(self, sql, rows):
        self.log.append(("executemany", sql, list(rows)))

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))


class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, row=DEF):
        self.row = row
        self.log = []

    async def fetchrow(self, sql, *args):
        self.log.append(("fetchrow", sql, args))
        return self.row

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))

    def acquire(self):
        return _Acq(FakeConn(self.log))


def good_result(h=3, **kw):
    r = {"ok": True, "method": "ets", "baseline": "seasonal_naive", "mae": 1.0, "mape": 0.1,
         "wape": 0.1, "baseline_wape": 0.2, "coverage": 0.8, "n_periods": 3, "h": h,
         "origins": 2, "notes": ["n"],
         "points": [{"period": f"2026-02-{2 + 7 * i:02d}", "mean": 5.0, "low": 4.0, "high": 6.0}
                    for i in range(h)],
         "actuals": [{"period": "2026-01-05", "value": 1.0}, {"period": "2026-01-19", "value": 3.0}]}
    r.update(kw)
    return r


@pytest.fixture
def env(monkeypatch, tmp_path):
    pool = FakePool()

    async def gp():
        return pool

    async def dr(sql, grain=None):
        return SERIES

    monkeypatch.setattr(forecasts, "get_pool", gp)
    monkeypatch.setattr(forecasts, "dry_run", dr)
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    _settings(monkeypatch, tz="UTC", hour=4)
    box = types.SimpleNamespace(pool=pool, result=good_result(), ret="[OK]", seen={})

    async def fake_run(run_id, code):
        d = sandbox.scratch_dir(run_id)
        box.seen["code"] = code
        box.seen["cfg"] = json.loads((d / "config.json").read_text())
        box.seen["csv"] = (d / "series.csv").read_text()
        box.seen["old_gone"] = not (d / "result.json").exists()
        if box.result is not None:
            (d / "result.json").write_text(
                box.result if isinstance(box.result, str) else json.dumps(box.result))
        return box.ret

    monkeypatch.setattr(forecasts.sandbox, "run_python", fake_run)
    (tmp_path / "run_forecast_7").mkdir()
    (tmp_path / "run_forecast_7" / "result.json").write_text("stale")
    return box


def _fail_args(pool):
    ex = [e for e in pool.log if e[0] == "execute" and "failing_since" in e[1]]
    return ex[-1][2] if ex else None


async def test_nothing_due(env):
    env.pool.row = None
    assert await forecasts.run_one_due() is None


async def test_happy_path(env):
    assert await forecasts.run_one_due() == "ok"
    assert env.seen["old_gone"] and env.seen["cfg"]["max_history"] == 260
    assert env.seen["cfg"]["actual_tail"] == 24 and env.seen["cfg"]["horizon"] == 3
    assert env.seen["csv"].splitlines()[0] == "period,value"
    assert "2026-01-12," in env.seen["csv"]
    em = [e for e in env.pool.log if e[0] == "executemany"][0]
    assert len(em[2]) == 5  # 2 actuals + 3 forecasts
    assert len({r[1] for r in em[2]}) == 1
    sqls = [e[1] for e in env.pool.log if e[0] == "execute"]
    assert any("last_ok_at" in s for s in sqls)
    assert any("DELETE FROM forecast_points" in s for s in sqls)


def _pts(r, first):
    return {**r, "points": [first] + r["points"][1:]}


@pytest.mark.parametrize("mut", [
    lambda r: "not json",
    lambda r: "x" * 300_000,
    lambda r: {**r, "ok": "yes"},
    lambda r: {**r, "points": r["points"][:2]},
    lambda r: {**r, "method": "magic"},
    lambda r: {**r, "points": r["points"][::-1]},
    lambda r: {**r, "points": [{**p, "period": "2026-01-19"} for p in r["points"]]},
    lambda r: _pts(r, {**r["points"][0], "low": 9.0}),
    lambda r: _pts(r, {**r["points"][0], "mean": 7.0}),
    lambda r: {**r, "mae": float("inf")},
    lambda r: _pts(r, {**r["points"][0], "mean": float("nan")}),
])
async def test_unreadable_results(env, mut):
    env.result = mut(good_result())
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == "the forecasting script returned an unreadable result"
    assert not any(e[0] == "executemany" for e in env.pool.log)


async def test_missing_result_file(env):
    env.result = None
    assert await forecasts.run_one_due() == "failed"
    assert "unreadable" in _fail_args(env.pool)[1]


@pytest.mark.parametrize("res,msg", [
    ({"ok": False, "reason": "too_short", "message": "5 periods; needs 12"},
     "not enough history (5 periods; needs 12)"),
    ({"ok": False, "reason": "bad_input", "message": "x" * 400},
     "the forecasting script could not read the series: " + "x" * 200),
    ({"ok": False, "reason": "error", "message": "boom"}, "the forecasting script failed"),
])
async def test_script_failures(env, res, msg):
    env.result = res
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == msg


@pytest.mark.parametrize("ret,msg", [
    ("TIMEOUT: 120s", "the forecasting script timed out"),
    ("SANDBOX ERROR: docker", "the forecasting sandbox is unavailable"),
])
async def test_sandbox_failures(env, ret, msg):
    env.ret = ret
    env.result = None
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == msg


@pytest.mark.parametrize("exc,msg", [
    (guardrails.SQLGuardrailError("bad"),
     "the series query was rejected by the read-only guardrail: bad"),
    (TimeoutError(), "the series query timed out"),
    (ValueError("needs period and value"), "needs period and value"),
    (asyncpg.UndefinedTableError("no table"), "the series query failed: no table"),
])
async def test_export_errors(env, monkeypatch, exc, msg):
    async def dr(sql, grain=None):
        raise exc

    monkeypatch.setattr(forecasts, "dry_run", dr)
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == msg
    assert not env.seen  # sandbox never reached


async def test_unavailable_in_window_defers(env, monkeypatch):
    async def dr(sql, grain=None):
        raise ConnectionError("down")

    monkeypatch.setattr(forecasts, "dry_run", dr)
    monkeypatch.setattr(forecasts, "in_night_window", lambda now: True)
    assert await forecasts.run_one_due() == "deferred"
    assert _fail_args(env.pool) is None and not env.seen


async def test_unavailable_outside_window_fails(env, monkeypatch):
    async def dr(sql, grain=None):
        raise ConnectionError("down")

    monkeypatch.setattr(forecasts, "dry_run", dr)
    monkeypatch.setattr(forecasts, "in_night_window", lambda now: False)
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == "the business database was unavailable all night"


async def test_long_error_capped(env, monkeypatch):
    async def dr(sql, grain=None):
        raise ValueError("y" * 900)

    monkeypatch.setattr(forecasts, "dry_run", dr)
    await forecasts.run_one_due()
    assert len(_fail_args(env.pool)[1]) <= 200


async def test_zero_llm_run(env, monkeypatch):
    from app import llmclient

    def boom(*a, **k):
        raise AssertionError("LLM call on the nightly path")

    for n in ("chat_anthropic", "messages_kwargs", "sdk_kwargs"):
        monkeypatch.setattr(llmclient, n, boom, raising=False)
    assert await forecasts.run_one_due() == "ok"


# -- loop wiring and zero-LLM proofs (task 3) -------------------------------

import ast  # noqa: E402
import asyncio  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from app import dashboard_refresh, heartbeat  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
BANNED = ("app.llmclient", "app.agents", "langchain", "langgraph", "anthropic", "openai")


class _Stop(Exception):
    pass


async def test_run_forever_order_and_busy_window(monkeypatch):
    calls = []
    monkeypatch.setattr(heartbeat, "busy", lambda n, w: calls.append(("busy", w)))
    monkeypatch.setattr(heartbeat, "beat", lambda n, w: calls.append(("beat", w)))

    async def refresh():
        calls.append(("refresh", None))
        return False

    async def fc():
        calls.append(("forecast", None))

    async def stop(_):
        raise _Stop

    monkeypatch.setattr(dashboard_refresh, "refresh_due", refresh)
    monkeypatch.setattr(dashboard_refresh, "forecast_due", fc)
    monkeypatch.setattr(dashboard_refresh.asyncio, "sleep", stop)
    with pytest.raises(_Stop):
        await dashboard_refresh.run_forever()
    assert [c[0] for c in calls] == ["busy", "refresh", "forecast", "beat"]
    assert calls[0][1] == (dashboard_refresh.BATCH * dashboard_refresh.QUERY_TIMEOUT
                           + dashboard_refresh.FORECAST_STEP_TIMEOUT + 60)


async def test_forecast_due_skips_when_not_configured(monkeypatch):
    called = []

    async def run(info=None):
        called.append(1)

    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: False)
    monkeypatch.setattr(forecasts, "run_one_due", run)
    await dashboard_refresh.forecast_due()
    assert not called


@pytest.mark.parametrize("exc", [RuntimeError("x"), asyncpg.PostgresConnectionError("x"),
                                 TimeoutError(), ConnectionError("x")])
async def test_forecast_due_swallows(monkeypatch, exc):
    async def run(info=None):
        raise exc

    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    monkeypatch.setattr(forecasts, "run_one_due", run)
    await dashboard_refresh.forecast_due()


async def test_forecast_due_cancel_propagates(monkeypatch):
    async def run(info=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    monkeypatch.setattr(forecasts, "run_one_due", run)
    with pytest.raises(asyncio.CancelledError):
        await dashboard_refresh.forecast_due()


async def test_forecast_due_is_bounded(monkeypatch):
    async def run(info=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    monkeypatch.setattr(dashboard_refresh, "FORECAST_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(forecasts, "run_one_due", run)
    await dashboard_refresh.forecast_due()


def test_import_loads_no_llm_modules():
    code = ("import sys, app.forecasts, app.dashboard_refresh; "
            f"bad=[m for m in sys.modules if m.startswith({BANNED!r})]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _imports(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(("." * n.level) + (n.module or "") + "|" + ",".join(a.name for a in n.names))
    return out


def test_ast_no_llm_imports():
    bad = ("llmclient", "agents", "langchain", "langgraph", "anthropic", "openai")
    for rel in ("app/forecasts.py", "app/assets/forecast_runner.py"):
        tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
        for imp in _imports(tree):
            assert not any(b in imp for b in bad), (rel, imp)
    tree = ast.parse((BACKEND / "app/dashboard_refresh.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "forecast_due")
    assert _imports(fn) == [".|forecasts"]


# -- Quick 013: WR-01 (claimed runs always end recorded) and the Runs record ----

from datetime import timedelta  # noqa: E402

from app import jobruns  # noqa: E402


def _next_at(pool):
    ex = [e for e in pool.log if e[0] == "execute" and "failing_since" in e[1]]
    return ex[-1][2][2] if ex else None


@pytest.mark.parametrize("where", ["inputs", "runner_text", "sandbox", "store"])
async def test_exception_after_claim_records_failure_and_releases_lease(env, monkeypatch, where):
    boom = PermissionError("artifacts volume is read-only")
    if where == "inputs":
        monkeypatch.setattr(forecasts, "_write_inputs", lambda *a: (_ for _ in ()).throw(boom))
    elif where == "runner_text":
        monkeypatch.setattr(forecasts, "RUNNER_PATH", forecasts.RUNNER_PATH.with_name("nope.py"))
        boom = FileNotFoundError()
    elif where == "sandbox":
        async def bad_run(run_id, code):
            raise RuntimeError("docker exploded")
        monkeypatch.setattr(forecasts.sandbox, "run_python", bad_run)
        boom = RuntimeError()
    else:
        async def bad_store(*a, **k):
            raise RuntimeError("write failed")
        monkeypatch.setattr(forecasts, "_store_ok", bad_store)
        boom = RuntimeError()
    info = {}
    assert await forecasts.run_one_due(info=info) == "failed"
    msg = _fail_args(env.pool)[1]
    assert msg == f"the forecast run hit an internal error: {type(boom).__name__}"
    # lease released to the next nightly slot (always > 2 h away), never the 30-minute retry
    assert _next_at(env.pool) > datetime.now(UTC) + timedelta(hours=forecasts.MIN_GAP_HOURS)
    assert info["id"] == 7 and info["status"] == "failed" and info["error"] == msg
    assert not any(e[0] == "executemany" for e in env.pool.log)


async def test_info_on_success_and_deferral(env, monkeypatch):
    info = {}
    assert await forecasts.run_one_due(info=info) == "ok"
    assert info == {"id": 7, "name": "W", "grain": "week", "horizon": 3,
                    "nightly": info["nightly"], "status": "ok", "method": "ets", "wape": 0.1}

    async def dr(sql, grain=None):
        raise ConnectionError("down")
    monkeypatch.setattr(forecasts, "dry_run", dr)
    monkeypatch.setattr(forecasts, "in_night_window", lambda now: True)
    info = {}
    assert await forecasts.run_one_due(info=info) == "deferred"
    assert info["status"] == "deferred" and info["nightly"] is True


async def test_dry_run_gets_the_grain(env, monkeypatch):
    seen = {}

    async def dr(sql, grain=None):
        seen["grain"] = grain
        return SERIES
    monkeypatch.setattr(forecasts, "dry_run", dr)
    await forecasts.run_one_due()
    assert seen["grain"] == "week"


async def test_stale_result_is_a_failure(env):
    env.result = {"ok": False, "reason": "stale", "message": "no rows after 2026-01-05"}
    assert await forecasts.run_one_due() == "failed"
    assert _fail_args(env.pool)[1] == "the series looks stale: no rows after 2026-01-05"


async def test_step_timeout_never_cancels_the_sandbox(env, monkeypatch):
    done = {}

    async def slow_run(run_id, code):
        await asyncio.sleep(0.3)
        done["finished"] = True
        return "[OK]"
    monkeypatch.setattr(forecasts.sandbox, "run_python", slow_run)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(forecasts.run_one_due(), 0.05)
    await asyncio.sleep(0.5)
    assert done == {"finished": True}  # the shielded docker run finished on its own
    assert not forecasts._SANDBOX_TASKS


@pytest.fixture
def runs(monkeypatch):
    rec = []

    async def record(kind, name="", **kw):
        rec.append((kind, name, kw))
        return 1
    monkeypatch.setattr(jobruns, "record", record)
    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    return rec


def _fake_run(info_out, status=None, raise_exc=None, sleep=0):
    async def run(info=None):
        info.update(info_out)
        if sleep:
            await asyncio.sleep(sleep)
        if raise_exc:
            raise raise_exc
        if status:
            info["status"] = status
        return status
    return run


async def test_runs_record_ok(monkeypatch, runs):
    out = {"id": 7, "name": "Weekly revenue", "grain": "week", "horizon": 8, "nightly": True,
           "method": "ets", "wape": 11.64}
    monkeypatch.setattr(forecasts, "run_one_due", _fake_run(out, "ok"))
    await dashboard_refresh.forecast_due()
    assert len(runs) == 1
    kind, name, kw = runs[0]
    assert (kind, name) == ("forecast", "Weekly revenue")
    assert kw["ref"] == ("forecast", "7") and kw["trigger"] == "nightly"
    assert kw["routine"] is True and kw["collapse"] == "replace" and "status" not in kw
    assert kw["counts"] == {"definitions": 1, "ok": 1, "failed": 0, "deferred": 0,
                            "method": "ets", "wape": 11.6}
    assert kw["summary"] == ("Forecast refreshed: 8 weeks ahead, method ets, "
                             "typical miss 12% (WAPE)")


async def test_runs_record_failure_names_the_definition(monkeypatch, runs):
    out = {"id": 3, "name": "Daily orders", "grain": "day", "horizon": 14, "nightly": False,
           "error": "not enough history (5 periods; needs 28)"}
    monkeypatch.setattr(forecasts, "run_one_due", _fake_run(out, "failed"))
    await dashboard_refresh.forecast_due()
    kind, name, kw = runs[0]
    assert kw["status"] == "failed" and kw["collapse"] == "replace"
    assert kw["error"] == ("forecast 'Daily orders' (#3): not enough history "
                           "(5 periods; needs 28)")
    assert kw["counts"] == {"definitions": 1, "ok": 0, "failed": 1, "deferred": 0}
    assert kw["trigger"] == "run now or new forecast"


async def test_runs_record_deferral_is_routine(monkeypatch, runs):
    monkeypatch.setattr(forecasts, "run_one_due",
                        _fake_run({"id": 3, "name": "D", "nightly": True}, "deferred"))
    await dashboard_refresh.forecast_due()
    kind, name, kw = runs[0]
    assert kw["routine"] is True and kw["counts"]["deferred"] == 1 and "status" not in kw


async def test_runs_record_nothing_due(monkeypatch, runs):
    monkeypatch.setattr(forecasts, "run_one_due", _fake_run({}, None))
    await dashboard_refresh.forecast_due()
    assert runs == []


async def test_step_timeout_marks_the_claimed_definition_failed(monkeypatch, runs):
    marked = []

    async def fail_claimed(def_id, message):
        marked.append((def_id, message))
    monkeypatch.setattr(forecasts, "fail_claimed", fail_claimed)
    monkeypatch.setattr(dashboard_refresh, "FORECAST_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(forecasts, "run_one_due",
                        _fake_run({"id": 9, "name": "Slow"}, "ok", sleep=5))
    await dashboard_refresh.forecast_due()
    assert marked == [(9, "the forecast run took too long and was stopped")]
    assert runs[0][2]["status"] == "failed"
    assert "'Slow' (#9): the forecast run took too long" in runs[0][2]["error"]


async def test_step_timeout_is_not_logged_as_a_blip(monkeypatch, runs, caplog):
    monkeypatch.setattr(dashboard_refresh, "FORECAST_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(forecasts, "run_one_due", _fake_run({}, None, sleep=5))
    with caplog.at_level("WARNING"):
        await dashboard_refresh.forecast_due()
    text = caplog.text
    assert "timed out" in text and "blip" not in text


async def test_crash_after_claim_is_still_recorded(monkeypatch, runs):
    monkeypatch.setattr(forecasts, "run_one_due",
                        _fake_run({"id": 4, "name": "X"}, raise_exc=RuntimeError("db")))
    await dashboard_refresh.forecast_due()
    assert runs[0][2]["status"] == "failed"
    assert "stopped before the result was recorded" in runs[0][2]["error"]


async def test_forecast_step_is_listed_while_running(monkeypatch, runs):
    seen = {}

    async def run(info=None):
        seen["active"] = [j["kind"] for j in jobruns.active()]
    monkeypatch.setattr(forecasts, "run_one_due", run)
    await dashboard_refresh.forecast_due()
    assert "forecast" in seen["active"]
    assert "forecast" not in [j["kind"] for j in jobruns.active()]
