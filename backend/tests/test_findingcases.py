"""Quick 006: cases from findings, pure logic (no DB, no LLM).

The real-Postgres lifecycle (fire, repeat, investigation, recovery close, insights,
open_case, stopped watches) is in test_findingcases_db.py. These pin the rules that
must hold without a database: which fires open a case, what counts as back to
normal, where a close notice goes, that triage never sees finding cases and never
chains them, and the open_case tool's gates.
"""

import types

import pytest

from app import findingcases, sentinel, triage


# ── rules ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trigger,current,want", [
    ({"type": "threshold", "column": "n", "op": ">", "value": 10}, {"n": 11}, True),
    ({"type": "threshold", "column": "n", "op": ">", "value": 10}, {"n": 10}, False),
    ({"type": "threshold", "column": "n", "op": "<=", "value": 5}, {"n": "4"}, True),
    ({"type": "threshold", "column": "n", "op": "<", "value": 5}, {"n": 9}, False),
    ({"type": "threshold", "column": "n", "op": ">", "value": 10}, {"m": 99}, None),
    ({"type": "threshold", "column": "n", "op": ">", "value": 10}, {"n": "abc"}, None),
    ({"type": "threshold", "column": "n", "op": "!=", "value": 10}, {"n": 1}, None),
    ({"type": "increase", "column": "n"}, {"n": 3}, None),
    (None, {"n": 3}, None),
])
def test_in_alert(trigger, current, want):
    assert findingcases.in_alert(trigger, current) is want


def test_only_critical_fires_open_cases():
    """Critical is the KPI threshold rule: the one severity with a real normal state."""
    kpi = {"kind": "kpi"}
    assert sentinel._severity_for(kpi, {"type": "threshold"}) == findingcases.CASE_SEVERITY
    assert sentinel._severity_for(kpi, {"type": "increase"}) != findingcases.CASE_SEVERITY
    assert sentinel._severity_for({"kind": "user"}, {"type": "threshold"}) != "critical"


async def test_non_critical_fire_touches_nothing():
    class NoPool:
        def acquire(self):
            raise AssertionError("a warning fire must not open a case")
    out = await findingcases.on_watch_fire(NoPool(), {"id": 1, "title": "t"}, severity="warning",
                                           summary="s", current={}, trigger={})
    assert out == (None, False)


@pytest.mark.parametrize("raw,want", [(3, 3), (0, 1), (99, 50), ("x", 3), (None, 3)])
def test_recovery_checks_is_clamped(monkeypatch, raw, want):
    monkeypatch.setattr(findingcases, "get_settings",
                        lambda: types.SimpleNamespace(watch_case_recovery_checks=raw))
    assert findingcases.recovery_checks() == want


def test_recovery_setting_is_registered_live_with_a_range():
    from app import configservice
    from app.config import Settings
    assert Settings.model_fields["watch_case_recovery_checks"].default == 3
    entry = configservice.REGISTRY["watch_case_recovery_checks"]
    assert entry["group"] == "tuning" and entry["min"] == 1 and entry["max"] == 50
    assert "live" in entry["label"]
    configservice._validate_value("watch_case_recovery_checks", "5")
    with pytest.raises(ValueError):
        configservice._validate_value("watch_case_recovery_checks", "0")
    with pytest.raises(ValueError):
        configservice._validate_value("watch_case_recovery_checks", "51")


def test_refs_dedupe_on_normalised_titles():
    assert findingcases.watch_ref(12) == "watch:12"
    assert findingcases.insight_ref("South orders -30%!") == findingcases.insight_ref("south ORDERS 30")
    assert findingcases.chat_ref("A  b") == "chat:a b"
    assert len(findingcases.insight_ref("x" * 500)) == findingcases.REF_CAP


# ── close notices ───────────────────────────────────────────────────────────

