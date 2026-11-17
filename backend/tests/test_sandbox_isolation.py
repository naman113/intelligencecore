"""L1 sandbox isolation — E2E test traffic must never touch real cases.

The incident: an E2E scenario wrote fictional payments onto a REAL case and
the assistant reported them authoritatively. The fix is structural, not
prompt-level: threads listed in the `test_threads` setting are quarantined —
cases born from them carry cases.sandbox = TRUE, and every consumer is scoped
in SQL so test events can never attach to real cases AND real events can
never attach to sandbox cases: triage's existing-case resolution and both
case INSERTs, the triage open-case index, the reactive context case block,
the expectations violation sweep, and the cases list API. The by-id detail
endpoint deliberately stays unscoped (test_case_endpoints pins that) so E2E
can still inspect its own sandbox cases.

Fakes only; no DB/LLM/network. Patterns follow test_case_convergence.py
(recording conns) and test_group_questions.py (pool doubles)."""

import inspect
import re
import sys
import types
from types import SimpleNamespace

import pytest

import app.main as main
from app import contextassembler, expectations, triage


def _settings(monkeypatch, **kw):
    """Patch every module-level get_settings name the helpers may read through
    (triage's import, the config module itself, contextassembler's import), so
    the pins hold wherever the integrator ends up defining _test_threads /
    _is_test_thread — the contract is behavioral, not a home address."""
    import app.config as config
    ns = SimpleNamespace(**kw)
    monkeypatch.setattr(triage, "get_settings", lambda: ns)
    monkeypatch.setattr(config, "get_settings", lambda: ns)
    monkeypatch.setattr(contextassembler, "get_settings", lambda: ns, raising=False)
    return ns


# ── the setting itself ─────────────────────────────────────────────────────

def test_test_threads_setting_exists_with_default():
    from app.config import Settings
    assert Settings.model_fields["test_threads"].default == "test-local"


def test_test_threads_is_dashboard_editable_and_labeled_quarantined():
    from app.configservice import REGISTRY
    entry = REGISTRY["test_threads"]
    assert entry["group"] == "tuning"
    label = str(entry.get("label", "")).lower()
    assert "sandbox" in label or "quarantin" in label, \
        "the operator must be told these threads' cases are quarantined"


# ── the parser helpers ─────────────────────────────────────────────────────

def test_is_test_thread_parses_csv_and_is_none_safe(monkeypatch):
    _settings(monkeypatch, test_threads="test-local, 999@g.us")
    assert triage._test_threads() == {"test-local", "999@g.us"}
    assert triage._is_test_thread("test-local")
    assert triage._is_test_thread("999@g.us")
    assert not triage._is_test_thread("123@g.us")
    assert not triage._is_test_thread(None), "db events have no thread_ref"


def test_no_test_threads_when_setting_blank(monkeypatch):
    _settings(monkeypatch, test_threads="")
    assert triage._test_threads() == set()
    assert not triage._is_test_thread("test-local")


# ── _apply_verdict: attach + open are sandbox-scoped in SQL ────────────────

class SandboxConn:
    """NoteConn-style recorder with scripted existing-case resolution."""

    def __init__(self, resolve_to=None):
        self.resolve_to = resolve_to
        self.fetchvals = []
        self.executed = []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "SELECT id FROM cases WHERE id" in sql:
            return self.resolve_to
        if "INSERT INTO cases" in sql:
            return 55
        if "INSERT INTO events" in sql:
            return 777
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _ev(id=1, source="whatsapp", thread_ref="123@g.us", **kw):
    e = {"id": id, "source": source, "content": "test payment of 5 lakh recorded",
         "actor_id": None, "actor_raw": "919876", "actor_name": None,
         "thread_ref": thread_ref}
    e.update(kw)
    return e


def _verdict(case, **extra):
    v = {"relevant": True, "case": case, "participants": [],
         "expectations_created": [], "expectations_satisfied": [],
         "expectations_updated": [], "case_note": None, "reason": "r"}
    v.update(extra)
    return v


def _resolutions(conn):
    return [(s, a) for s, a in conn.fetchvals if "SELECT id FROM cases WHERE id" in s]


