"""Quick 047: "send me X every day at 9 PM" makes a scheduled WhatsApp report.

The agent proposes (actions gate), the owner approves, the executor writes an active
cron playbook with a frozen report spec, the cron dispatch queues it at HH:MM in the
report's zone, and the run reads, formats and sends with fixed code and no model call.
Pure logic: fakes only, no DB, LLM or network."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from _supprompt import composed_supervisor_prompt

from app import (expectations, freshness, guardrails, memscope, modelcompiler, modeltools,
                 playbooks, reports, reporttools, runsapi, runstatus, semanticmodel)
from app.agents import graph

IST = ZoneInfo("Asia/Kolkata")
SQL_ITEM = {"label": "Today's rolls",
            "sql": "SELECT roll_no, weight_kg FROM rolls WHERE received_at >= {today} "
                   "AND received_at < {tomorrow} ORDER BY roll_no"}
MEASURE_ITEM = {"label": "Rolls received", "measure": "rolls_received", "range": "today"}


# ── fakes ────────────────────────────────────────────────────────────────────

class Pool:
    """Just enough of asyncpg for reports.py, keyed on the statements it sends."""

    def __init__(self):
        self.playbooks: list[dict] = []
        self.actions: list[dict] = []
        self.runs: dict = {}
        self.next_id = 100

    def _id(self):
        self.next_id += 1
        return self.next_id

    def _live(self):
        return [p for p in self.playbooks if p.get("report") and p["status"] != "archived"]

    async def fetchval(self, sql, *a):
        s = " ".join(sql.split())
        if "FROM playbooks WHERE report IS NOT NULL AND status <> 'archived' AND report->>'title_key'" in s:
            hit = [p for p in self._live() if json.loads(p["report"])["title_key"] == a[0]]
            return hit[0]["id"] if hit else None
        if "FROM actions WHERE kind = $1 AND status = 'proposed' AND payload->>'title_key'" in s:
            hit = [x for x in self.actions if x["kind"] == a[0] and x["status"] == "proposed"
                   and x["payload"].get("title_key") == a[1]]
            return hit[-1]["id"] if hit else None
        if s.startswith("SELECT count(*) FROM playbooks WHERE report IS NOT NULL"):
            return len(self._live())
        if "count(*) FROM actions WHERE kind = $1 AND status = 'proposed'" in s:
            return sum(1 for x in self.actions if x["kind"] == a[0] and x["status"] == "proposed")
        if "count(*) FROM actions WHERE kind = $1 AND task_run_id = $2" in s:
            return sum(1 for x in self.actions if x["kind"] == a[0] and x["task_run_id"] == a[1])
        if s.startswith("INSERT INTO actions (kind, payload, tier, status, task_run_id)"):
            aid = self._id()
            self.actions.append({"id": aid, "kind": a[0], "payload": json.loads(a[1]),
                                 "tier": "draft", "status": "proposed", "task_run_id": a[2]})
            return aid
        if "FROM playbooks WHERE report->>'proposal_key' = $1" in s:
            hit = [p for p in self.playbooks
                   if p.get("report") and json.loads(p["report"])["proposal_key"] == a[0]]
            return (hit[0]["id"] if s.startswith("SELECT id") else 1) if hit else None
        if s.startswith("SELECT 1 FROM playbooks WHERE name = $1"):
            return 1 if any(p["name"] == a[0] for p in self.playbooks) else None
        if s.startswith("INSERT INTO playbooks"):
            if any(p["name"] == a[0] for p in self.playbooks):
                return None
            pid = self._id()
            self.playbooks.append({"id": pid, "name": a[0], "trigger": a[1], "goal": a[2],
                                   "report": a[3], "status": "active", "sql": s})
            return pid
        if s.startswith("SELECT report IS NOT NULL FROM playbooks WHERE id = $1"):
            hit = [p for p in self.playbooks if p["id"] == a[0]]
            return bool(hit and hit[0].get("report")) if hit else None
        raise AssertionError(f"unexpected fetchval: {s[:120]}")

    async def execute(self, sql, *a):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO actions (playbook_id, task_run_id, kind, payload"):
            self.actions.append({"id": self._id(), "playbook_id": a[0], "task_run_id": a[1],
                                 "kind": "whatsapp_message", "payload": json.loads(a[2]),
                                 "status": a[3]})
            return "INSERT 0 1"
        if s.startswith("UPDATE task_runs SET status = $2"):
            self.runs[a[0]] = {"status": a[1], "summary": a[2], "error": a[3]}
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s[:120]}")


@pytest.fixture
def pool(monkeypatch):
    p = Pool()

    async def gp():
        return p
    monkeypatch.setattr(reports, "get_pool", gp)
    return p


@pytest.fixture
def env(monkeypatch, pool):
    """Owner number set, operator zone Asia/Kolkata (as on Mill), the clock frozen
    at 2026-11-04 15:30 UTC (21:00 IST), guardrails rows from `rows`, sends recorded,
    freshness never stale, every model constructor forbidden."""
    st = SimpleNamespace(owner_number="919800000001", timezone="Asia/Kolkata")
    monkeypatch.setattr(reports, "get_settings", lambda: st)
    from app import kpihistory
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    monkeypatch.setattr(reports, "_utcnow", lambda: datetime(2026, 11, 4, 15, 30, tzinfo=timezone.utc))
    state = {"rows": [{"roll_no": "R-1", "weight_kg": Decimal("512.5")},
                      {"roll_no": "R-2", "weight_kg": Decimal("498")}],
             "sql": [], "sends": [], "send_ok": True, "settings": st}

    async def rro(sql, limit=None):
        state["sql"].append((sql, limit))
        if isinstance(state["rows"], Exception):
            raise state["rows"]
        return list(state["rows"])
    monkeypatch.setattr(guardrails, "run_readonly_sql", rro)

    async def send(phone, message):
        state["sends"].append((phone, message))
        return state["send_ok"]
    monkeypatch.setattr(reports, "whatsapp_send", send)

    async def fresh(sql, **kw):
        return SimpleNamespace(stale=False, summary=lambda: "")
    monkeypatch.setattr(freshness, "for_sql", fresh)

    def no_model(*a, **k):
        raise AssertionError("a scheduled report must make no model call")
    from app import llmclient
    monkeypatch.setattr(llmclient, "chat_anthropic", no_model)
    monkeypatch.setattr(llmclient, "chat_openai", no_model)
    monkeypatch.setattr(playbooks, "create_react_agent", no_model)
    return state


def _ident(kind, origin, label="x", sender=None, thread=""):
    return memscope.RunIdentity(kind, "s", None, (), origin, label, thread, sender)


@pytest.fixture
def who(monkeypatch):
    box = {"ident": _ident("owner", "whatsapp", "the owner (WhatsApp)")}

    async def for_thread(t):
        return box["ident"]
    monkeypatch.setattr(memscope, "for_thread", for_thread)
    return box


# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("at,ok", [("21:00", (21, 0)), ("9:05", (9, 5)), (" 00:00 ", (0, 0)),
                                   ("23:59", (23, 59)), ("24:00", None), ("9 PM", None),
                                   ("21", None), ("21:5", None), (None, None), (2100, None)])
def test_parse_at(at, ok):
    assert reports.parse_at(at) == ok


def test_zone_name(monkeypatch):
    from app import kpihistory
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    assert reports.zone_name("") == ("Asia/Kolkata", None)      # operator zone
    assert reports.zone_name(None) == ("Asia/Kolkata", None)
    assert reports.zone_name("Europe/London") == ("Europe/London", None)
    for bad in ("IST", "Mars/Base", "../etc/passwd", "x" * 80, 5):
        name, err = reports.zone_name(bad)
        assert name is None and "IANA" in err


def test_validate_items_accepts_measures_and_tables():
    items, err = reports.validate_items([
        MEASURE_ITEM, dict(SQL_ITEM),
        {"label": "By supplier", "measure": "rolls_weight", "range": "yesterday",
         "by": ["rolls.supplier"], "filters": [{"dimension": "rolls.grade", "op": "=",
                                                "values": ["A"]}]}])
    assert err is None and len(items) == 3
    assert items[0] == {"label": "Rolls received", "measure": "rolls_received",
                        "range": "today", "filters": [], "by": []}
    assert items[1] == {"label": "Today's rolls", "sql": SQL_ITEM["sql"]}


@pytest.mark.parametrize("items,needle", [
    ([], "1 to 6"),
    ([MEASURE_ITEM] * 7, "at most 6"),
    (["x"], "must be an object"),
    ([{"measure": "m"}], "needs a label"),
    ([{"label": "a", "measure": "m", "sql": "select 1"}], "exactly one"),
    ([{"label": "a"}], "exactly one"),
    ([{"label": "a", "measure": "m", "grain": "day"}], "takes only"),
    ([{"label": "a", "measure": "Bad Name"}], "modeled measure"),
    ([{"label": "a", "measure": "m", "range": "next_week"}], "range must be one of"),
    ([{"label": "a", "measure": "m", "by": ["a", "b", "c"]}], "at most 2"),
    ([{"label": "a", "measure": "m", "filters": "x"}], "filters"),
    ([{"label": "a", "sql": "DELETE FROM rolls"}], "rejected"),
    ([{"label": "a", "sql": "SELECT 1; SELECT 2"}], "rejected"),
    ([{"label": "a", "sql": "SELECT 1​"}], "hidden"),
    ([{"label": "a", "sql": "SELECT 1", "limit": 5}], "takes only"),
    ([{"label": "a", "sql": "SELECT " + "1," * 3000 + "1"}], "longer than"),
])
def test_validate_items_refusals(items, needle):
    out, err = reports.validate_items(items)
    assert out is None and needle in err


def test_validate_spec_and_trigger(monkeypatch):
    from app import kpihistory
    monkeypatch.setattr(kpihistory, "_tz", lambda: "UTC")
    spec, err = reports.validate_spec(" Rolls *received* today ", "9:00", "Asia/Kolkata",
                                      [MEASURE_ITEM])
    assert err is None
    assert spec["title"] == "Rolls received today" and spec["title_key"] == "rolls received today"
    assert (spec["at"], spec["hour"], spec["minute"], spec["timezone"]) == ("09:00", 9, 0,
                                                                            "Asia/Kolkata")
    assert reports.trigger_for(spec) == {"type": "cron", "hour": 9, "minute": 0,
                                         "tz": "Asia/Kolkata"}
    assert reports.validate_spec("", "21:00", "", [MEASURE_ITEM])[1].startswith("title")
    assert reports.validate_spec("t", "9pm", "", [MEASURE_ITEM])[1].startswith("at must")


def test_fill_tokens_uses_the_report_day():
    out = reports.fill_tokens(SQL_ITEM["sql"] + " -- {yesterday}", date(2026, 11, 4))
    assert "DATE '2026-11-04'" in out and "DATE '2026-11-05'" in out
    assert "DATE '2026-11-03'" in out and "{" not in out


# ── formatting ───────────────────────────────────────────────────────────────

def test_fmt_value():
    assert reports.fmt_value(None) == "-"
    assert reports.fmt_value(True) == "yes"
    assert reports.fmt_value(1234567) == "1,234,567"
    assert reports.fmt_value(Decimal("498")) == "498"
    assert reports.fmt_value(Decimal("1234.5")) == "1,234.50"
    assert reports.fmt_value(float("nan")) == "nan"
    assert reports.fmt_value(datetime(2026, 11, 4, 9, 7, 30)) == "2026-11-04 09:07"
    assert reports.fmt_value(date(2026, 11, 4)) == "2026-11-04"
    assert reports.fmt_value("a *bold*\nname") == "a bold name"
    assert reports.fmt_value("x" * 60).endswith("…") and len(reports.fmt_value("x" * 60)) == 40


def test_table_text_caps_rows_and_scalar_shortcut():
    rows = [{"roll": f"R-{i}", "kg": i} for i in range(25)]
    t = reports.table_text("Rolls", rows).split("\n")
    assert t[0] == "Rolls:" and t[1] == "roll · kg" and t[2] == "R-0 · 0"
    assert len(t) == 2 + reports.MAX_TABLE_ROWS + 1 and t[-1] == "(first 20 rows shown)"
    assert reports.table_text("Count", [{"n": 42}]) == "Count: 42"
    assert reports.table_text("Rolls", []) == "Rolls: none"


def test_compose_heads_with_local_time_and_caps():
    now = datetime(2026, 11, 4, 21, 0, tzinfo=IST)
    r = [reports.ItemResult("a", True, "a: 1")]
    msg = reports.compose("Rolls", now, r, ["Note: stale"])
    assert msg == "*Rolls*\nWed 04 Nov 2026, 21:00 IST\n\na: 1\n\nNote: stale"
    big = [reports.ItemResult("a", True, "x" * 5000)]
    out = reports.compose("Rolls", now, big)
    assert len(out) <= reports.MESSAGE_MAX and out.endswith("(report cut to fit one message)")


# ── reading items (no model call; governed path and guardrails) ──────────────

def _fake_model(monkeypatch, unit="kg", columns=("v",)):
    seen = {}

    async def load():
        return SimpleNamespace(measure=lambda n: SimpleNamespace(unit=unit))

    def bmr(model, measure, by=None, grain=None, filters=None, range=None, today=None):
        seen.update(measure=measure, by=by, filters=filters, range=range, today=today)
        return SimpleNamespace(measure=measure), None, None

    def compile_measure(model, request):
        return modelcompiler.Compiled(sql="SELECT 1 AS v", columns=columns,
                                      measure=request.measure, joins=(), notes=())
    monkeypatch.setattr(semanticmodel, "load_model", load)
    monkeypatch.setattr(modeltools, "build_measure_request", bmr)
    monkeypatch.setattr(modelcompiler, "compile_measure", compile_measure)
    return seen


async def test_render_measure_today_is_the_report_day(monkeypatch, env):
    seen = _fake_model(monkeypatch)
    env["rows"] = [{"v": Decimal("1234.5")}]
    spec = {"title": "Rolls", "items": [dict(MEASURE_ITEM, filters=[], by=[])]}
    msg, res = await reports.render(spec, datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert res[0].ok and "Rolls received: 1,234.50 kg" in msg
    assert seen["range"] == {"start": "2026-11-04", "end": "2026-11-05"}
    assert seen["today"] == date(2026, 11, 4)
    assert env["sql"] == [("SELECT 1 AS v", reports.MAX_TABLE_ROWS + 1)]


async def test_render_measure_breakdown_and_relative_range(monkeypatch, env):
    seen = _fake_model(monkeypatch, unit="", columns=("supplier", "v"))
    env["rows"] = [{"supplier": "Acme", "v": 3}, {"supplier": "Bolt", "v": 2}]
    item = {"label": "By supplier", "measure": "rolls_received", "range": "last_7_days",
            "filters": [], "by": ["rolls.supplier"]}
    msg, res = await reports.render({"title": "R", "items": [item]},
                                    datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert seen["range"] == "last_7_days" and seen["by"] == ["rolls.supplier"]
    assert "By supplier:\nAcme: 3\nBolt: 2" in msg


async def test_render_sql_table_fills_tokens(env):
    msg, res = await reports.render({"title": "R", "items": [dict(SQL_ITEM)]},
                                    datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    sql, limit = env["sql"][0]
    assert "DATE '2026-11-04'" in sql and "DATE '2026-11-05'" in sql and limit == 21
    assert "Today's rolls:\nroll_no · weight_kg\nR-1 · 512.50\nR-2 · 498" in msg
    assert res[0].ok and res[0].sql == sql


async def test_render_failure_is_one_line_and_never_raises(monkeypatch, env):
    def not_modeled(*a, **k):
        raise modelcompiler.NotModeled("measure rolls_received is not modeled")
    _fake_model(monkeypatch)
    monkeypatch.setattr(modelcompiler, "compile_measure", not_modeled)
    msg, res = await reports.render(
        {"title": "R", "items": [dict(MEASURE_ITEM, filters=[], by=[]), dict(SQL_ITEM)]},
        datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert not res[0].ok and res[0].error.startswith("NOT MODELED")
    assert "Rolls received: could not be read today." in msg and res[1].ok
    env["rows"] = ConnectionError("host=secret user=x")
    msg, res = await reports.render({"title": "R", "items": [dict(SQL_ITEM)]},
                                    datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert res[0].error == "the business database is unavailable" and "secret" not in msg


async def test_render_adds_a_stale_note(monkeypatch, env):
    async def stale(sql, **kw):
        return SimpleNamespace(stale=True, summary=lambda: "Stale data: rolls (last sync never)")
    monkeypatch.setattr(freshness, "for_sql", stale)
    msg, _ = await reports.render({"title": "R", "items": [dict(SQL_ITEM), dict(SQL_ITEM)]},
                                  datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert msg.count("Note: Stale data: rolls (last sync never)") == 1


# ── recipient (decided in code) ──────────────────────────────────────────────

async def test_recipient_for_thread(who):
    who["ident"] = _ident("owner", "whatsapp")
    assert await reports.recipient_for_thread("919800000001") == ("owner", "the owner")
    who["ident"] = _ident("owner", "web")
    assert await reports.recipient_for_thread("ui:owner") == ("owner", "the owner")
    who["ident"] = _ident("unattributed", "group", "a group")
    assert await reports.recipient_for_thread("123-456@g.us") == ("owner", "the owner")
    who["ident"] = _ident("person", "whatsapp", "Ravi", "919811111111")
    assert await reports.recipient_for_thread("919811111111") == ("919811111111", "Ravi")
    who["ident"] = _ident("unattributed", "sender", "an unregistered sender (+919822222222)",
                          "919822222222")
    to, label = await reports.recipient_for_thread("919822222222")
    assert to == "919822222222" and "2222222" not in label and "919" in label
    who["ident"] = _ident("unattributed", "sender", "x", "123456789012345")
    assert (await reports.recipient_for_thread("123456789012345@lid"))[0] == "123456789012345@lid"


# ── propose ──────────────────────────────────────────────────────────────────

async def test_propose_records_a_proposal_with_preview(env, pool, who):
    out = await reports.propose("Rolls received today", "21:00", [dict(SQL_ITEM)],
                                "Asia/Kolkata", thread_id="919800000001", run_id=7)
    assert out.startswith("proposed daily report (action #")
    assert "21:00 Asia/Kolkata" in out and "awaiting owner approval" in out
    assert "R-1 · 512.50" in out and env["sends"] == []        # nothing sent yet
    a = pool.actions[0]
    assert a["kind"] == "create_scheduled_report" and a["status"] == "proposed"
    assert a["tier"] == "draft" and a["task_run_id"] == 7
    p = a["payload"]
    assert p["deliver_to"] == "owner" and p["hour"] == 21 and p["minute"] == 0
    assert p["_summary"] == ("Daily report: Rolls received today at 21:00 Asia/Kolkata, "
                             "to the owner on WhatsApp")
    assert p["preview"].startswith("*Rolls received today*") and len(p["proposal_key"]) == 32


async def test_propose_refuses_bad_spec_failing_item_and_conflicts(env, pool, who):
    assert (await reports.propose("R", "9 PM", [SQL_ITEM], "", thread_id="t",
                                  run_id=1)).startswith("REFUSED: at must")
    env["rows"] = TimeoutError()
    out = await reports.propose("R", "21:00", [SQL_ITEM], "", thread_id="t", run_id=1)
    assert out.startswith("REFUSED: item 'Today's rolls': took too long") and not pool.actions
    env["rows"] = [{"n": 1}]
    assert "action #" in await reports.propose("R", "21:00", [SQL_ITEM], "", thread_id="t",
                                               run_id=1)
    again = await reports.propose("r!", "08:00", [SQL_ITEM], "", thread_id="t", run_id=2)
    assert again.startswith("REFUSED: already proposed (action #")
    assert "action #" in await reports.propose("Second", "21:00", [SQL_ITEM], "",
                                               thread_id="t", run_id=1)
    cap = await reports.propose("Third", "21:00", [SQL_ITEM], "", thread_id="t", run_id=1)
    assert cap.startswith("REFUSED: this run already proposed 2")


async def test_propose_refuses_at_the_report_limit(env, pool, who, monkeypatch):
    monkeypatch.setattr(reports, "MAX_REPORTS", 1)
    pool.playbooks.append({"id": 1, "name": "Report: a", "status": "active",
                           "report": json.dumps({"title_key": "a", "proposal_key": "k"})})
    out = await reports.propose("B", "21:00", [SQL_ITEM], "", thread_id="t", run_id=1)
    assert out.startswith("REFUSED: the daily report limit (1)")


async def test_tool_passes_thread_and_run(monkeypatch):
    seen = {}

    async def prop(*a, **kw):
        seen.update(a=a, kw=kw)
        return "proposed daily report (action #5): x"
    logged = []

    async def log_tool(name, args, result, start, refused=False):
        logged.append((name, args, refused))
    monkeypatch.setattr(reports, "propose", prop)
    monkeypatch.setattr(reporttools, "_log_tool", log_tool)
    t1, t2 = graph.ctx_phone.set("919800000001"), graph.ctx_run_id.set(9)
    try:
        out = await reporttools.propose_scheduled_report.ainvoke(
            {"title": "R", "at": "21:00", "items": [SQL_ITEM], "timezone": "Asia/Kolkata"})
    finally:
        graph.ctx_phone.reset(t1)
        graph.ctx_run_id.reset(t2)
    assert out.startswith("proposed daily report")
    assert seen["kw"] == {"thread_id": "919800000001", "run_id": 9}
    assert seen["a"] == ("R", "21:00", [SQL_ITEM], "Asia/Kolkata")
    assert logged == [("propose_scheduled_report",
                       {"title": "R", "at": "21:00", "timezone": "Asia/Kolkata", "items": 1},
                       False)]


def test_tool_description_names_the_contract():
    d = reporttools.propose_scheduled_report.description
    for need in ("EVERY DAY", "HH:MM", "21:00", "Asia/Kolkata", "get_model", "{today}",
                 "{tomorrow}", "at most 20", "1 to 6", "no AI run", "Approvals", "preview"):
        assert need in d, need
    assert "watch" not in d.lower()


# ── approval executor ────────────────────────────────────────────────────────

def _payload(**over):
    spec, _ = reports.validate_spec("Rolls received today", "21:00", "Asia/Kolkata",
                                    [MEASURE_ITEM, SQL_ITEM])
    return {**spec, "deliver_to": "owner", "deliver_label": "the owner",
            "proposal_key": "a" * 32, "_summary": "x", **over}


async def test_executor_creates_an_active_cron_playbook(env, pool):
    assert await reports.create_from_proposal(_payload()) is True
    pb = pool.playbooks[0]
    assert pb["name"] == "Report: Rolls received today" and pb["status"] == "active"
    assert json.loads(pb["trigger"]) == {"type": "cron", "hour": 21, "minute": 0,
                                         "tz": "Asia/Kolkata"}
    assert "ARRAY[]::text[], 'notify', NULL, 'active', 'human', 'human'" in pb["sql"]
    rep = json.loads(pb["report"])
    assert rep["deliver_to"] == "owner" and rep["proposal_key"] == "a" * 32
    assert len(rep["items"]) == 2 and pb["goal"].startswith("Daily report, no AI run")
    # a retry of the same approval is a success that inserts nothing
    assert await reports.create_from_proposal(_payload()) is True and len(pool.playbooks) == 1
    # same title, other proposal: a distinct name
    assert await reports.create_from_proposal(_payload(proposal_key="b" * 32)) is True
    assert pool.playbooks[1]["name"] == "Report: Rolls received today (bbbbbb)"


@pytest.mark.parametrize("over", [
    {"deliver_to": "120363000000@g.us"}, {"deliver_to": "ui:owner"}, {"deliver_to": None},
    {"proposal_key": ""}, {"proposal_key": "k" * 65}, {"at": "25:00"},
    {"timezone": "Nowhere/City"}, {"items": [{"label": "x", "sql": "DROP TABLE rolls"}]},
])
async def test_executor_refuses_tampered_payloads(env, pool, over):
    assert await reports.create_from_proposal(_payload(**over)) is False
    assert pool.playbooks == []
    assert await reports.create_from_proposal("not a dict") is False


async def test_actionsvc_routes_the_kind(monkeypatch):
    from app import actionsvc
    seen = []

    async def create(payload):
        seen.append(payload)
        return True
    monkeypatch.setattr(reports, "create_from_proposal", create)
    assert await actionsvc._execute("create_scheduled_report", {"k": 1}) is True
    assert seen == [{"k": 1}]


# ── the daily run ────────────────────────────────────────────────────────────

def _pb(pool, **over):
    rep = {**_payload(items=[SQL_ITEM]), **over}
    rep.pop("_summary", None)
    pb = {"id": 5, "name": "Report: Rolls received today", "status": "active",
          "report": json.dumps(rep)}
    pool.playbooks.append(pb)
    return pb


async def test_run_sends_records_and_finishes(env, pool):
    await reports.run({"id": 77}, _pb(pool))
    (phone, msg), = env["sends"]
    assert phone == "919800000001" and msg.startswith("*Rolls received today*\n")
    assert "IST" in msg.split("\n")[1] and "R-1 · 512.50" in msg
    sent = [a for a in pool.actions if a["kind"] == "whatsapp_message"]
    assert sent[0]["status"] == "executed" and sent[0]["task_run_id"] == 77
    assert sent[0]["payload"]["recipient"] == "owner" and sent[0]["payload"]["body"] == msg
    assert pool.runs[77]["status"] == "done" and pool.runs[77]["error"] is None
    assert "1 of 1 items read" in pool.runs[77]["summary"]


async def test_run_to_the_requester_thread(env, pool):
    await reports.run({"id": 78}, _pb(pool, deliver_to="919811111111", deliver_label="Ravi"))
    assert env["sends"][0][0] == "919811111111"
    assert pool.actions[-1]["payload"]["recipient"] == "requester"


async def test_run_without_owner_number_fails_and_sends_nothing(env, pool):
    env["settings"].owner_number = ""
    await reports.run({"id": 79}, _pb(pool))
    assert env["sends"] == [] and pool.runs[79]["status"] == "failed"
    assert "owner_number" in pool.runs[79]["error"]


async def test_run_send_failure_and_partial_items_fail_the_run(env, pool):
    env["send_ok"] = False
    await reports.run({"id": 80}, _pb(pool))
    assert pool.runs[80]["status"] == "failed" and "WhatsApp send failed" in pool.runs[80]["error"]
    assert pool.actions[-1]["status"] == "failed"
    env["send_ok"], env["rows"] = True, RuntimeError("boom")
    await reports.run({"id": 81}, _pb(pool))
    assert env["sends"][-1][1].endswith("Today's rolls: could not be read today.")
    assert pool.runs[81]["status"] == "failed"
    assert pool.runs[81]["error"].startswith("sent, but these items could not be read")


async def test_run_never_raises(env, pool, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("render broke")
    monkeypatch.setattr(reports, "render", boom)
    await reports.run({"id": 82}, _pb(pool))
    assert pool.runs[82] == {"status": "failed", "summary": None,
                             "error": "RuntimeError: render broke"}
    bad = {"id": 6, "name": "x", "status": "active", "report": "{not json"}
    await reports.run({"id": 83}, bad)
    assert pool.runs[83]["error"] == "the report spec is missing or unreadable"


async def test_playbook_engine_hands_report_rows_over_without_an_agent(env, monkeypatch):
    row = {"id": 5, "name": "Report: x", "status": "active", "tool_grants": [],
           "report": json.dumps({"title": "x"})}

    class PbPool:
        async def fetchrow(self, sql, *a):
            return row

    async def gp():
        return PbPool()
    ran = []

    async def fake_run(task, pb):
        ran.append((task["id"], pb["id"]))

    async def no_ctx(*a, **k):
        raise AssertionError("no agent context for a report")
    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(reports, "run", fake_run)
    monkeypatch.setattr(playbooks, "_assemble_context", no_ctx)
    await playbooks.run_playbook_task({"id": 9, "playbook_id": 5, "case_id": None,
                                       "expectation_id": None})
    assert ran == [(9, 5)]
    assert reports.report_of({"report": None}) is None
    assert reports.report_of({"id": 1}) is None              # an agent playbook row
    assert reports.report_of({"report": {"title": "x"}}) == {"title": "x"}


async def test_worker_budget_pause_skips_report_runs(monkeypatch):
    from app import usage, worker
    from tests.test_prompt_layout import FakeConn, FakePool, _wire_worker

    class PlaybookConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "UPDATE task_runs SET status = 'running'" in sql:
                return {"id": 8, "thread_id": "playbook-global", "message_id": None,
                        "kind": "playbook", "playbook_id": 5, "case_id": None,
                        "expectation_id": None}
            return None

    class ReportPool(FakePool):
        async def fetchval(self, sql, *args):
            if "SELECT report IS NOT NULL FROM playbooks" in sql:
                return True
            return None

    pool = ReportPool()
    pool.conn = PlaybookConn(pool)
    _wire_worker(monkeypatch, pool, None)

    async def reason():
        return "Today's AI budget ($1.00) is used up."
    ran = []

    async def run_task(task):
        ran.append(task["id"])
    monkeypatch.setattr(usage, "background_pause_reason", reason)
    monkeypatch.setattr(playbooks, "run_playbook_task", run_task)
    assert await worker._process_one() is True
    assert ran == [8]
    assert not [a for s, a in pool.executed if "status='failed'" in s]


async def test_is_report_playbook_never_raises():
    class Broken:
        async def fetchval(self, *a):
            raise OSError("down")
    assert await reports.is_report_playbook(1, Broken()) is False


# ── scheduler: minute and zone ───────────────────────────────────────────────

UTC = timezone.utc


def test_cron_due_hour_only_keeps_its_meaning():
    now = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    assert expectations.cron_due({"type": "cron", "hour": 9}, now) == (
        True, "2026-11-04", "daily at 09:00")
    assert expectations.cron_due({"type": "cron", "hour": 9},
                                 now.replace(minute=59))[0] is True
    assert expectations.cron_due({"type": "cron", "hour": 10}, now)[0] is False


def test_cron_due_minute_and_zone():
    trig = {"type": "cron", "hour": 21, "minute": 0, "tz": "Asia/Kolkata"}
    at_2059 = datetime(2026, 11, 4, 15, 29, tzinfo=UTC)          # 20:59 IST
    at_2100 = datetime(2026, 11, 4, 15, 30, tzinfo=UTC)          # 21:00 IST
    assert expectations.cron_due(trig, at_2059)[0] is False
    assert expectations.cron_due(trig, at_2100) == (True, "2026-11-04",
                                                    "daily at 21:00 Asia/Kolkata")
    half = {"type": "cron", "hour": 21, "minute": 30, "tz": "Asia/Kolkata"}
    assert expectations.cron_due(half, at_2100)[0] is False
    assert expectations.cron_due(half, datetime(2026, 11, 4, 16, 0, tzinfo=UTC))[0] is True
    # the day is the trigger's own: 01:30 IST on the 5th is still the 4th in UTC
    early = {"type": "cron", "hour": 1, "minute": 30, "tz": "Asia/Kolkata"}
    assert expectations.cron_due(early, datetime(2026, 11, 4, 20, 0, tzinfo=UTC))[1] == "2026-11-05"
    # an unknown zone falls back to the operator clock
    assert expectations.cron_due({"type": "cron", "hour": 15, "minute": 30, "tz": "No/Where"},
                                 at_2100)[0] is True


@pytest.mark.parametrize("trig", [{"type": "cron"}, {"type": "cron", "hour": None},
                                  {"type": "cron", "hour": "x"}, {"type": "cron", "hour": 24},
                                  {"type": "cron", "hour": 9, "minute": 60}, None, [], "cron"])
def test_cron_due_raises_on_malformed(trig):
    with pytest.raises((TypeError, ValueError, KeyError, AttributeError)):
        expectations.cron_due(trig, datetime(2026, 11, 4, 9, 0, tzinfo=UTC))


def _wire_dispatch(monkeypatch, rows, now, state):
    saved = {}

    class P:
        async def fetch(self, sql, *a):
            return rows

    async def gp():
        return P()

    async def load():
        return dict(state)

    async def save(s):
        saved.update(s)
    queued = []

    async def once(pid):
        queued.append(pid)
        return 500 + pid, True
    st = SimpleNamespace(anthropic_api_key="k", openai_api_key="", consolidation_enabled=False,
                         consolidation_hour=3)
    monkeypatch.setattr(expectations, "get_settings", lambda: st)
    monkeypatch.setattr(expectations, "get_pool", gp)
    monkeypatch.setattr(expectations, "_load_cron_state", load)
    monkeypatch.setattr(expectations, "_save_cron_state", save)
    monkeypatch.setattr(expectations, "_local_now", lambda: now)
    monkeypatch.setattr(playbooks, "enqueue_playbook_run_once", once)
    return queued, saved


async def test_dispatch_fires_a_report_once_at_its_local_time(monkeypatch):
    rows = [{"id": 1, "name": "bad", "trigger": json.dumps({"type": "cron", "hour": "x"})},
            {"id": 2, "name": "Report: rolls", "trigger": json.dumps(
                {"type": "cron", "hour": 21, "minute": 0, "tz": "Asia/Kolkata"})},
            {"id": 3, "name": "daily_digest", "trigger": {"type": "cron", "hour": 9}}]
    now = datetime(2026, 11, 4, 15, 31, tzinfo=UTC)
    queued, saved = _wire_dispatch(monkeypatch, rows, now, {})
    assert await expectations._dispatch_cron() == 1
    assert queued == [2] and saved["2"] == "2026-11-04"
    queued, _ = _wire_dispatch(monkeypatch, rows, now, {"2": "2026-11-04"})
    assert await expectations._dispatch_cron() == 0 and queued == []
    queued, _ = _wire_dispatch(monkeypatch, rows, datetime(2026, 11, 4, 15, 29, tzinfo=UTC), {})
    assert await expectations._dispatch_cron() == 0 and queued == []


# ── routing, registry, labels, schema ────────────────────────────────────────

def test_registry_superset_and_label():
    assert graph._tool_registry()["propose_scheduled_report"] is \
        reporttools.propose_scheduled_report
    sup = graph.AGENT_TOOL_SUPERSET
    assert "propose_scheduled_report" in sup["supervisor"]
    assert sup["supervisor"][-2:] == ["discover_schema", "propose_context_change"]
    for k, v in sup.items():
        if k != "supervisor":
            assert "propose_scheduled_report" not in v, k
    assert "propose_scheduled_report" in graph.DEFAULT_CONFIGS["supervisor"]["tools"]
    assert "propose_scheduled_report" not in playbooks._playbook_registry()
    assert runstatus.LABELS["propose_scheduled_report"] == \
        "Proposing the daily report for your approval"


def test_routing_rule_sends_timed_reports_to_the_tool(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch)
    assert p.count("SCHEDULED REPORTS (Quick 047)") == 1
    rule = p[p.index("SCHEDULED REPORTS (Quick 047)"):].split("\n")[0]
    assert "propose_scheduled_report in THIS turn" in rule
    assert "never propose_kpi or create_watch" in rule and "9 PM" in rule
    assert "this rule wins over WATCH REQUESTS and TRACK THIS" in rule
    assert p.index(graph.SUPERVISOR_RULES_HEADER) < p.index("SCHEDULED REPORTS") < \
        p.index("SQL ROUTING")
    tools = [t for t in graph.DEFAULT_CONFIGS["supervisor"]["tools"]
             if t != "propose_scheduled_report"]
    assert "SCHEDULED REPORTS" not in composed_supervisor_prompt(monkeypatch, tools=tools)
    assert "propose_scheduled_report" in graph.propose_kpi.description


async def test_watch_claim_guard_leaves_a_report_proposal_alone(monkeypatch):
    from app import worker

    class P:
        async def fetch(self, sql, *a):
            if "FROM tool_calls" in sql:
                return [{"tool": "propose_scheduled_report",
                         "result_digest": "proposed daily report (action #12)"}]
            return []

        async def fetchval(self, *a):
            return None

    async def gp():
        return P()
    monkeypatch.setattr(worker, "get_pool", gp)
    reply = "Done! I've set up a daily alert for 9 PM IST (action #12), awaiting approval."
    out = await worker._guard_false_watch(5, reply, "Can send todays rolls every day 9 pm ist")
    assert out == reply


def test_run_labels_show_minute_and_zone():
    assert runsapi.playbook_trigger({"type": "cron", "hour": 2}) == "schedule (daily 02:00)"
    assert runsapi.playbook_trigger(json.dumps(
        {"type": "cron", "hour": 21, "minute": 5, "tz": "Asia/Kolkata"})) == \
        "schedule (daily 21:05 Asia/Kolkata)"
    assert runsapi.playbook_trigger({"type": "cron", "hour": "x"}) == "schedule"


def test_schema_adds_the_report_column_idempotently():
    schema = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
        encoding="utf-8").replace("\r\n", "\n")
    start = schema.index("-- ── Quick 047: scheduled WhatsApp reports ──")
    block = schema[start:schema.index("-- End Quick 047: scheduled WhatsApp reports")]
    stmts = [ln for ln in block.split("\n") if ln and not ln.startswith("--")]
    assert stmts == ["ALTER TABLE playbooks ADD COLUMN IF NOT EXISTS report JSONB;"]


# ── the owner's case: 9 PM IST, owner in Toronto, business on IST ────────────

async def test_9pm_ist_runs_on_the_operator_zone_not_utc_or_the_sender(env, pool, who,
                                                                        monkeypatch):
    """"Every day 9 pm ist" on an Asia/Kolkata install: scheduled at 21:00 Asia/Kolkata
    and reporting that IST business day, not UTC's day and not the Toronto owner's."""
    out = await reports.propose("Rolls received today", "21:00", [dict(SQL_ITEM)], "",
                                thread_id="919800000001", run_id=7)
    assert "21:00 Asia/Kolkata" in out
    p = pool.actions[0]["payload"]
    assert (p["hour"], p["minute"], p["timezone"]) == (21, 0, "Asia/Kolkata")
    assert await reports.create_from_proposal(p) is True
    trig = json.loads(pool.playbooks[0]["trigger"])
    assert trig == {"type": "cron", "hour": 21, "minute": 0, "tz": "Asia/Kolkata"}

    # schedule: 21:00 IST is 15:30 UTC; 21:00 UTC and 21:00 Toronto (EST) are not it
    assert expectations.cron_due(trig, datetime(2026, 11, 4, 15, 30, tzinfo=UTC)) == (
        True, "2026-11-04", "daily at 21:00 Asia/Kolkata")
    assert expectations.cron_due(trig, datetime(2026, 11, 4, 21, 0, tzinfo=UTC))[0] is False
    toronto_9pm = datetime(2026, 11, 4, 21, 0, tzinfo=ZoneInfo("America/Toronto"))
    assert expectations.cron_due(trig, toronto_9pm)[0] is False

    # window: the 21:00 IST run reports 2026-11-04 IST, header on the IST clock
    env["sql"].clear()
    await reports.run({"id": 90}, pool.playbooks[0])
    sql = env["sql"][0][0]
    assert "DATE '2026-11-04'" in sql and "DATE '2026-11-05'" in sql
    assert env["sends"][-1][1].splitlines()[1] == "Wed 04 Nov 2026, 21:00 IST"

    # where the days differ (00:30 IST on the 5th is 19:00 UTC and 14:00 Toronto on
    # the 4th), "today" is the IST day, even for a send time given in Toronto's zone
    monkeypatch.setattr(reports, "_utcnow", lambda: datetime(2026, 11, 4, 19, 0, tzinfo=UTC))
    toronto = reports.validate_spec("Rolls Toronto", "14:00", "America/Toronto",
                                    [dict(SQL_ITEM)])[0]
    assert reports.trigger_for(toronto)["tz"] == "America/Toronto"
    env["sql"].clear()
    msg, _ = await reports.render(toronto, reports.business_now())
    assert "DATE '2026-11-05'" in env["sql"][0][0] and "DATE '2026-11-06'" in env["sql"][0][0]
    assert msg.splitlines()[1] == "Thu 05 Nov 2026, 00:30 IST"


def test_business_now_follows_the_operator_setting(monkeypatch):
    from app import kpihistory
    monkeypatch.setattr(reports, "_utcnow", lambda: datetime(2026, 11, 4, 19, 0, tzinfo=UTC))
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    assert reports.business_now().isoformat() == "2026-11-05T00:30:00+05:30"
    monkeypatch.setattr(kpihistory, "_tz", lambda: "UTC")
    assert reports.business_now().date() == date(2026, 11, 4)