def test_notice_wording_per_ending():
    case = {"id": 4, "title": "KPI alert: Revenue", "summary": "Fired twice."}
    assert findingcases.notice_text(case, "resolved").startswith("✅ Back to normal: “KPI alert: Revenue”")
    assert "Fired twice." in findingcases.notice_text(case, "resolved")
    assert "watch is no longer running" in findingcases.notice_text(case, "watch_off")
    assert "archived" in findingcases.notice_text(case, "stale")
    # The owner closed it themselves: nothing to tell them.
    assert findingcases.notice_text(case, "completed") is None


def test_notice_goes_where_the_finding_went(monkeypatch):
    s = types.SimpleNamespace(owner_number="919800000000", wa_api_base="")
    monkeypatch.setattr(findingcases, "get_settings", lambda: s)
    assert findingcases.notice_thread({"origin_chat_jid": "ui:owner:abcdefghij"}) == "ui:owner:abcdefghij"
    # No WhatsApp connector: the in-app chat, never a number nobody reads.
    assert findingcases.notice_thread({"origin_chat_jid": None}) == "ui:owner"
    s.wa_api_base = "http://wa"
    assert findingcases.notice_thread({}) == "919800000000"


# ── triage stays as it was ──────────────────────────────────────────────────

class _Rec:
    def __init__(self, ret=None):
        self.sqls, self.ret = [], ret

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        return self.ret or []


async def test_triage_index_leaves_finding_cases_out():
    conn = _Rec()
    await triage._open_cases_index(conn)
    sql = conn.sqls[0]
    assert "c.origin NOT IN ('watch', 'insight', 'chat', 'exploration')" in sql
    assert "c.origin IS NULL" in sql    # every message case (origin NULL) still listed


async def test_stale_sweep_never_closes_a_watch_case_or_the_insights_log(monkeypatch):
    pool = _Rec()

    async def gp():
        return pool
    monkeypatch.setattr(triage, "get_pool", gp)
    monkeypatch.setattr(triage, "get_settings", lambda: types.SimpleNamespace(stale_case_days=21))
    await triage._close_stale_cases()
    sql = pool.sqls[0]
    assert "c.origin NOT IN ('watch', 'exploration')" in sql and "c.origin IS NULL" in sql


class _ClosePool:
    """Just enough of the pool for triage._close_case."""

    def __init__(self, origin):
        self.origin, self.sqls = origin, []

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if "UPDATE cases SET status = 'closed'" in sql:
            return args[0]
        if "SELECT sandbox" in sql:
            return False
        if "SELECT origin" in sql:
            return self.origin
        return None


@pytest.mark.parametrize("origin,chains", [(None, True), ("exploration", True),
                                           ("watch", False), ("insight", False), ("chat", False)])
async def test_a_finding_case_never_chains(monkeypatch, origin, chains):
    calls = {"notify": [], "distill": [], "chain": []}

    async def notify(cid, kind="completed"):
        calls["notify"].append((cid, kind))

    async def distill(cid):
        calls["distill"].append(cid)

    async def chain(pool, cid):
        calls["chain"].append(cid)
    import app.actionsvc as actionsvc
    import app.memory_pipeline as memory_pipeline
    monkeypatch.setattr(actionsvc, "notify_case_closed", notify)
    monkeypatch.setattr(memory_pipeline, "distill_case", distill)
    monkeypatch.setattr(triage, "_maybe_chain_case", chain)
    assert await triage._close_case(_ClosePool(origin), 9, "closed by the owner") is True
    assert calls["notify"] == [(9, "completed")] and calls["distill"] == [9]
    assert calls["chain"] == ([9] if chains else [])


# ── sentinel wiring ─────────────────────────────────────────────────────────

class _FirePool:
    def __init__(self):
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return 501 if "INSERT INTO watch_events" in sql else None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))