def _case_inserts(conn):
    return [(s, a) for s, a in conn.fetchvals if "INSERT INTO cases" in s]


@pytest.mark.parametrize("case_sel", [19, "19"])
async def test_existing_case_resolution_is_sandbox_scoped(monkeypatch, case_sel):
    """BOTH resolution sites (int verdict and digit-string verdict) must pass
    is_test into the SQL — the model cannot reach across the boundary."""
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn(resolve_to=19)
    assert await triage._apply_verdict(conn, _ev(), _verdict(case_sel)) == 19
    (sql, args), = _resolutions(conn)
    assert "sandbox = $" in sql and "status = 'open'" in sql
    assert args == (19, False), "a real-thread event resolves REAL cases only"


@pytest.mark.parametrize("case_sel", [19, "19"])
async def test_test_thread_resolution_sees_sandbox_cases_only(monkeypatch, case_sel):
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn(resolve_to=19)
    await triage._apply_verdict(conn, _ev(thread_ref="test-local"),
                                _verdict(case_sel))
    (sql, args), = _resolutions(conn)
    assert args == (19, True), "a test-thread event resolves SANDBOX cases only"


async def test_test_event_cannot_land_on_a_real_case(monkeypatch):
    """The verdict points at a real case; the sandbox-scoped resolution misses
    (None) and the fallback opens a NEW case instead — itself sandboxed. This
    is the exact incident shape: fictional payment aimed at a live case."""
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn(resolve_to=None)
    cid = await triage._apply_verdict(conn, _ev(thread_ref="test-local"),
                                      _verdict(19))
    assert cid == 55, "fell through to a fresh case, never the real one"
    (sql, args), = _case_inserts(conn)
    assert "sandbox" in sql, "the fallback INSERT must set the sandbox column"
    assert True in args


async def test_new_case_insert_marks_sandbox_flag(monkeypatch):
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn()
    await triage._apply_verdict(conn, _ev(thread_ref="test-local"),
                                _verdict("new", new_case_title="Test case"))
    (sql, args), = _case_inserts(conn)
    assert "sandbox" in sql and True in args


async def test_real_thread_new_case_is_not_sandboxed(monkeypatch):
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn()
    await triage._apply_verdict(conn, _ev(thread_ref="123@g.us"),
                                _verdict("new", new_case_title="Real case"))
    (sql, args), = _case_inserts(conn)
    assert "sandbox" in sql and False in args and True not in args


# ── step 6: expectations_satisfied is sandbox-scoped in SQL ────────────────
# The incident, moved one table over: a test-thread event routing a fictional
# "payment received" onto a REAL expectation would silence its reminder, and
# the auto-complete sweep would then close the real case and notify the owner.


@pytest.mark.parametrize("thread_ref,is_test", [
    ("123@g.us", False),
    ("test-local", True),
])
async def test_expectations_satisfied_update_is_sandbox_scoped(
        monkeypatch, thread_ref, is_test):
    _settings(monkeypatch, test_threads="test-local")
    conn = SandboxConn(resolve_to=19)
    v = _verdict(19)
    v["expectations_satisfied"] = [7]
    await triage._apply_verdict(conn, _ev(thread_ref=thread_ref), v)
    sats = [(s, a) for s, a in conn.executed
            if "SET status = 'satisfied'" in s]
    (sql, args), = sats
    assert "SELECT id FROM cases WHERE sandbox = $" in sql, \
        "the satisfy UPDATE must be scoped through the cases table"
    assert "status = 'open'" in sql
    assert args[0] == 7 and args[-1] is is_test, \
        "a test event satisfies sandbox expectations only, and vice versa"


# ── triage open-expectations index ─────────────────────────────────────────


async def test_open_expectations_index_excludes_sandbox():
    class Rec:
        def __init__(self):
            self.sql = None

        async def fetch(self, sql, *args):
            self.sql = sql
            return []

    r = Rec()
    await triage._open_expectations_index(r)
    assert re.search(r"JOIN\s+cases\s+c\s+ON\s+c\.id\s*=\s*e\.case_id", r.sql), \
        "the index must join cases to see the sandbox flag"
    assert "NOT c.sandbox" in r.sql, \
        "fictional expectations must not contaminate the triage prompt (nor " \
        "evict real ones from the LIMIT window)"


