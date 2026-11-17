"""B3 rolling thread summary + B1 wiring: worker delegates context assembly to
contextassembler.assemble (passing the resolved sender's name), _load_history
returns (summary, short tail), the summary is prepended as a synthetic leading
user message, and the summary refresh loop is threshold-driven and fail-soft.
Pure logic — fakes/monkeypatch only, no DB/LLM/network."""

from types import SimpleNamespace

import app.triage as triage_mod
from app import worker


# ── fakes ────────────────────────────────────────────────────────────────

class _FakeSummaryModel:
    def __init__(self, reply="rolled summary"):
        self.reply = reply
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.reply)


class SummaryPool:
    """Pool for the _bump_and_maybe_summarize path only."""

    def __init__(self, counter, rows, prev_summary=None):
        self.counter = counter          # value RETURNING'd by the bump UPDATE
        self.rows = rows                # newest-first message rows
        self.prev = prev_summary
        self.executed = []

    async def fetchval(self, sql, *args):
        if "UPDATE threads" in sql and "messages_since_summary" in sql:
            return self.counter
        if "SELECT summary FROM threads" in sql:
            return self.prev
        return None

    async def fetch(self, sql, *args):
        limit = args[1] if len(args) > 1 else len(self.rows)
        return self.rows[:limit]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _wire_summary(monkeypatch, pool, model):
    async def fake_get_pool():
        return pool

    monkeypatch.setattr(worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(triage_mod, "_model", lambda: model)


# ── B3: refresh threshold + reset ────────────────────────────────────────

async def test_summary_refresh_at_threshold_stores_and_resets(monkeypatch):
    rows = [{"role": "assistant", "content": "a" * 1000},
            {"role": "user", "content": "please do the thing"}]
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES, rows=rows,
                       prev_summary="old summary")
    model = _FakeSummaryModel(reply="  fresh rolling summary  ")
    _wire_summary(monkeypatch, pool, model)

    await worker._bump_and_maybe_summarize("t1")

    # one LLM call, fed the previous summary and a per-message-clipped transcript
    assert len(model.calls) == 1
    body = next(m[1] for m in model.calls[0] if m[0] == "user")
    assert "old summary" in body
    assert "please do the thing" in body
    assert "a" * worker.SUMMARY_MSG_CHARS in body
    assert "a" * (worker.SUMMARY_MSG_CHARS + 1) not in body  # 300-char clip
    assert len(body) <= worker.SUMMARY_TRANSCRIPT_CHARS
    # stored trimmed, counter reset in the same statement
    (sql, args), = pool.executed
    assert "summary = $2" in sql and "messages_since_summary = 0" in sql
    assert args == ("t1", "fresh rolling summary")


async def test_summary_not_refreshed_below_threshold(monkeypatch):
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES - 1,
                       rows=[{"role": "user", "content": "x"}])
    model = _FakeSummaryModel()
    _wire_summary(monkeypatch, pool, model)

    await worker._bump_and_maybe_summarize("t1")

    assert model.calls == []
    assert pool.executed == []      # counter bump is the RETURNING update


async def test_summary_capped_and_content_blocks_joined(monkeypatch):
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES,
                       rows=[{"role": "user", "content": "q"}])
    model = _FakeSummaryModel(reply=[{"type": "text", "text": "s" * 3000}])
    _wire_summary(monkeypatch, pool, model)

    await worker._bump_and_maybe_summarize("t1")

    (_, args), = pool.executed
    assert args[1] == "s" * worker.SUMMARY_MAX_CHARS


async def test_summary_refresh_failure_is_swallowed_and_counter_kept(monkeypatch):
    """Loop-resilience contract: a broken LLM must not raise out of the reply
    path, and the counter is NOT reset so the next reply retries."""
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES,
                       rows=[{"role": "user", "content": "q"}])

    def broken_model():
        raise RuntimeError("no api key")

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(triage_mod, "_model", broken_model)

    await worker._bump_and_maybe_summarize("t1")   # must not raise

    assert pool.executed == []                     # no store, no reset