@pytest.fixture
def fire_env(monkeypatch):
    state = {"delivered": [], "escalated": [], "fires": []}
    pool = _FirePool()

    async def gp():
        return pool

    async def deliver(thread, msg):
        state["delivered"].append((thread, msg))

    async def escalate(watch, judgement, current=None, case_id=None):
        state["escalated"].append((judgement["severity"], case_id))
        return judgement["severity"] == "critical"

    async def on_fire(p, watch, **kw):
        state["fires"].append(kw)
        return (77, len(state["fires"]) == 1) if kw["severity"] == "critical" else (None, False)
    monkeypatch.setattr(sentinel, "get_pool", gp)
    monkeypatch.setattr(sentinel, "_deliver", deliver)
    monkeypatch.setattr(sentinel, "_maybe_escalate", escalate)
    monkeypatch.setattr(findingcases, "on_watch_fire", on_fire)
    state["pool"] = pool
    return state


WATCH = {"id": 5, "title": "Daily revenue", "thread_id": "ui:owner", "kind": "kpi", "one_shot": False}
THRESH = {"type": "threshold", "column": "revenue", "op": "<", "value": 50000}


async def test_critical_fire_names_its_case_and_escalates_into_it(fire_env):
    await sentinel._fire(dict(WATCH), {"revenue": 41000}, THRESH, "")
    (thread, msg), = fire_env["delivered"]
    assert thread == "ui:owner" and "Opened case #77" in msg
    assert fire_env["escalated"] == [("critical", 77)]
    assert fire_env["fires"][0]["watch_event_id"] == 501
    sqls = [c[1] for c in fire_env["pool"].calls]
    assert any("UPDATE watch_events SET escalated = true" in s for s in sqls)
    # Second fire: same case, says so.
    await sentinel._fire(dict(WATCH), {"revenue": 40000}, THRESH, "")
    assert "Added to the open case #77" in fire_env["delivered"][1][1]


async def test_warning_fire_has_no_case(fire_env):
    await sentinel._fire(dict(WATCH), {"revenue": 3}, {"type": "increase", "column": "revenue"}, "")
    (thread, msg), = fire_env["delivered"]
    assert "case #" not in msg
    assert fire_env["escalated"] == [("warning", None)]


