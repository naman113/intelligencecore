"""Quick 019 (audit Fix 2): each run's history is the conversation as it stood
when its message arrived, plus the replies sent since.

The audit reproduced three defects with the real worker._load_history: a
second Ask message sent while a run works, a burst of two messages, and group
chatter after a mention. The fake connection below applies the same filter as
the SQL (pinned by test_history_sql_excludes_trigger_and_newer_user_rows), so
these tests read like the database would answer.
"""

from app import worker


class _Rows:
    """messages rows for one thread (oldest first, ids ascending), queried the
    way _load_history's SQL does: user/assistant only, minus the triggering
    message and any user row newer than it, newest first, LIMIT $2."""

    def __init__(self, rows, summary=None):
        self.rows = [dict(id=i, role=r, content=c) for i, r, c in rows]
        self.summary = summary
        self.queries = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        _thread, limit = args[0], args[1]
        mid = args[2] if len(args) > 2 else None
        keep = [r for r in self.rows if r["role"] in ("user", "assistant")
                and (mid is None or (r["id"] != mid
                                     and not (r["role"] == "user" and r["id"] > mid)))]
        return sorted(keep, key=lambda r: -r["id"])[:limit]

    async def fetchval(self, sql, *args):
        if "SELECT summary FROM threads" in sql:
            return self.summary
        return None


def _texts(hist):
    return [(h["role"], h["content"]) for h in hist]


async def test_history_sql_excludes_trigger_and_newer_user_rows():
    conn = _Rows([(1, "user", "hi")])
    await worker._load_history(conn, "t", message_id=1)
    sql, args = conn.queries[-1]
    assert "$3::bigint IS NULL" in sql
    assert "id <> $3" in sql
    assert "NOT (role = 'user' AND id > $3)" in sql
    assert "ORDER BY id DESC LIMIT $2" in sql
    assert args == ("t", 8, 1)
    # without an id the filter is a no-op ($3 IS NULL), i.e. the old query
    await worker._load_history(conn, "t")
    assert conn.queries[-1][1] == ("t", 8, None)


async def test_second_ask_message_sees_first_exchange_once():
    """m2 was sent while run 1 worked on m1; run 1 replied r1 afterwards.
    Run 2 must see [m1, r1] and then m2 (passed separately), not m2 twice
    with r1 after it."""
    conn = _Rows([(1, "user", "what were sales yesterday?"),
                  (2, "user", "and the day before?"),
                  (3, "assistant", "Yesterday was quiet.")])
    _, hist = await worker._load_history(conn, "ui:owner", message_id=2)
    assert _texts(hist) == [("user", "what were sales yesterday?"),
                            ("assistant", "Yesterday was quiet.")]


async def test_burst_run_does_not_see_the_later_message():
    """m1 and m2 both arrived before run 1 started: run 1 answers m1 only and
    must not see m2 (that is run 2's question)."""
    conn = _Rows([(1, "user", "send me the fuel report"),
                  (2, "user", "also the certificate list")])
    _, hist = await worker._load_history(conn, "919000000001", message_id=1)
    assert hist == []
    conn.rows.append(dict(id=3, role="assistant", content="Fuel report attached."))
    _, hist2 = await worker._load_history(conn, "919000000001", message_id=2)
    assert _texts(hist2) == [("user", "send me the fuel report"),
                             ("assistant", "Fuel report attached.")]


async def test_group_run_ignores_chatter_after_the_mention():
    conn = _Rows([(10, "user", "truck 7 is back at the depot"),
                  (11, "user", "@bot how much diesel did truck 7 take?"),
                  (12, "user", "lunch at 1?"),
                  (13, "user", "ok see you")])
    _, hist = await worker._load_history(conn, "1203@g.us", message_id=11)
    assert _texts(hist) == [("user", "truck 7 is back at the depot")]
    # a later mention sees the earlier chatter and the reply sent since
    conn.rows += [dict(id=14, role="assistant", content="Truck 7 took 120 L."),
                  dict(id=15, role="user", content="@bot and truck 9?")]
    _, hist2 = await worker._load_history(conn, "1203@g.us", message_id=15)
    assert [c for _, c in _texts(hist2)][-3:] == ["lunch at 1?", "ok see you",
                                                   worker.HISTORY_REPLY_NOTE + "Truck 7 took 120 L."]