async def test_empty_summary_backs_counter_off_instead_of_retrying_every_reply(monkeypatch):
    """A model that consistently returns empty output must not cost one LLM
    call per subsequent reply: the counter backs off to half the threshold."""
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES,
                       rows=[{"role": "user", "content": "q"}])
    model = _FakeSummaryModel(reply="   ")
    _wire_summary(monkeypatch, pool, model)

    await worker._bump_and_maybe_summarize("t1")

    (sql, args), = pool.executed
    assert "messages_since_summary" in sql
    assert "SET summary" not in sql              # no summary stored
    assert args == ("t1", worker.SUMMARY_REFRESH_MESSAGES // 2)


async def test_summary_skipped_when_no_messages(monkeypatch):
    pool = SummaryPool(counter=worker.SUMMARY_REFRESH_MESSAGES, rows=[])
    model = _FakeSummaryModel()
    _wire_summary(monkeypatch, pool, model)

    await worker._bump_and_maybe_summarize("t1")

    assert model.calls == [] and pool.executed == []


# ── B3: _load_history returns (summary, short tail) ──────────────────────

class _HistConn:
    def __init__(self, rows, summary=None):
        self.rows = rows
        self.summary = summary

    async def fetch(self, sql, *args):
        limit = args[1] if len(args) > 1 else len(self.rows)
        mid = args[2] if len(args) > 2 else None       # Quick 019 filter on $3
        rows = [r for r in self.rows if mid is None or "id" not in r
                or (r["id"] != mid and not (r["role"] == "user" and r["id"] > mid))]
        return rows[:limit]           # newest-first, as the DESC query returns

    async def fetchval(self, sql, *args):
        if "SELECT summary FROM threads" in sql:
            return self.summary
        return None


async def test_load_history_returns_summary_and_max_8_rows_with_caps():
    rows = [{"role": "user", "content": f"m{i}" + "y" * 2500} for i in range(12)]
    summary, hist = await worker._load_history(_HistConn(rows, "the summary"), "t")
    assert summary == "the summary"
    assert len(hist) <= 8
    # Phase A caps still applied to the tail
    assert all(len(h["content"]) <= worker.HISTORY_MSG_CHARS + 1 for h in hist)
    assert sum(len(h["content"]) for h in hist) <= worker.HISTORY_TOTAL_CHARS
    # newest message survives, order oldest-first
    assert hist[-1]["content"].startswith("m0")


async def test_load_history_fetch_limit_is_8_independent_of_char_caps():
    """Pins the row-fetch limit itself: 12 SHORT messages (far under both char
    caps) must trim to exactly the 8 newest. The long-message test above passes
    under any limit >= 5 because the char caps do the trimming there."""
    rows = [{"role": "user", "content": f"m{i}"} for i in range(12)]  # newest-first
    summary, hist = await worker._load_history(_HistConn(rows), "t")
    assert len(hist) == 8
    assert hist[-1]["content"] == "m0"          # newest survives, last
    assert hist[0]["content"] == "m7"           # 8th-newest, first
    assert [h["content"] for h in hist] == [f"m{i}" for i in range(7, -1, -1)]


async def test_load_history_summary_read_is_fail_soft():
    class _NoSummaryConn(_HistConn):
        async def fetchval(self, sql, *args):
            raise RuntimeError("column does not exist")

    rows = [{"role": "user", "content": "hello"}]
    summary, hist = await worker._load_history(_NoSummaryConn(rows), "t")
    assert summary == ""
    assert hist == [{"role": "user", "content": "hello"}]


# ── B1 wiring: _process_one passes sender_name to assemble and prepends
#    the summary to graph.run's history ───────────────────────────────────

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class WorkerConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 9, "thread_id": self.pool.thread_id, "message_id": 3,
                    "kind": "user", "playbook_id": None, "case_id": None,
                    "expectation_id": None}
        if "SELECT content FROM messages" in sql:
            return {"content": "what changed since last week?"}
        return None

    async def fetch(self, sql, *args):
        if "FROM messages" in sql and "ORDER BY id DESC" in sql:
            limit = args[1] if len(args) > 1 else 0
            mid = args[2] if len(args) > 2 else None   # Quick 019 filter on $3
            rows = [r for r in self.pool.history_rows if mid is None or "id" not in r
                    or (r["id"] != mid and not (r["role"] == "user" and r["id"] > mid))]
            return rows[:limit]
        return []

    async def fetchval(self, sql, *args):
        if "SELECT summary FROM threads" in sql:
            return self.pool.summary
        if "FROM people" in sql and "whatsapp = $1" in sql:
            return self.pool.person_id
        if "SELECT name FROM people" in sql:
            return self.pool.person_name
        return None

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class WorkerPool:
    def __init__(self, thread_id="911234567890", person_id=None,
                 person_name=None, summary=None, history_rows=()):
        self.thread_id = thread_id
        self.person_id = person_id
        self.person_name = person_name
        self.summary = summary
        self.history_rows = list(history_rows)
        self.executed = []
        self.conn = WorkerConn(self)

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _wire_process_one(monkeypatch, pool):
    async def fake_get_pool():
        return pool

    async def none1(a, b):
        return None

    async def none2(a, b, c):
        return None

    assemble_calls = []

    async def fake_assemble(content, thread_id, sender_name=None):
        assemble_calls.append((content, thread_id, sender_name))
        return "", {}

    run_calls = []

    async def fake_run(history, user_message, context_text=""):
        run_calls.append({"history": history, "user_message": user_message,
                          "context_text": context_text})
        return "All good."

    async def fake_send(to, text):
        return True

    async def fake_reflect(*a, **k):
        return 0

    monkeypatch.setattr(worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(worker, "_handle_chat_onboarding", none1)
    monkeypatch.setattr(worker, "_handle_watch_confirmation", none2)
    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", fake_assemble)
    monkeypatch.setattr(worker.graph, "run", fake_run)
    monkeypatch.setattr(worker.whatsapp, "send_message", fake_send)
    monkeypatch.setattr(worker.memory, "reflect_and_store", fake_reflect)
    return assemble_calls, run_calls


async def test_worker_passes_sender_name_to_assemble(monkeypatch):
    pool = WorkerPool(person_id=42, person_name="Asha")
    assemble_calls, _ = _wire_process_one(monkeypatch, pool)

    assert await worker._process_one() is True
    assert assemble_calls == [
        ("what changed since last week?", "911234567890", "Asha")]


async def test_worker_passes_none_sender_for_unresolved_and_groups(monkeypatch):
    pool = WorkerPool(person_id=None)
    assemble_calls, _ = _wire_process_one(monkeypatch, pool)
    assert await worker._process_one() is True
    assert assemble_calls[-1] == (
        "what changed since last week?", "911234567890", None)

    grp = WorkerPool(thread_id="123@g.us", person_id=42, person_name="Asha")
    assemble_calls, _ = _wire_process_one(monkeypatch, grp)
    assert await worker._process_one() is True
    # group thread: sender not derivable per-message — no name even though the
    # pool would resolve one
    assert assemble_calls[-1] == (
        "what changed since last week?", "123@g.us", None)


async def test_assemble_output_reaches_graph_run_verbatim(monkeypatch):
    """The Phase B seam: whatever assemble() returns must arrive as graph.run's
    context_text, unchanged. Guards against the worker dropping the return
    value or passing '' regardless."""
    pool = WorkerPool()
    _, run_calls = _wire_process_one(monkeypatch, pool)

    async def sentinel_assemble(content, thread_id, sender_name=None):
        return "MEMORY-CTX-SENTINEL", {"chars": 19}

    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance",
                        sentinel_assemble)

    assert await worker._process_one() is True
    assert run_calls[0]["context_text"] == "MEMORY-CTX-SENTINEL"


