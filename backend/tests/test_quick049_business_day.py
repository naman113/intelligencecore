"""Quick 049: "today" is the business day (operator timezone); triage dates requests on it;
an honest "nothing to report" reply satisfies the request. Fixed inputs, no DB, no LLM."""

import json
from datetime import datetime, timezone

from app import bizclock, triage
from tests.test_assistant_requests import DM, SweepDB, _wire_sweep

# The owner's message: 19:02 UTC on 4 Nov = 14:02 Toronto = 00:32 IST on 5 Nov.
SENT = datetime(2026, 11, 4, 19, 2, tzinfo=timezone.utc)


def test_just_started_day_is_flagged_and_names_the_last_full_day():
    line = bizclock.clock_line(SENT, "Asia/Kolkata")
    assert "Business today = 2026-11-05 (Thu)" in line
    assert "last full business day = 2026-11-04 (Wed)" in line
    assert "day_just_started = true" in line and "0h32m" in line


def test_threshold_is_six_hours():
    zone = "Asia/Kolkata"
    # 05:29 IST = 23:59 UTC the day before; 05:30 IST = 00:00 UTC; 06:00 IST = 00:30 UTC
    assert "day_just_started = true" in bizclock.clock_line(
        datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc), zone)
    assert "day_just_started = true" in bizclock.clock_line(
        datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc), zone)
    assert "day_just_started = false" in bizclock.clock_line(
        datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc), zone)


def test_invalid_zone_falls_back_to_utc(monkeypatch):
    class S:
        timezone = "Not/AZone"
    monkeypatch.setattr(bizclock, "get_settings", lambda: S())
    assert bizclock.tz_name() == "UTC"
    assert "business timezone UTC" in bizclock.clock_line(SENT)


def test_triage_now_is_business_dated():
    now = bizclock.business_now_text(SENT, "Asia/Kolkata")
    assert "business date 2026-11-05" in now and "Asia/Kolkata" in now
    assert "UTC 2026-11-04T19:02:00Z" in now


def test_event_carries_its_business_time():
    ev = {"id": 1, "source": "whatsapp", "content": "Send it for today now",
          "occurred_at": SENT}
    out = triage._fmt_event(ev)
    assert out["occurred_at"].startswith("2026-11-04T19:02")
    assert out["occurred_business"].startswith("2026-11-0")  # operator zone, set by settings


def test_triage_prompt_dates_on_the_business_calendar():
    p = " ".join(triage.TRIAGE_SYSTEM.split())
    assert "BUSINESS DATES" in p and "occurred_business" in p and "never from the UTC date" in p


def test_sweep_prompt_accepts_an_honest_nothing_to_report():
    p = " ".join(triage.SATISFY_SYSTEM.split())
    assert "truthfully reports there is nothing for the period" in p
    assert "empty details are the correct answer" in p
    assert "could not look" in p and "does not" in p
    assert "could not find" in p  # the old rule against failure replies stays


REQ = {"id": 17, "content": "Send it for today now", "thread_ref": DM,
       "provenance": "external", "triage_status": "done"}
NOTHING = {"id": 20, "thread_ref": DM,
           "content": "No rolls received so far today (Thu 5 Nov IST): 0 rolls. Last "
                      "received 4 Nov: 18 rolls (details below)."}
COMMIT = {"id": 5, "owed_by": "assistant", "source_event_id": 17,
          "description": "Send today's received rolls details report now (5 Nov 2026), "
                         "including roll count and roll details"}


async def test_sweep_closes_on_a_truthful_nothing_reply_and_shows_business_now(monkeypatch):
    db = SweepDB([REQ, NOTHING], [COMMIT])
    seen = _wire_sweep(monkeypatch, db,
                       lambda p: [{"expectation_id": 5, "delivery_event_id": 20}])
    await triage._satisfaction_sweep()
    assert db.exps[5]["status"] == "satisfied" and db.exps[5]["satisfied_by"] == 20
    payload, = seen
    assert "business date" in payload["now"] and "UTC" in payload["now"]
    assert json.loads(json.dumps(payload))["open_expectations"][0]["asked"] == REQ["content"]


def test_agent_gets_the_clock_and_the_rules():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "agents" / "graph.py").read_text(
        encoding="utf-8")
    assert 'messages.append(("user", bizclock.clock_line()))' in src
    m = re.search(r'GROUNDING_RULES \+= """(.*?)"""', src, re.S)
    rules = " ".join(m.group(1).split())
    for phrase in ("day_just_started = true", "last full business day", "today so far",
                   "5 Nov IST", "date of the last activity"):
        assert phrase in rules