async def test_retry_keeps_replies_sent_since_and_not_the_question():
    """Retry reuses the failed run's message id (askapi.retry): the apology
    written after it is history, the question itself is not repeated."""
    conn = _Rows([(1, "user", "big question"),
                  (2, "assistant", "Sorry — that request needed more processing than I allow.")])
    _, hist = await worker._load_history(conn, "ui:owner", message_id=1)
    assert _texts(hist) == [("assistant",
                             "Sorry — that request needed more processing than I allow.")]


async def test_limit_counts_eight_rows_without_the_trigger():
    rows = [(i, "user" if i % 2 else "assistant", f"m{i}") for i in range(1, 13)]
    conn = _Rows(rows)
    _, hist = await worker._load_history(conn, "t", message_id=11)
    assert len(hist) == 8
    assert not any(h["content"].endswith("m11") for h in hist)
    assert hist[-1]["role"] == "assistant"       # a reply sent since stays, newest last
    assert hist[-1]["content"].endswith("m12")


# ── _process_one wiring ──────────────────────────────────────────────────

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _WorkerConn(_Rows):
    def __init__(self, rows, task_message_id):
        super().__init__(rows)
        self.task_message_id = task_message_id
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 21, "thread_id": "ui:owner", "message_id": self.task_message_id,
                    "kind": "user", "playbook_id": None, "case_id": None,
                    "expectation_id": None}
        if "SELECT content FROM messages" in sql:
            mid = args[0]
            return next(({"content": r["content"]} for r in self.rows if r["id"] == mid),
                        {"content": "m-without-id"})
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _WorkerPool:
    def __init__(self, conn):
        self.conn = conn
        self.executed = conn.executed

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _wire(monkeypatch, pool):
    async def fake_get_pool():
        return pool

    async def none_(*a):
        return None

    async def fake_assemble(content, thread_id, sender_name=None):
        return "", {}

    runs = []

    async def fake_run(history, user_message, context_text=""):
        runs.append((history, user_message))
        return "ok"

    async def fake_send(to, text):
        return True

    async def fake_reflect(*a, **k):
        return 0

    async def fake_bump(thread_id):
        return None

    monkeypatch.setattr(worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(worker, "_handle_chat_onboarding", none_)
    monkeypatch.setattr(worker, "_handle_watch_confirmation", none_)
    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", fake_assemble)
    monkeypatch.setattr(worker.graph, "run", fake_run)
    monkeypatch.setattr(worker.whatsapp, "send_message", fake_send)
    monkeypatch.setattr(worker.memory, "reflect_and_store", fake_reflect)
    monkeypatch.setattr(worker, "_bump_and_maybe_summarize", fake_bump)
    return runs


async def test_process_one_second_message_sees_first_exchange(monkeypatch):
    conn = _WorkerConn([(1, "user", "first question"),
                        (2, "user", "second question"),
                        (3, "assistant", "first answer")], task_message_id=2)
    runs = _wire(monkeypatch, _WorkerPool(conn))
    assert await worker._process_one() is True
    history, user_message = runs[0]
    assert user_message == "second question"
    assert _texts(history) == [("user", "first question"), ("assistant", "first answer")]


async def test_process_one_without_message_id_keeps_content_strip(monkeypatch):
    """The content-compare strip stays as the fallback for a task that names no
    message: the newest row equal to the message text is not repeated."""
    conn = _WorkerConn([(1, "assistant", "hello"), (2, "user", "m-without-id")],
                       task_message_id=None)
    runs = _wire(monkeypatch, _WorkerPool(conn))
    assert await worker._process_one() is True
    history, user_message = runs[0]
    assert user_message == "m-without-id"
    assert _texts(history) == [("assistant", "hello")]


async def test_process_one_with_id_does_not_strip_an_equal_earlier_message(monkeypatch):
    """With an id, an earlier message that happens to read the same ("yes")
    is real history and stays."""
    conn = _WorkerConn([(1, "user", "yes"), (2, "user", "yes")], task_message_id=2)
    runs = _wire(monkeypatch, _WorkerPool(conn))
    assert await worker._process_one() is True
    history, _ = runs[0]
    assert _texts(history) == [("user", "yes")]
