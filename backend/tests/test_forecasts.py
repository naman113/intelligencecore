"""Phase 13.1 unit tests: forecast definition validation, proposal and executor."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app import forecasts, insights

SQL = "SELECT d AS period, v AS value FROM t"


# -- validate_spec ---------------------------------------------------------

def test_valid_spec_defaults():
    spec, err = forecasts.validate_spec("Daily diesel", SQL, "day", 30, None, "flow")
    assert err is None
    assert spec["season"] == 7 and spec["kind"] == "flow"
    assert spec["name_key"] == insights.norm_title("Daily diesel")


def test_bad_grain_names_choices():
    _, err = forecasts.validate_spec("x", SQL, "hour", 3)
    assert "day" in err and "week" in err and "month" in err


@pytest.mark.parametrize("grain,h", [("day", 0), ("day", 91), ("week", 27), ("month", 13), ("day", True)])
def test_bad_horizon(grain, h):
    _, err = forecasts.validate_spec("x", SQL, grain, h)
    assert err and "horizon" in err


def test_season_rules():
    spec, err = forecasts.validate_spec("x", SQL, "week", 4, None)
    assert spec["season"] == 52
    spec, err = forecasts.validate_spec("x", SQL, "week", 4, 0)
    assert err is None and spec["season"] == 0
    _, err = forecasts.validate_spec("x", SQL, "week", 4, 7)
    assert err
    _, err = forecasts.validate_spec("x", SQL, "week", 4, None, "stock")
    assert err


@pytest.mark.parametrize("name", ["", "a" * 121, "bad​name", "bad\x01name", "!!!"])
def test_bad_names(name):
    assert forecasts.validate_spec(name, SQL, "day", 3)[1]


def test_bad_sql():
    assert forecasts.validate_spec("x", "SELECT 1; SELECT 2", "day", 3)[1].startswith("the series SQL was rejected")
    assert forecasts.validate_spec("x", "DELETE FROM t", "day", 3)[1]
    assert forecasts.validate_spec("x", "SELECT 1 -- " + "a" * 6000, "day", 3)[1]
    assert forecasts.validate_spec("x", "SELECT 1​", "day", 3)[1]


# -- check_series_rows -----------------------------------------------------

def test_rows_ok_and_sorted():
    rows = [{"period": "2026-01-12", "value": 2},
            {"period": date(2026, 1, 5), "value": Decimal("10.5")},
            {"period": date(2026, 1, 19), "value": None}]
    out = forecasts.check_series_rows(rows)
    assert out == [(date(2026, 1, 5), 10.5), (date(2026, 1, 12), 2.0), (date(2026, 1, 19), None)]


def test_timestamp_period_refused():
    with pytest.raises(ValueError, match="cast it"):
        forecasts.check_series_rows([{"period": datetime(2026, 1, 5, 3), "value": 1}])


@pytest.mark.parametrize("rows", [
    [{"period": date(2026, 1, 1), "value": 1, "extra": 2}],
    [{"period": date(2026, 1, 1)}],
    [{"period": "nope", "value": 1}],
    [{"period": date(2026, 1, 1), "value": "abc"}],
    [{"period": date(2026, 1, 1), "value": float("nan")}],
    [{"period": date(2026, 1, 1), "value": float("inf")}],
    [{"period": date(2026, 1, 1), "value": 1}, {"period": date(2026, 1, 1), "value": 2}],
])
def test_rows_refused(rows):
    with pytest.raises(ValueError):
        forecasts.check_series_rows(rows)


def test_rows_error_names_columns():
    with pytest.raises(ValueError, match="day, total"):
        forecasts.check_series_rows([{"day": 1, "total": 2}])


def test_period_start():
    assert forecasts.period_start(date(2026, 10, 25), "day") == date(2026, 10, 25)
    assert forecasts.period_start(date(2026, 10, 25), "week") == date(2026, 10, 19)
    assert forecasts.period_start(date(2026, 10, 25), "month") == date(2026, 10, 1)


def test_complete_periods():
    s = [(date(2026, 10, 5), 1.0), (date(2026, 10, 12), 1.0), (date(2026, 10, 19), 1.0)]
    r = forecasts.complete_periods(s, date(2026, 10, 25), "week")
    assert r == {"n": 2, "first": "2026-10-05", "last": "2026-10-12"}
    assert forecasts.complete_periods([], date(2026, 10, 25), "day")["n"] == 0


# -- propose / executor (faked pool and guardrails) -------------------------

import asyncpg  # noqa: E402

from app import actionsvc, guardrails, worker  # noqa: E402


class FakePool:
    def __init__(self, fetchval=None, fetchrow=None, raise_on_insert=None):
        self.calls = []
        self._fetchval = fetchval or {}
        self._fetchrow = fetchrow or {}
        self.raise_on_insert = raise_on_insert

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q, a))
        for k, v in self._fetchrow.items():
            if k in q:
                return v
        return None

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q, a))
        if q.lstrip().startswith("INSERT"):
            if self.raise_on_insert:
                raise self.raise_on_insert
            return self._fetchval.get("INSERT", 7)
        for k, v in self._fetchval.items():
            if k in q:
                return v
        return 0


def weekly_rows(n_complete, today=None):
    from datetime import timedelta
    today = today or forecasts.local_today()
    cur = forecasts.period_start(today, "week")
    return [{"period": cur - timedelta(weeks=i), "value": 10 + i} for i in range(n_complete + 1)]


@pytest.fixture
def env(monkeypatch):
    pool = FakePool()
    state = {"rows": weekly_rows(40)}

    async def get_pool():
        return pool

    async def fake_run(sql, limit=None):
        state["limit"] = limit
        state["sql"] = sql
        return state["rows"]

    monkeypatch.setattr(forecasts, "get_pool", get_pool)
    monkeypatch.setattr(guardrails, "run_readonly_sql", fake_run)
    return pool, state


async def test_propose_ok(env):
    pool, state = env
    r = await forecasts.propose("Weekly sales", SQL, "week", 8, run_id=3)
    assert r.startswith("proposed forecast (action #")
    assert "awaiting owner approval" in r and "Nothing runs until approved" in r
    low = r.lower()
    for w in ("draft", "watch", "alert", "created", "set up"):
        assert w not in low
    assert state["limit"] == forecasts.series_tail("week") == 293
    ins = [c for c in pool.calls if c[0] == "fetchval" and c[1].lstrip().startswith("INSERT")]
    import json
    payload = json.loads(ins[0][2][0])
    for k in ("_summary", "name", "name_key", "sql", "grain", "horizon", "season", "kind",
              "rationale", "history", "origin", "thread_id", "proposal_key"):
        assert k in payload
    assert payload["history"]["n"] == 40
    assert payload["_summary"].startswith("Forecast every night: Weekly sales, 8 weeks ahead")
    assert ins[0][2][1] == 3


async def test_reply_passes_guards(env):
    r = await forecasts.propose("Weekly sales", SQL, "week", 8)
    assert not worker._claims_draft(r)
    assert worker._watch_claim_sentences(r) == []


async def test_long_history_reads_only_the_newest_rows(env):
    # Quick 013 (WR-03): no row cap failure; the newest series_tail rows are read
    from datetime import timedelta
    _, state = env
    today = forecasts.local_today()
    state["rows"] = [{"period": today - timedelta(days=i), "value": 5}
                     for i in range(forecasts.series_tail("day"))]
    r = await forecasts.propose("Daily orders", SQL, "day", 14)
    assert r.startswith("proposed forecast (action #"), r
    assert state["limit"] == forecasts.series_tail("day") == 1197
    assert state["sql"].endswith("\n) AS _series ORDER BY _series.period DESC LIMIT 1197")
    assert state["sql"].startswith("SELECT * FROM (\n" + SQL)


@pytest.mark.parametrize("inner", [
    SQL,
    SQL + " -- trailing comment",
    SQL + ";",
    ("WITH s AS (SELECT date_trunc('week', o.d)::date AS period, sum(o.v) AS value FROM o "
     "GROUP BY 1) SELECT g::date AS period, coalesce(s.value, 0) AS value FROM "
     "generate_series(DATE '2024-11-04', DATE '2026-10-12', INTERVAL '7 days') AS g "
     "LEFT JOIN s ON s.period = g::date ORDER BY 1"),
])
async def test_wrapped_series_sql_passes_the_guardrail(env, inner):
    _, state = env
    await forecasts.dry_run(inner, grain="week")
    assert guardrails.validate_readonly_sql(state["sql"]) == state["sql"].strip()
    assert "-- trailing comment\n)" in state["sql"] or "--" not in state["sql"]


async def test_dry_run_rejects_bad_sql_before_running(env):
    _, state = env
    with pytest.raises(guardrails.SQLGuardrailError):
        await forecasts.dry_run("DELETE FROM t", grain="week")
    assert "sql" not in state


@pytest.mark.parametrize("grain,first,step,frag", [
    ("week", date(2026, 1, 6), 1, "start of each week (the Monday"),       # daily dates
    ("week", date(2026, 1, 7), 7, "start of each week"),                    # Wednesdays
    ("month", date(2026, 1, 15), 30, "start of each month (the first day"),
])
async def test_misaligned_periods_refused_at_propose(env, grain, first, step, frag):
    # Quick 013 (WR-04)
    from datetime import timedelta
    pool, state = env
    state["rows"] = [{"period": first + timedelta(days=step * i), "value": 1} for i in range(40)]
    r = await forecasts.propose("Misaligned", SQL, grain, 4)
    assert r.startswith("REFUSED: periods must be the start of each") and frag in r, r
    assert first.isoformat() in r and "::date" in r
    assert not any(c[1].lstrip().startswith("INSERT") for c in pool.calls)


def test_check_alignment_day_and_aligned():
    forecasts.check_alignment([(date(2026, 1, 7), 1.0)], "day")
    forecasts.check_alignment([(date(2026, 1, 5), 1.0), (date(2026, 1, 12), 2.0)], "week")
    forecasts.check_alignment([(date(2026, 2, 1), 1.0)], "month")
    with pytest.raises(ValueError, match="1 of 2 rows"):
        forecasts.check_alignment([(date(2026, 1, 5), 1.0), (date(2026, 1, 13), 2.0)], "week")


async def test_wrong_columns_and_query_errors_are_refusals(env, monkeypatch):
    async def undefined(sql, limit=None):
        raise asyncpg.UndefinedColumnError('column _series.period does not exist')
    monkeypatch.setattr(guardrails, "run_readonly_sql", undefined)
    r = await forecasts.propose("x", SQL, "week", 4)
    assert r == "REFUSED: the series SQL must return exactly the columns period and value"

    async def other(sql, limit=None):
        raise asyncpg.UndefinedTableError('relation "nope" does not exist')
    monkeypatch.setattr(guardrails, "run_readonly_sql", other)
    r = await forecasts.propose("x", SQL, "week", 4)
    assert r.startswith("REFUSED: the series SQL failed: relation")


async def test_not_enough_history(env):
    _, state = env
    state["rows"] = weekly_rows(20)
    r = await forecasts.propose("x", SQL, "week", 30 // 2 if False else 13)
    assert r.startswith("REFUSED: not enough history (20 periods; needs 26)")
    assert "at most 5" in r


@pytest.mark.parametrize("exc,frag", [
    (guardrails.SQLGuardrailError("nope"), "rejected"),
    (TimeoutError(), "too long"),
    (asyncpg.PostgresConnectionError("x"), "unavailable"),
    (ConnectionError("x"), "unavailable"),
])
async def test_dry_run_errors(env, monkeypatch, exc, frag):
    async def boom(sql, limit=None):
        raise exc
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    r = await forecasts.propose("x", SQL, "week", 4)
    assert r.startswith("REFUSED") and frag in r


@pytest.mark.parametrize("fetchrow,fetchval,run_id,frag", [
    ({"FROM forecast_defs WHERE name_key": {"id": 4}}, {}, None, "already exists"),
    ({"payload->>'name_key' = $1": {"id": 9}}, {}, None, "already proposed"),
    ({}, {"status IN ('active','paused')": 20}, None, "limit"),
    ({}, {"task_run_id": 2}, 5, "this run already proposed"),
])
async def test_conflicts(env, monkeypatch, fetchrow, fetchval, run_id, frag):
    pool, _ = env
    pool._fetchrow, pool._fetchval = fetchrow, fetchval
    r = await forecasts.propose("x", SQL, "week", 4, run_id=run_id)
    assert r.startswith("REFUSED") and frag in r


async def test_unique_violation(env):
    pool, _ = env
    pool.raise_on_insert = asyncpg.UniqueViolationError()
    r = await forecasts.propose("x", SQL, "week", 4)
    assert r.startswith("already proposed")


async def test_fixed_date_note(env):
    r = await forecasts.propose("x", "SELECT d AS period, v AS value FROM t WHERE d > DATE '2026-10-01'",
                                "week", 4)
    assert r.startswith("proposed forecast") and "no fixed date window" in r


def _payload(**kw):
    p = {"name": "Weekly sales", "sql": SQL, "grain": "week", "horizon": 8, "season": 52,
         "kind": "flow", "proposal_key": "abc", "thread_id": None}
    p.update(kw)
    return p


@pytest.mark.parametrize("bad", [
    None, "x", _payload(proposal_key=None), _payload(proposal_key="k" * 65),
    _payload(grain="hour"), _payload(sql="DELETE FROM t"), _payload(horizon=True),
])
async def test_create_rejects(env, bad):
    assert await forecasts.create_from_proposal(bad) is False


async def test_create_inserts(env):
    pool, _ = env
    pool._fetchval = {"INSERT": 11}
    assert await forecasts.create_from_proposal(_payload()) is True
    ins = [c for c in pool.calls if c[1].lstrip().startswith("INSERT INTO forecast_defs")]
    assert len(ins) == 1 and "ON CONFLICT (origin_ref)" in ins[0][1] and "now()" in ins[0][1]
    assert ins[0][2][8] == "abc" and ins[0][2][9] is None


async def test_create_conflict_retry(env):
    pool, _ = env
    pool._fetchval = {"INSERT": None, "SELECT 1 FROM forecast_defs WHERE origin_ref": 1}
    assert await forecasts.create_from_proposal(_payload()) is True


async def test_create_name_taken_and_cap(env):
    pool, _ = env
    pool.raise_on_insert = asyncpg.UniqueViolationError()
    assert await forecasts.create_from_proposal(_payload()) is False
    pool.raise_on_insert = None
    pool._fetchval = {"status IN ('active','paused')": 20}
    assert await forecasts.create_from_proposal(_payload()) is False


async def test_execute_delegates(monkeypatch):
    seen = {}

    async def fake(payload):
        seen["p"] = payload
        return True

    monkeypatch.setattr(forecasts, "create_from_proposal", fake)
    assert await actionsvc._execute("create_forecast", {"a": 1}) is True
    assert seen["p"] == {"a": 1}