# ── satisfaction sweep ─────────────────────────────────────────────────────


async def test_satisfaction_sweep_excludes_sandbox_expectations(monkeypatch):
    """The assistant's real deliveries must never satisfy a test fixture's
    expectations (which would rush a sandbox case to auto-completion)."""
    sqls = []

    class Conn:
        async def fetch(self, sql, *args):
            sqls.append(sql)
            if "provenance = 'self'" in sql:
                return [{"id": 1, "content": "here is the data"}]
            return []

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

        async def execute(self, sql, *args):
            sqls.append(sql)

    async def fake_pool():
        return Pool()
    monkeypatch.setattr(triage, "get_pool", fake_pool)

    await triage._satisfaction_sweep()
    exp_sql, = [s for s in sqls if "FROM expectations e" in s]
    assert "NOT c.sandbox" in exp_sql


# ── close path: sandbox cases close silently ───────────────────────────────
# A sandbox case still CLOSES (E2E scenarios need the lifecycle), but the
# side effects stay inside the quarantine: no real WhatsApp "Done" to the
# owner about a fictional case, no distillation of fictional content into
# the shared knowledge graph.


class ClosePool:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    async def fetchval(self, sql, *args):
        if "UPDATE cases SET status = 'closed'" in sql:
            return args[0]
        if "SELECT sandbox" in sql:
            return self.sandbox
        return None


def _wire_close(monkeypatch):
    monkeypatch.setattr(
        triage, "get_settings",
        lambda: SimpleNamespace(case_chaining_enabled=False))
    notified, distilled = [], []

    async def fake_notify(case_id, kind="completed"):
        notified.append(case_id)
        return True

    async def fake_distill(case_id):
        distilled.append(case_id)

    import app.actionsvc
    import app.memory_pipeline
    monkeypatch.setattr(app.actionsvc, "notify_case_closed", fake_notify)
    monkeypatch.setattr(app.memory_pipeline, "distill_case", fake_distill)
    return notified, distilled


async def test_sandbox_case_close_suppresses_notify_and_distill(monkeypatch):
    notified, distilled = _wire_close(monkeypatch)
    assert await triage._close_case(ClosePool(True), 5, "done") is True, \
        "the sandbox case still closes"
    assert notified == [] and distilled == [], \
        "no owner WhatsApp and no memory distillation for a fictional case"


async def test_real_case_close_keeps_notify_and_distill(monkeypatch):
    notified, distilled = _wire_close(monkeypatch)
    assert await triage._close_case(ClosePool(False), 5, "done") is True
    assert notified == [5] and distilled == [5]


# ── investigation: event search skips test threads ─────────────────────────


async def test_investigation_event_search_excludes_test_threads(monkeypatch):
    """Test-thread events are provenance='external' like any chat message —
    without the filter a real user's question surfaces fictional E2E messages
    as sourced 'history hits' written back onto a REAL case."""
    from app import investigation
    _settings(monkeypatch, test_threads="test-local")
    pool = RecPool()

    async def fake_pool():
        return pool
    monkeypatch.setattr(investigation, "get_pool", fake_pool)

    assert await investigation._search_events("pending on TC-006") == []
    assert len(pool.queries) == 2, "FTS miss falls back to ILIKE"
    for sql, args in pool.queries:
        assert "<> ALL($3" in sql, "both queries must exclude test threads"
        assert "thread_ref IS NULL" in sql, \
            "db/email events without a thread_ref must stay searchable"
        assert ["test-local"] in args


# ── triage open-case index ─────────────────────────────────────────────────

async def test_triage_open_case_index_excludes_sandbox():
    class Rec:
        def __init__(self):
            self.sql = None

        async def fetch(self, sql, *args):
            self.sql = sql
            return []

    r = Rec()
    await triage._open_cases_index(r)
    assert "NOT c.sandbox" in r.sql, \
        "sandbox cases must be invisible to the triage model's case index"
    assert "c.status = 'open'" in r.sql


