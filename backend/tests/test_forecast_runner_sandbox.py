"""Phase 13.1 (FCST-02): contract tests of forecast_runner.py in the real sandbox image.

Gated: FORECAST_SANDBOX_TEST=1 and docker on PATH. SANDBOX_IMAGE picks the image
(e.g. agentic-sandbox:ic-d7c3ca2517f9). Series are deterministic (no random module).
"""
import json
import math
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import sandbox

pytestmark = pytest.mark.skipif(
    os.getenv("FORECAST_SANDBOX_TEST") != "1" or not shutil.which("docker"),
    reason="set FORECAST_SANDBOX_TEST=1 with docker to run the sandbox contract tests",
)

RUNNER_PATH = Path(__file__).resolve().parents[1] / "app" / "assets" / "forecast_runner.py"
_counter = {"n": 0}


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    root = tmp_path
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", root)
    monkeypatch.setattr(sandbox, "ARTIFACTS_VOLUME", "")
    monkeypatch.setattr(sandbox, "SANDBOX_IMAGE", os.getenv("SANDBOX_IMAGE", "agentic-sandbox:latest"))


def _wobble(i, salt=7):
    return (((i + salt) * 2654435761) % 1000) / 1000.0 - 0.5


def _series(start, step_days, vals):
    out, d = [], start
    for v in vals:
        out.append((d, v))
        d = d + timedelta(days=step_days)
    return out


def _csv(rows):
    lines = ["period,value"]
    for d, v in rows:
        lines.append("%s,%s" % (d.isoformat(), "" if v is None else repr(float(v))))
    return "\n".join(lines) + "\n"


