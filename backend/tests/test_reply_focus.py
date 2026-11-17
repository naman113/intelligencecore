"""Quick 021: the supervisor answers the message that was asked.

Run 19 on the client copy: after "is any machine actively consuming diesel" (answered
with an offer to check live fuel readings), the owner asked "how many cases are open".
The first answer came from the open-case lines of the memory message (no tool lists
cases), the grounding guard re-asked for run_sql figures, and the re-ask carried out
the old offer instead. These tests rebuild that exact sequence through the real
worker, history loader, context renderer and graph.run (fake supervisor), and pin the
re-ask note and the list_cases tool. Made-up thread id; no DB, LLM or network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import casetools, contextassembler, worker
from app.agents import graph

THREAD = "15550001111"  # made-up
Q = "how many cases are open"
GUARD_TEXT = ("I did not set up a watch in this turn, so nothing is being watched yet. Ask me "
              "again (for example 'let me know when ...') and I will create it now.")
OFFER = ("I can't see live burn from these figures. The fuel reports update once a day, so "
         "the newest data shows 5 machines used 410 L on 24 Oct. Do you want me to check the "
         "live fuel-level readings for the last few hours?")
CASE5 = ("- Case #5: Machine diesel consumption status (owner query) Recent: The owner asked "
         "whether any machine is actively consuming diesel right now; the assistant owes an "
         "answer from available data | is any machine actively consuming diesel")
CASE4 = ("- Case #4: Barge reported diesel use (44,755 L, Sep) doesn't match tank balances "
         "(-3,368 L)")

# Thread rows, newest first (the DESC query); 37 is the message run 19 answers.
ROWS = [
    {"id": 37, "role": "user", "content": Q},
    {"id": 36, "role": "assistant", "content": OFFER},
    {"id": 35, "role": "user", "content": "is any machine actively consuming diesel"},
    {"id": 34, "role": "assistant", "content": GUARD_TEXT},
    {"id": 33, "role": "user", "content": "hi"},
]


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class Conn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 19, "thread_id": THREAD, "message_id": 37, "kind": "user",
                    "playbook_id": None, "case_id": None, "expectation_id": None}
        if "FROM messages WHERE id = $1" in sql:
            return {"content": Q, "wa_message_id": None}
        return None

    async def fetch(self, sql, *args):
        if "FROM messages" in sql and "role IN ('user', 'assistant')" in sql:
            rows = ROWS
            if len(args) > 2 and args[2] is not None:  # Quick 019: the trigger and later users out
                rows = [r for r in rows if r["id"] != args[2]
                        and not (r["role"] == "user" and r["id"] > args[2])]
            return rows[:args[1]]
        return []

    async def fetchval(self, sql, *args):
        return None  # no rolling summary, unknown sender

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class Pool:
    def __init__(self):
        self.executed = []
        self.conn = Conn(self)

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *args):
        return []  # no tool calls in this run, no earlier runs to carry files from

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class Supervisor:
    """Scripted replies; records every payload graph.run hands it."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, payload, config=None):
        self.calls.append(payload["messages"])
        return {"messages": [SimpleNamespace(content=self.replies.pop(0))]}


@pytest.fixture
def run19(monkeypatch):
    def wire(replies):
        pool = Pool()
        sup = Supervisor(replies)

        async def gp():
            return pool

        async def none(*a, **k):
            return None

        async def nothing(*a, **k):
            return []

        async def cases(msg, thread_id):
            return [CASE5, CASE4]

        sends = []

        async def send(to, text):
            sends.append((to, text))
            return True

        async def configs():
            return {"fake": True}

        monkeypatch.setattr(worker, "get_pool", gp)
        monkeypatch.setattr(worker, "_handle_chat_onboarding", none)
        monkeypatch.setattr(worker, "_handle_watch_confirmation", none)
        monkeypatch.setattr(worker.whatsapp, "send_message", send)
        monkeypatch.setattr(worker.memory, "reflect_and_store", none)
        # the real renderer, with the sources run 19 had: two open-case lines, nothing else
        monkeypatch.setattr(contextassembler, "_learnings", nothing)
        monkeypatch.setattr(contextassembler, "_lessons", nothing)
        monkeypatch.setattr(contextassembler, "_global_facts", nothing)
        monkeypatch.setattr(contextassembler, "_open_cases", cases)
        monkeypatch.setattr(graph, "effective_configs", configs)
        monkeypatch.setattr(graph, "_supervisor", sup)
        monkeypatch.setattr(graph, "_built_with", {"fake": True})
        return pool, sup, sends
    return wire