# ── expectations violation sweep ───────────────────────────────────────────

async def test_violation_sweep_excludes_sandbox_cases(monkeypatch):
    """A sandbox case's expectations must never fire real playbook runs — the
    sweep joins cases and filters the flag in SQL."""
    stub = types.ModuleType("app.playbooks")

    async def _never(*a, **k):
        raise AssertionError("no due rows -> nothing may be enqueued")
    stub.enqueue_playbook_run = _never
    monkeypatch.setitem(sys.modules, "app.playbooks", stub)

    sqls = []

    class Conn:
        async def fetch(self, sql, *args):
            sqls.append(sql)
            return []

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    async def fake_pool():
        return Pool()
    monkeypatch.setattr(expectations, "get_pool", fake_pool)

    assert await expectations._sweep_violations() == 0
    sweep, = sqls
    assert re.search(r"JOIN\s+cases\s+c\s+ON\s+c\.id\s*=\s*e\.case_id", sweep), \
        "the sweep must join cases to see the sandbox flag"
    assert "NOT c.sandbox" in sweep
    assert re.search(r"e\.status\s*=\s*'open'", sweep)
    assert "due_at" in sweep and "now()" in sweep


# ── reactive context: _open_cases scopes on the requesting thread ──────────

class RecPool:
    def __init__(self):
        self.queries = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return []


def _wire_ctx(monkeypatch, pool):
    import app.db as db

    async def fake_pool():
        return pool
    monkeypatch.setattr(db, "get_pool", fake_pool)


async def test_reactive_case_block_scopes_on_sandbox(monkeypatch):
    """Signature unchanged — is_test is computed from the thread id and the
    same setting, and lands in the SQL as the sandbox parameter."""
    _settings(monkeypatch, test_threads="test-local")
    pool = RecPool()
    _wire_ctx(monkeypatch, pool)

    await contextassembler._open_cases("pending on TC-006?", "919000000101")
    (sql, args), = [q for q in pool.queries if "FROM cases c" in q[0]]
    assert "sandbox = $" in sql
    assert False in args and True not in args, \
        "a real thread's context sees real cases only"

    pool.queries.clear()
    await contextassembler._open_cases("pending on TC-006?", "test-local")
    (sql2, args2), = [q for q in pool.queries if "FROM cases c" in q[0]]
    assert True in args2, "a test thread's context sees sandbox cases only"


# ── ops API: list is scoped, default real ──────────────────────────────────

def test_api_list_cases_sandbox_param_defaults_false():
    p = inspect.signature(main.api_list_cases).parameters
    assert "sandbox" in p and p["sandbox"].default is False


async def test_api_list_cases_always_filters_sandbox(monkeypatch):
    pool = RecPool()

    async def fake_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_pool))

    assert await main.api_list_cases() == []
    (sql, args), = pool.queries
    assert "sandbox = $" in sql, "the sandbox clause must ALWAYS be present"
    assert False in args and True not in args

    pool.queries.clear()
    await main.api_list_cases(status="open", sandbox=True)
    (sql2, args2), = pool.queries
    assert "sandbox = $" in sql2 and "c.status = $" in sql2, \
        "status filter and sandbox scope must compose"
    assert True in args2 and "open" in args2


async def test_playbook_case_overview_excludes_sandbox(monkeypatch):
    """The scheduled-digest tool must never report fictional test cases to the
    owner — sandbox rows sort to the TOP of the digest otherwise (their E2E
    expectations are deliberately violated, and the query orders by
    violated_exp DESC)."""
    from app import playbook_tools

    pool = RecPool()

    async def fake_pool():
        return pool
    monkeypatch.setattr(playbook_tools, "get_pool", fake_pool)

    async def noop_log(*a, **k):
        pass
    monkeypatch.setattr(playbook_tools, "_log_tool", noop_log)

    fn = playbook_tools.case_overview.coroutine or playbook_tools.case_overview.func
    await fn()
    (sql, _), = pool.queries
    assert "NOT c.sandbox" in sql, "digest query must exclude sandbox cases"