async def test_a_broken_case_layer_never_stops_the_alert(fire_env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("cases table missing")
    monkeypatch.setattr(findingcases, "on_watch_fire", boom)
    await sentinel._fire(dict(WATCH), {"revenue": 41000}, THRESH, "")
    assert len(fire_env["delivered"]) == 1
    assert fire_env["escalated"] == [("critical", None)]


async def test_escalation_findings_become_a_note_on_the_case(monkeypatch):
    notes = []

    async def run(history, message, context_text="", **kw):
        from app.playbook_tools import ctx_case_id
        notes.append(("ctx", ctx_case_id.get()))
        return "South region had no orders on Tuesday."

    async def deliver(thread_id, msg):
        return True

    async def record(pool, case_id, watch, findings):
        notes.append(("note", case_id, findings))

    async def gp():
        return object()
    from app.agents import graph
    monkeypatch.setattr(graph, "run", run)
    monkeypatch.setattr(sentinel.appchannel, "deliver", deliver)
    monkeypatch.setattr(sentinel, "_escalation_times", [])
    monkeypatch.setattr(sentinel, "get_pool", gp)
    monkeypatch.setattr(sentinel, "get_settings", lambda: types.SimpleNamespace(playbook_token_budget=1))
    monkeypatch.setattr(findingcases, "record_investigation", record)
    ok = await sentinel._maybe_escalate(dict(WATCH, compiled_sql="SELECT 1"),
                                        {"severity": "critical", "summary": "s"}, {}, case_id=77)
    assert ok is True
    assert notes == [("ctx", 77), ("note", 77, "South region had no orders on Tuesday.")]
    from app.playbook_tools import ctx_case_id
    assert ctx_case_id.get() is None   # reset after the run


# ── open_case tool ──────────────────────────────────────────────────────────

def test_open_case_is_a_supervisor_tool_only():
    from app.agents import graph
    assert "open_case" in graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert graph.AGENT_TOOL_SUPERSET["supervisor"][-2:] == ["discover_schema", "propose_context_change"]
    for key, tools in graph.AGENT_TOOL_SUPERSET.items():
        if key != "supervisor":
            assert "open_case" not in tools
    assert "open_case" in graph._tool_registry()
    from app import playbook_tools
    assert "open_case" not in playbook_tools.PLAYBOOK_TOOL_REGISTRY
    for tier, caps in playbook_tools.TIER_CAPABILITIES.items():
        assert "open_case" not in caps, tier


async def _call_open_case(monkeypatch, **ctx):
    from app import casetools
    from app.agents.graph import ctx_phone
    from app.playbook_tools import ctx_case_id, ctx_tier
    opened = []

    async def open_chat_case(**kw):
        opened.append(kw)
        return 31, True, 900

    async def log_tool(*a, **k):
        return None
    monkeypatch.setattr(casetools.findingcases, "open_chat_case", open_chat_case)
    monkeypatch.setattr(casetools, "_log_tool", log_tool)
    tokens = [ctx_tier.set(ctx.get("tier")), ctx_case_id.set(ctx.get("case_id")),
              ctx_phone.set(ctx.get("thread", "ui:owner"))]
    try:
        out = await casetools.open_case.ainvoke({
            "title": ctx.get("title", "Tuesday orders 30 percent low"),
            "finding": "21 orders against about 30 expected.",
            "evidence_sql": ctx.get("sql", "")})
    finally:
        ctx_phone.reset(tokens[2])
        ctx_case_id.reset(tokens[1])
        ctx_tier.reset(tokens[0])
    return out, opened


async def test_open_case_in_chat_opens_one(monkeypatch):
    async def links(*a):
        return {}, []
    from app import casetools
    monkeypatch.setattr(casetools, "_links", links)
    out, opened = await _call_open_case(monkeypatch, sql="SELECT count(*) FROM sales_orders")
    assert out.startswith("case #31 opened")
    assert opened[0]["thread_id"] == "ui:owner"
    assert opened[0]["evidence_sql"] == "SELECT count(*) FROM sales_orders"


@pytest.mark.parametrize("ctx,want", [
    ({"tier": "draft"}, "TIER REFUSED"),
    ({"tier": "autonomous_message"}, "TIER REFUSED"),
    ({"case_id": 77}, "REFUSED: this run already belongs to case #77"),
    ({"thread": None}, "REFUSED: no conversation"),
    ({"title": ""}, "REFUSED: title"),
    ({"title": "!!!"}, "REFUSED: title needs letters"),
    ({"sql": "DELETE FROM sales_orders"}, "REFUSED: evidence_sql"),
])
async def test_open_case_refusals(monkeypatch, ctx, want):
    out, opened = await _call_open_case(monkeypatch, **ctx)
    assert out.startswith(want), out
    assert opened == []


def test_skills_mention_open_case():
    from pathlib import Path
    lib = Path(__file__).resolve().parents[2] / "skills_library" / "intelligence-corner"
    for slug in ("anomaly-detection", "statistical-tests"):
        text = (lib / slug / "SKILL.md").read_text(encoding="utf-8")
        assert "open_case" in text and "unasked" in text and "Tracking a finding" in text


def test_schema_block_is_additive_and_guarded():
    from pathlib import Path
    s = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    a, b = s.index("-- ── Quick 006: cases from findings ──"), s.index("-- End Quick 006: cases from findings")
    block = s[a:b]
    for stmt in ("ALTER TABLE cases ADD COLUMN IF NOT EXISTS origin TEXT;",
                 "ADD COLUMN IF NOT EXISTS watch_id BIGINT REFERENCES watches(id) ON DELETE SET NULL;",
                 "CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_open_origin_ref",
                 "ALTER TABLE watches ADD COLUMN IF NOT EXISTS normal_streak INTEGER NOT NULL DEFAULT 0;",
                 "ALTER TABLE watch_events ADD COLUMN IF NOT EXISTS case_id BIGINT;"):
        assert stmt in block, stmt
    assert "DROP " not in block and "ALTER COLUMN" not in block
    import hashlib
    import re
    lib = Path(__file__).resolve().parents[2] / "skills_library" / "intelligence-corner"
    for slug, tag in (("anomaly-detection", "$q006_anomaly_detection$"),
                      ("statistical-tests", "$q006_statistical_tests$")):
        body = (lib / slug / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        i = block.index(f"UPDATE skills SET body = {tag}")
        j = block.index(tag, i + len(f"UPDATE skills SET body = {tag}"))
        assert block[i + len(f"UPDATE skills SET body = {tag}"):j].strip() == body.strip()
        stmt = block[j:block.index(");", j) + 2]
        assert f"WHERE name = '{slug}'" in stmt and "enabled" not in stmt
        olds = re.findall(r"'([0-9a-f]{32})'", stmt)
        assert olds and hashlib.md5(body.strip(" \n\t").encode()).hexdigest() not in olds


# ── insights: plain-words fields and the evidence sample ───────────────────

def test_owner_fields_are_cleaned_not_refused():
    from app import insights
    out, dropped = insights.clean_owner_fields(
        "South orders fell.", "About 9,000 a month less revenue.", "Call the South rep.",
        ["South orders in Sep: 210", "no colon here", "Drop: 30 percent", "A: 1", "B: 2"])
    assert out["summary"] == "South orders fell." and out["next_step"] == "Call the South rep."
    assert out["key_figures"] == [{"label": "South orders in Sep", "value": "210"},
                                  {"label": "Drop", "value": "30 percent"},
                                  {"label": "A", "value": "1"}]
    assert any("no colon here" in d for d in dropped) and any("first 4" in d for d in dropped)
    out, dropped = insights.clean_owner_fields("x" * 301, "", "", None)
    assert out == {} and dropped and "summary" in dropped[0]
    assert insights.clean_owner_fields() == ({}, [])


def test_sample_text_renders_the_rows():
    from app import insights
    ev = {"row_count": 12, "columns": ["region", "orders"],
          "sample": [{"region": "South", "orders": 210}, {"region": "North", "orders": 300}]}
    t = insights.sample_text(ev)
    assert t.splitlines() == ["Sample (2 of 12 rows):", "region=South, orders=210",
                              "region=North, orders=300"]
    assert insights.sample_text(None) == "" and insights.sample_text({"sample": []}) == ""


async def test_record_insight_passes_owner_fields_and_same_subject(monkeypatch):
    from app import insights, playbook_tools
    seen = {}

    async def check(sql):
        return {"row_count": 2, "columns": ["n"], "sample": [{"n": 1}]}, ""

    async def note(t, f, sql, sev, ev, **kw):
        seen.update(kw)
        return 5, 9, ""

    async def log_tool(*a, **k):
        return None
    monkeypatch.setattr(insights, "check_evidence", check)
    monkeypatch.setattr(insights, "record_note", note)
    monkeypatch.setattr(playbook_tools, "_log_tool", log_tool)
    tok = playbook_tools.ctx_tier.set("draft")
    try:
        out = await playbook_tools.record_insight.ainvoke({
            "title": "South orders fell 30 percent", "finding": "From 300 to 210. Caveat: one month.",
            "evidence_sql": "SELECT 1", "severity": "warning", "same_as_case": 9,
            "summary": "South orders fell.", "key_figures": ["Orders in Sep: 210", "bad"]})
    finally:
        playbook_tools.ctx_tier.reset(tok)
    assert out.startswith("recorded insight (note event #5) on case #9")
    assert "Not kept" in out
    assert seen["same_as_case"] == 9
    assert seen["extra"] == {"summary": "South orders fell.",
                             "key_figures": [{"label": "Orders in Sep", "value": "210"}]}