def _last_user_blocks(messages):
    """The supervisor's final user turn as Anthropic receives it (consecutive user
    messages merged into content blocks by langchain-anthropic)."""
    from langchain_anthropic.chat_models import _format_messages
    lc = [HumanMessage(c) if r == "user" else AIMessage(c) for r, c in messages]
    _, formatted = _format_messages(lc)
    last = formatted[-1]
    assert last["role"] == "user"
    content = last["content"]
    return [content] if isinstance(content, str) else [b["text"] for b in content]


async def test_run19_last_user_turn_is_the_new_question(run19):
    pool, sup, sends = run19(["There are 3 open cases on the Cases page."])

    assert await worker._process_one() is True

    (messages,) = sup.calls
    clock = [m for m in messages if m[1].startswith("CLOCK:")]  # Quick 049
    assert len(clock) == 1
    messages = [m for m in messages if m not in clock]
    assert messages[-1] == ("user", Q)                       # the new question, last, once
    assert sum(1 for m in messages if m == ("user", Q)) == 1
    mem_role, mem = messages[-2]
    assert mem_role == "user" and mem.startswith(graph.MEMORY_HEADER) and "Case #5" in mem
    # the history is the conversation as it stood: it ends with the offer, before memory
    assert [r for r, _ in messages[:-2]] == ["user", "assistant", "user", "assistant"]
    assert messages[0] == ("user", "hi")
    assert messages[3][0] == "assistant" and messages[3][1].endswith(OFFER)
    # nothing is injected after the question; Anthropic sees it as the last block
    assert _last_user_blocks(messages)[-1] == Q
    assert sends == [(THREAD, "There are 3 open cases on the Cases page.")]


async def test_run19_reask_stays_on_the_new_question(run19):
    first = ("You have *2 open cases*: Case #5 (live diesel use) and Case #4 (barge 44,755 L "
             "against -3,368 L). Want me to check live fuel levels for case #5?")
    pool, sup, sends = run19([first, "There are 3 open cases (list_cases)."])

    assert await worker._process_one() is True

    assert len(sup.calls) == 2                               # the grounding guard re-asked
    reask = sup.calls[1]
    role, text = reask[-1]
    assert role == "user" and text.startswith(Q)
    assert text == Q + worker._reask_note(Q)
    assert f'"{Q}"' in text and "only this message" in text
    assert "offer from an earlier reply" in text and "list_cases" in text
    assert reask[:-1] == sup.calls[0][:-1]                   # same history and memory
    assert _last_user_blocks(reask)[-1] == text
    assert sends[-1] == (THREAD, "There are 3 open cases (list_cases).")


def test_reask_note_quotes_and_clips_the_message():
    note = worker._reask_note("line one\nline two")
    assert '"line one line two"' in note
    long = worker._reask_note("x" * 1000)
    assert "x" * worker.REASK_QUOTE_CHARS + "…" in long and "x" * 301 not in long


def test_supervisor_prompt_carries_the_current_message_and_cases_rules():
    p = graph.DEFAULT_PROMPTS["supervisor"]
    assert "CURRENT MESSAGE (Quick 021)" in p and "clearly accepts it" in p
    assert "never the question itself" in p
    assert "CASES (Quick 021)" in p and "call list_cases" in p
    assert "list_cases" in graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert graph.AGENT_TOOL_SUPERSET["supervisor"][-2:] == ["discover_schema",
                                                            "propose_context_change"]
    assert "list_cases" not in graph.AGENT_TOOL_SUPERSET["data"]
    assert graph._tool_registry()["list_cases"] is casetools.list_cases