async def test_process_one_bumps_summary_counter_after_reply(monkeypatch):
    """The B3 wiring: a successful reply must count toward the thread's rolling
    summary — deleting the _bump_and_maybe_summarize call in _process_one
    would silently disable the feature in production."""
    pool = WorkerPool()
    _wire_process_one(monkeypatch, pool)
    bumped = []

    async def fake_bump(thread_id):
        bumped.append(thread_id)

    monkeypatch.setattr(worker, "_bump_and_maybe_summarize", fake_bump)

    assert await worker._process_one() is True
    assert bumped == ["911234567890"]


async def test_summary_prepended_as_leading_user_message(monkeypatch):
    rows = [{"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "earlier question"}]
    pool = WorkerPool(summary="prior context distilled", history_rows=rows)
    _, run_calls = _wire_process_one(monkeypatch, pool)

    assert await worker._process_one() is True
    history = run_calls[0]["history"]
    assert history[0] == {
        "role": "user",
        "content": worker.SUMMARY_HISTORY_PREFIX + "prior context distilled"}
    # tail follows the summary, oldest-first
    assert history[1]["content"] == "earlier question"
    assert history[2]["content"] == "earlier answer"


async def test_no_summary_message_when_summary_empty(monkeypatch):
    rows = [{"role": "user", "content": "earlier question"}]
    pool = WorkerPool(summary=None, history_rows=rows)
    _, run_calls = _wire_process_one(monkeypatch, pool)

    assert await worker._process_one() is True
    history = run_calls[0]["history"]
    assert all(worker.SUMMARY_HISTORY_PREFIX not in h["content"] for h in history)