async def _run(rows, cfg, name=None):
    _counter["n"] += 1
    run_id = name or "forecast_t%d" % _counter["n"]
    wd = sandbox.scratch_dir(run_id)
    (wd / "series.csv").write_text(_csv(rows), encoding="utf-8")
    (wd / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    out = await sandbox.run_python(run_id, RUNNER_PATH.read_text(encoding="utf-8"))
    assert out.startswith("[OK]"), out
    raw = (wd / "result.json").read_bytes()
    return json.loads(raw), raw, run_id


def _cfg(grain, horizon, season, kind, rows, step):
    last = rows[-1][0]
    return {"grain": grain, "horizon": horizon, "season": season, "kind": kind,
            "as_of": (last + timedelta(days=step)).isoformat(), "max_history": 3000,
            "actual_tail": 12}


def _check_ok(res, rows, cfg, nonneg=True):
    assert res["ok"] is True, res
    assert res["method"] in ("naive", "moving_avg", "seasonal_naive", "ets", "arima")
    assert res["h"] == cfg["horizon"] == len(res["points"])
    assert res["origins"] >= 1
    prev = rows[-1][0].isoformat()
    for p in res["points"]:
        assert p["period"] > prev
        prev = p["period"]
        for k in ("mean", "low", "high"):
            assert isinstance(p[k], float) and math.isfinite(p[k])
        assert p["low"] <= p["mean"] <= p["high"]
        if nonneg:
            assert p["low"] >= 0 and p["mean"] >= 0
    assert len(res["actuals"]) <= cfg["actual_tail"]
    assert res["actuals"][-1]["period"] == rows[-1][0].isoformat()
    assert res["wape"] is None or math.isfinite(res["wape"])


async def test_weekly_seasonal_series():
    vals = [1000 + 4 * i + 300 * math.sin(2 * math.pi * i / 52) + 40 * _wobble(i) for i in range(120)]
    rows = _series(date(2024, 1, 1), 7, vals)
    cfg = _cfg("week", 8, 52, "flow", rows, 7)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    assert res["n_periods"] == 120


async def test_daily_weekly_pattern():
    vals = [200 + 60 * (1 if i % 7 in (5, 6) else 0) + 0.1 * i + 10 * _wobble(i) for i in range(400)]
    rows = _series(date(2025, 1, 1), 1, vals)
    cfg = _cfg("day", 30, 7, "flow", rows, 1)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    first = date.fromisoformat(res["points"][0]["period"])
    assert first == rows[-1][0] + timedelta(days=1)
    assert [date.fromisoformat(p["period"]) for p in res["points"]][-1] == rows[-1][0] + timedelta(days=30)


async def test_gappy_daily_flow():
    vals = [100 + 20 * (i % 7) + 5 * _wobble(i) for i in range(120)]
    rows = _series(date(2026, 1, 1), 1, vals)
    rows = [r for i, r in enumerate(rows) if not (50 <= i < 60)]
    cfg = _cfg("day", 14, 7, "flow", rows, 1)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    assert res["n_periods"] == 120


async def test_weekly_level_no_season():
    vals = [5000 + 10 * i + 80 * _wobble(i) for i in range(80)]
    rows = _series(date(2025, 1, 6), 7, vals)
    cfg = _cfg("week", 6, 0, "level", rows, 7)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    assert res["method"] != "seasonal_naive"
    cand = [n for n in res["notes"] if n.startswith("candidates:")][0]
    assert "seasonal_naive" not in cand
    assert any("no season" in n for n in res["notes"])


async def test_constant_series():
    rows = _series(date(2026, 1, 1), 1, [50.0] * 60)
    cfg = _cfg("day", 7, 7, "flow", rows, 1)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    assert res["method"] == "naive"
    for p in res["points"]:
        assert p["mean"] == 50.0 and p["low"] == 50.0 and p["high"] == 50.0


async def test_too_short():
    rows = _series(date(2026, 1, 5), 7, [10.0 + i for i in range(8)])
    cfg = _cfg("week", 4, 52, "flow", rows, 7)
    res, _, _ = await _run(rows, cfg)
    assert res == {"ok": False, "reason": "too_short", "message": "8 periods; needs 12"}


async def test_bad_input_is_a_readable_result():
    _counter["n"] += 1
    run_id = "forecast_t%d" % _counter["n"]
    wd = sandbox.scratch_dir(run_id)
    (wd / "series.csv").write_text("period,value\nnope,1\n", encoding="utf-8")
    (wd / "config.json").write_text(json.dumps({"grain": "day", "horizon": 3, "as_of": "2026-01-01"}))
    out = await sandbox.run_python(run_id, RUNNER_PATH.read_text(encoding="utf-8"))
    assert out.startswith("[OK]"), out
    res = json.loads((wd / "result.json").read_text())
    assert res["ok"] is False and res["reason"] == "bad_input"


async def test_deterministic_byte_identical():
    vals = [300 + 2 * i + 50 * math.sin(2 * math.pi * i / 52) + 8 * _wobble(i) for i in range(110)]
    rows = _series(date(2024, 3, 4), 7, vals)
    cfg = _cfg("week", 8, 52, "flow", rows, 7)
    _, raw1, run_id = await _run(rows, cfg, name="forecast_det")
    (sandbox.scratch_dir(run_id) / "result.json").unlink()
    _, raw2, _ = await _run(rows, cfg, name="forecast_det")
    assert raw1 == raw2


async def test_ets_candidate_is_not_dropped():
    """Regression: statsmodels 0.15 ETS get_prediction needs pandas input; a numpy-only
    call dropped ets with AttributeError and left only the baselines."""
    vals = [30 + 5 * math.sin(2 * math.pi * i / 7) + 2 * _wobble(i) for i in range(200)]
    rows = _series(date(2025, 1, 1), 1, vals)
    cfg = _cfg("day", 14, 7, "flow", rows, 1)
    res, _, _ = await _run(rows, cfg)
    _check_ok(res, rows, cfg)
    notes = " ".join(res.get("notes") or [])
    assert "dropped" not in notes, notes
    assert "ets" in notes.split("candidates:")[-1], notes


# -- Quick 013 (WR-02): trailing empty flow periods and stale series ------------

async def test_trailing_empty_days_are_zero_filled():
    vals = [100 + 20 * (i % 7) + 5 * _wobble(i) for i in range(120)]
    rows = _series(date(2026, 1, 1), 1, vals)
    cfg = _cfg("day", 14, 7, "flow", rows, 4)          # three complete days with no rows
    res, _, _ = await _run(rows, cfg)
    assert res["ok"] is True, res
    assert [a["value"] for a in res["actuals"][-3:]] == [0.0, 0.0, 0.0]
    assert res["actuals"][-1]["period"] == (rows[-1][0] + timedelta(days=3)).isoformat()
    assert res["points"][0]["period"] == cfg["as_of"]   # starts at the current day, not in the past
    assert any("counted as 0" in n for n in res["notes"])


async def test_stale_series_is_a_readable_failure():
    vals = [100 + 20 * (i % 7) for i in range(120)]
    rows = _series(date(2026, 1, 1), 1, vals)
    cfg = _cfg("day", 14, 7, "flow", rows, 30)
    res, _, _ = await _run(rows, cfg)
    assert res["ok"] is False and res["reason"] == "stale", res