# ── list_cases ───────────────────────────────────────────────────────────

def _case(i, title, status="open", origin="insight", thread=None, exp=0, overdue=0):
    t = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)
    return {"id": i, "title": title, "status": status, "origin": origin, "created_at": t,
            "closed_at": t if status in ("closed", "dismissed") else None,
            "last_activity_at": t, "open_exp": exp, "overdue": overdue, "_thread": thread}


class CasePool:
    def __init__(self, cases):
        self.cases = cases
        self.queries = []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        live = [c for c in self.cases]
        return {"open": sum(c["status"] == "open" for c in live),
                "pending": sum(c["status"] == "pending_ack" for c in live),
                "closed": sum(c["status"] in ("closed", "dismissed") for c in live)}

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        rows = self.cases
        if "IN ('open', 'pending_ack')" in sql.split("ORDER BY")[0]:
            rows = [c for c in rows if c["status"] in ("open", "pending_ack")]
        if "origin_chat_jid = $3" in sql:
            rows = [c for c in rows if c["_thread"] == args[2]]
        return rows[:args[1]]


@pytest.fixture
def cases_env(monkeypatch):
    def wire(cases, thread, owner=True):
        pool = CasePool(cases)

        async def gp():
            return pool

        async def log_tool(*a, **k):
            return None

        monkeypatch.setattr(casetools, "get_pool", gp)
        monkeypatch.setattr(casetools, "_log_tool", log_tool)
        from app import dashboardtools
        monkeypatch.setattr(dashboardtools, "is_owner_thread", lambda t: owner)
        graph.ctx_phone.set(thread)
        return pool
    return wire


async def test_list_cases_owner_gets_every_open_case(cases_env):
    pool = cases_env([_case(5, "Machine diesel consumption status", origin=None, thread=THREAD),
                      _case(4, "Barge diesel mismatch", exp=2, overdue=1),
                      _case(3, "Alerts never acknowledged"),
                      _case(2, "Excavator fuel drops", status="closed")], THREAD)
    out = await casetools.list_cases.ainvoke({})
    assert out.startswith("Cases page: 3 open case(s), 1 closed or dismissed.")
    assert "#5 Machine diesel consumption status" in out
    assert "#4 Barge diesel mismatch (open, from insight" in out
    assert "2 open expectation(s) (1 overdue)" in out
    assert "#3 Alerts never acknowledged" in out and "#2" not in out
    # the Cases page filter: no exploration log, sandbox only for test threads
    count_sql, count_args = pool.queries[0]
    assert "c.origin IS DISTINCT FROM 'exploration'" in count_sql and count_args == (False,)


async def test_list_cases_elsewhere_counts_and_this_chats_cases_only(cases_env):
    group = "120363000000000001@g.us"  # made-up
    cases_env([_case(9, "Pump delivery late", thread=group),
               _case(4, "Barge diesel mismatch")], group, owner=False)
    out = await casetools.list_cases.ainvoke({"status": "open"})
    assert "2 open case(s)" in out and "Details are shown only to the owner" in out
    assert "#9 Pump delivery late" in out and "#4" not in out


async def test_list_cases_bad_arguments_fall_back(cases_env):
    pool = cases_env([], THREAD)
    out = await casetools.list_cases.ainvoke({"status": "everything", "limit": 500})
    assert "0 open case(s)" in out and "No open cases to list." in out
    assert pool.queries[1][1][1] == casetools.CASE_LIST_MAX


async def test_list_cases_error_is_reported_not_raised(monkeypatch):
    async def gp():
        raise RuntimeError("db down")

    async def log_tool(*a, **k):
        return None

    monkeypatch.setattr(casetools, "get_pool", gp)
    monkeypatch.setattr(casetools, "_log_tool", log_tool)
    graph.ctx_phone.set(THREAD)
    assert (await casetools.list_cases.ainvoke({})).startswith("TOOL ERROR: RuntimeError")


def test_list_cases_has_a_live_step_label():
    from app import runstatus
    assert runstatus.LABELS["list_cases"] == "Checking the cases"
