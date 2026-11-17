"""Phase A reactive-path changes: cache-stable prompt layout, history bounds,
tool-output caps, reactive token budget, remember_fact delegation, and
reflection gating. Pure logic — fakes/monkeypatch only, no DB/LLM/network."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.prebuilt import create_react_agent

from app import memory as memory_mod
from app import memoryservice, worker
from app.agents import graph
from app.budget import BudgetExceeded, TokenBudget


# ── fakes ────────────────────────────────────────────────────────────────

class FakeSupervisor:
    def __init__(self, reply="done!"):
        self.reply = reply
        self.calls = []

    async def ainvoke(self, payload, config=None):
        self.calls.append((payload, config))
        return {"messages": [SimpleNamespace(content=self.reply)]}


_seen_model_inputs = []


class _FakeChat(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _seen_model_inputs.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, **kwargs):
        return self


class ChatAnthropic(_FakeChat):
    """Name is what _cached_prompt dispatches on."""


class OtherChat(_FakeChat):
    pass


def _wire_fake_supervisor(monkeypatch, sup):
    """Make graph.run use `sup` without touching real models or the DB."""
    configs = {"fake": True}

    async def fake_effective_configs():
        return configs

    monkeypatch.setattr(graph, "effective_configs", fake_effective_configs)
    monkeypatch.setattr(graph, "_supervisor", sup)
    monkeypatch.setattr(graph, "_built_with", configs)
    # Quick 007: a ui:* ctx_phone adds the app-chat channel note. Some sync
    # fixtures elsewhere set ctx_phone without resetting it; these layout tests
    # pin the WhatsApp/no-channel layout.
    graph.ctx_phone.set(None)


# ── A2: graph.run message layout ─────────────────────────────────────────

async def test_memory_block_is_its_own_message(monkeypatch):
    sup = FakeSupervisor()
    _wire_fake_supervisor(monkeypatch, sup)

    history = [{"role": "user", "content": "earlier q"},
               {"role": "assistant", "content": "earlier a"}]
    await graph.run(history, "what now?", "- [fact] client prefers mornings")

    (payload, config), = sup.calls
    messages = payload["messages"]
    # Quick 049: the business clock rides as its own message after the history
    assert messages[2][1].startswith("CLOCK:")
    messages = messages[:2] + messages[3:]
    assert messages[0] == ("user", "earlier q")
    assert messages[1] == ("assistant", "earlier a")
    # memory block: own user message, delimited by the header, before the real one
    role, text = messages[2]
    assert role == "user"
    assert text.startswith(graph.MEMORY_HEADER)
    assert "client prefers mornings" in text
    # the user's message itself is NOT prefixed anymore
    assert messages[3] == ("user", "what now?")
    assert len(messages) == 4


async def test_no_memory_message_when_context_empty(monkeypatch):
    sup = FakeSupervisor()
    _wire_fake_supervisor(monkeypatch, sup)

    await graph.run([{"role": "user", "content": "hi"}], "question", "")

    (payload, _), = sup.calls
    messages = payload["messages"]
    assert messages[1][1].startswith("CLOCK:")  # Quick 049
    messages = messages[:1] + messages[2:]
    assert messages == [("user", "hi"), ("user", "question")]
    assert not any(graph.MEMORY_HEADER in str(m) for m in messages[:-1][:1])


# ── A4b: reactive token budget attached to callbacks ─────────────────────

async def test_run_attaches_token_budget_callback(monkeypatch):
    sup = FakeSupervisor()
    _wire_fake_supervisor(monkeypatch, sup)
    monkeypatch.setattr(
        graph, "get_settings",
        lambda: SimpleNamespace(reactive_token_budget=1234, langfuse_secret_key=""))

    await graph.run([], "hello")

    (_, config), = sup.calls
    budgets = [cb for cb in config["callbacks"] if isinstance(cb, TokenBudget)]
    assert len(budgets) == 1
    assert budgets[0].budget == 1234


class _ScriptedUsageChat(BaseChatModel):
    """Replays scripted AIMessages, stamping each with usage_metadata so the
    TokenBudget callback has something to count."""

    script: list
    tokens: int

    @property
    def _llm_type(self):
        return "scripted-usage"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script.pop(0) if self.script else AIMessage(content="ok")
        msg.usage_metadata = {"input_tokens": 0, "output_tokens": self.tokens,
                              "total_tokens": self.tokens}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


async def test_token_budget_counts_delegate_subagent_usage(monkeypatch):
    """The budget attached at the supervisor must observe tokens burned inside
    delegate sub-agents, which _delegate invokes with a fresh config carrying
    only _callbacks() — enforcement rests on langchain contextvar propagation
    of the parent's inheritable callbacks."""
    from langchain_core.tools import tool as lc_tool

    # No langfuse configured -> graph._callbacks() returns {} (the production
    # shape on a bare install), so the sub-agent config has NO explicit
    # callbacks key and the parent's must propagate.
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(langfuse_secret_key=""))

    sub_model = _ScriptedUsageChat(
        script=[AIMessage(content="sub result")], tokens=700)
    sub_agent = create_react_agent(sub_model, [], prompt="You are the sub-agent.")

    @lc_tool
    async def delegate_sub(task: str) -> str:
        """Delegate a task to the sub-agent (mirrors graph._delegate)."""
        out = await sub_agent.ainvoke(
            {"messages": [("user", task)]},
            config={"recursion_limit": 10, **graph._callbacks()},
        )
        return str(out["messages"][-1].content)

    sup_model = _ScriptedUsageChat(
        script=[AIMessage(content="", tool_calls=[
                    {"name": "delegate_sub", "args": {"task": "compute"},
                     "id": "c1", "type": "tool_call"}]),
                AIMessage(content="done")],
        tokens=100)
    supervisor = create_react_agent(sup_model, [delegate_sub])

    tb = TokenBudget(budget=10_000)
    out = await supervisor.ainvoke(
        {"messages": [("user", "hi")]},
        config={"recursion_limit": 10, "callbacks": [tb]},
    )
    assert out["messages"][-1].content == "done"
    # 2 supervisor calls @100 + 1 sub-agent call @700
    assert tb.used == 900


async def test_token_budget_aborts_from_inside_delegate(monkeypatch):
    """When the sub-agent's usage blows the budget, BudgetExceeded surfaces out
    of the supervisor run instead of being swallowed by callback dispatch."""
    from langchain_core.tools import tool as lc_tool

    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(langfuse_secret_key=""))

    sub_model = _ScriptedUsageChat(
        script=[AIMessage(content="sub result")], tokens=700)
    sub_agent = create_react_agent(sub_model, [], prompt="You are the sub-agent.")

    @lc_tool
    async def delegate_sub(task: str) -> str:
        """Delegate a task to the sub-agent (mirrors graph._delegate)."""
        out = await sub_agent.ainvoke(
            {"messages": [("user", task)]},
            config={"recursion_limit": 10, **graph._callbacks()},
        )
        return str(out["messages"][-1].content)

    sup_model = _ScriptedUsageChat(
        script=[AIMessage(content="", tool_calls=[
                    {"name": "delegate_sub", "args": {"task": "compute"},
                     "id": "c1", "type": "tool_call"}]),
                AIMessage(content="done")],
        tokens=100)
    supervisor = create_react_agent(sup_model, [delegate_sub])

    tb = TokenBudget(budget=500)  # sub-agent's 700 blows it mid-delegate
    with pytest.raises(BudgetExceeded):
        await supervisor.ainvoke(
            {"messages": [("user", "hi")]},
            config={"recursion_limit": 10, "callbacks": [tb]},
        )


# ── A2: cache breakpoint on the system prompt ────────────────────────────

def test_cached_prompt_anthropic_gets_cache_control():
    prompt = graph._cached_prompt("You are a test agent.", ChatAnthropic())
    assert isinstance(prompt, SystemMessage)
    block = prompt.content[0]
    assert block["type"] == "text"
    assert block["text"] == "You are a test agent."
    assert block["cache_control"] == {"type": "ephemeral"}


def test_cached_prompt_other_providers_get_plain_string():
    assert graph._cached_prompt("sys", OtherChat()) == "sys"
    # empty prompt never becomes an (invalid) empty content block
    assert graph._cached_prompt("", ChatAnthropic()) == ""


async def test_agent_builds_and_runs_with_cached_prompt_and_merged_users():
    """The pinned langgraph accepts a SystemMessage prompt with a cache_control
    text block, and consecutive user messages cause no runtime error."""
    model = ChatAnthropic()
    agent = create_react_agent(
        model, [], prompt=graph._cached_prompt("You are a test agent.", model))
    out = await agent.ainvoke(
        {"messages": [("user", "MEMORY CONTEXT: something"), ("user", "hi")]})
    assert out["messages"][-1].content == "ok"
    sent = _seen_model_inputs[-1]
    assert isinstance(sent[0], SystemMessage)  # prompt made it in front


# ── A5: history bounds ───────────────────────────────────────────────────

class _HistConn:
    def __init__(self, rows, summary=None):
        self.rows = rows
        self.summary = summary

    async def fetch(self, sql, *args):
        # already newest-first, as the DESC query returns; respect LIMIT $2
        # and, for rows that carry ids, the Quick 019 filter on $3 (the
        # triggering message and newer user rows are left out)
        limit = args[1] if len(args) > 1 else len(self.rows)
        mid = args[2] if len(args) > 2 else None
        rows = [r for r in self.rows if mid is None or "id" not in r
                or (r["id"] != mid and not (r["role"] == "user" and r["id"] > mid))]
        return rows[:limit]

    async def fetchval(self, sql, *args):
        if "SELECT summary FROM threads" in sql:
            return self.summary
        return None


async def test_history_per_message_truncation():
    rows = [{"role": "user", "content": "x" * 5000},
            {"role": "assistant", "content": "short"}]
    _, hist = await worker._load_history(_HistConn(rows), "t")
    assert hist[0]["content"] == "short"          # reversed to oldest-first
    assert hist[1]["content"] == "x" * 2000 + "…"
    assert len(hist[1]["content"]) == 2001


async def test_history_total_cap_drops_oldest_whole_messages():
    # 8 messages fetched (B3 tail limit) x 3000 chars -> 2001 each after
    # clipping; only 5 fit in 12000
    rows = [{"role": "user", "content": f"m{i}" + "x" * 3000} for i in range(10)]
    _, hist = await worker._load_history(_HistConn(rows), "t")
    assert len(hist) == 5
    assert sum(len(h["content"]) for h in hist) <= worker.HISTORY_TOTAL_CHARS
    # rows[0] is the NEWEST (DESC query); it must survive, oldest dropped
    assert hist[-1]["content"].startswith("m0")
    assert hist[0]["content"].startswith("m4")


async def test_history_small_messages_untouched():
    rows = [{"role": "user", "content": "hello"}]
    summary, hist = await worker._load_history(_HistConn(rows), "t")
    assert summary == ""
    assert hist == [{"role": "user", "content": "hello"}]


# ── Phase B: worker context assembly is delegated to contextassembler ────
# (Behavioral coverage of dedupe/provenance/caps/budget lives in
# test_contextassembler.py; here we only guard the worker-side wiring —
# see test_thread_summary.py for assemble-argument assertions too.)


# ── A3b: tool output caps ────────────────────────────────────────────────

async def test_search_business_context_capped_at_20k(monkeypatch):
    # Quick 037: each hit's text is cut at search_context_hit_chars (800) with a note, and
    # the list is cut at a hit boundary, so the result stays valid JSON under 20k.
    async def huge(query, limit=8):
        return [{"text": "y" * 30000}]
    monkeypatch.setattr(graph, "_search_ctx", huge)
    result = await graph.search_business_context.ainvoke({"query": "q"})
    assert len(result) <= 20000
    hit = json.loads(result)[0]
    assert hit["text"].startswith("y" * 800) and "cut at 800 of 30000 characters" in hit["text"]


def _doc_row(i, n_fields, value_len=5):
    return {"id": i, "filename": f"doc{i}.pdf", "source": "whatsapp",
            "doc_type": "invoice", "summary": "an invoice", "status": "extracted",
            "created_at": "2026-01-01",
            "extracted": json.dumps(
                {"fields": {f"f{k}": {"value": "v" * value_len}
                            for k in range(n_fields)}})}


class _DocPool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows


async def test_search_documents_slices_fields_to_40(monkeypatch):
    async def fake_pool():
        return _DocPool([_doc_row(1, 60)])
    monkeypatch.setattr(graph, "get_pool", fake_pool)
    result = await graph.search_documents.ainvoke({"query": "invoice"})
    docs = json.loads(result)
    assert len(docs[0]["fields"]) == 40


async def test_search_documents_final_cap_20k(monkeypatch):
    async def fake_pool():
        return _DocPool([_doc_row(i, 40, value_len=1000) for i in range(5)])
    monkeypatch.setattr(graph, "get_pool", fake_pool)
    result = await graph.search_documents.ainvoke({"query": "invoice"})
    assert len(result) == 20000


# ── A6: remember_fact delegates to memory.store_learning ─────────────────

async def test_remember_fact_delegates_to_store_learning(monkeypatch):
    """Quick 020: the run's identity (set by the worker) rides along, so
    memory.store_learning decides provenance, scope and whether to hold it."""
    from app import memscope
    captured = {}

    async def fake_store(content, kind, source=None, *, thread_id=None, run_id=None,
                         scope=None, identity=None):
        captured.update(content=content, kind=kind, source=source,
                        thread_id=thread_id, run_id=run_id, identity=identity)
        return "stored"

    monkeypatch.setattr(memory_mod, "store_learning", fake_store)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(openai_api_key="k"))
    ident = memscope.RunIdentity("person", "person:9", "wa:911234567890",
                                 ("wa:911234567890",), "whatsapp", "Asha",
                                 "911234567890", "911234567890")
    tok_phone = graph.ctx_phone.set("911234567890")
    tok_run = graph.ctx_run_id.set(55)
    tok_id = memscope.ctx_identity.set(ident)
    try:
        result = await graph.remember_fact.ainvoke(
            {"fact": "  always use raw data  ", "kind": "preference"})
    finally:
        graph.ctx_phone.reset(tok_phone)
        graph.ctx_run_id.reset(tok_run)
        memscope.ctx_identity.reset(tok_id)
    assert captured == {"content": "always use raw data", "kind": "preference",
                        "source": None, "thread_id": "911234567890", "run_id": 55,
                        "identity": ident}
    assert result == "stored learning (preference)"


async def test_remember_fact_outcomes_and_validation(monkeypatch):
    outcomes = iter(["merged", "duplicate"])

    async def fake_store(content, kind, source=None, *, thread_id=None, run_id=None,
                         scope=None, identity=None):
        return next(outcomes)

    async def fake_automatic(thread_id):           # Quick 020: no worker identity
        from app import memscope
        return memscope._nobody("")

    from app import memscope
    monkeypatch.setattr(memscope, "automatic", fake_automatic)

    monkeypatch.setattr(memory_mod, "store_learning", fake_store)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(openai_api_key="k"))

    assert await graph.remember_fact.ainvoke(
        {"fact": "some standing rule", "kind": "bogus-kind"}
    ) == "merged into an existing learning (fact)"     # bad kind coerced to fact
    assert await graph.remember_fact.ainvoke(
        {"fact": "some standing rule", "kind": "fact"}
    ) == "already stored (duplicate)"


async def test_remember_fact_caps_at_600_and_surfaces_value_errors(monkeypatch):
    captured = {}

    async def fake_store(content, kind, source=None, *, thread_id=None, run_id=None,
                         scope=None, identity=None):
        captured["content"] = content
        raise ValueError("learning content too short")

    async def fake_automatic(thread_id):           # Quick 020: no worker identity
        from app import memscope
        return memscope._nobody("")

    from app import memscope
    monkeypatch.setattr(memscope, "automatic", fake_automatic)

    monkeypatch.setattr(memory_mod, "store_learning", fake_store)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(openai_api_key="k"))

    result = await graph.remember_fact.ainvoke({"fact": "z" * 700, "kind": "fact"})
    assert len(captured["content"]) == 600
    assert result == "ERROR: learning content too short"


# ── A4b/A7b: worker _process_one behavior ────────────────────────────────

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 7, "thread_id": self.pool.thread_id, "message_id": 3,
                    "kind": "user", "playbook_id": None, "case_id": None,
                    "expectation_id": None}
        if "SELECT content FROM messages" in sql:
            return {"content": "please analyze everything"}
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        if "FROM people" in sql and "whatsapp = $1" in sql:
            return self.pool.person_id
        return None

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class FakePool:
    def __init__(self, thread_id="911234567890", person_id=None):
        self.thread_id = thread_id
        self.person_id = person_id
        self.executed = []
        self.conn = FakeConn(self)

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


reflections_scopes: dict = {}      # Quick 020: scope passed to each reflection


def _wire_worker(monkeypatch, pool, run_impl):
    async def fake_get_pool():
        return pool

    async def none1(a, b):
        return None

    async def none2(a, b, c):
        return None

    async def empty_ctx(content, thread_id, sender_name=None):
        return "", {}

    sends = []

    async def fake_send(to, text):
        sends.append((to, text))
        return True

    reflections = []

    async def fake_reflect(run_id, thread_id, user_message, reply, tool_log,
                           source=None, scope=None):
        reflections.append({"run_id": run_id, "source": source})
        scopes.append(scope)                       # Quick 020
        return 0

    scopes = []
    reflections_scopes[id(reflections)] = scopes

    monkeypatch.setattr(worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(worker, "_handle_chat_onboarding", none1)
    monkeypatch.setattr(worker, "_handle_watch_confirmation", none2)
    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", empty_ctx)
    monkeypatch.setattr(worker.graph, "run", run_impl)
    monkeypatch.setattr(worker.whatsapp, "send_message", fake_send)
    monkeypatch.setattr(worker.memory, "reflect_and_store", fake_reflect)
    return sends, reflections


async def test_budget_exceeded_fails_run_and_apologizes(monkeypatch):
    pool = FakePool()

    async def blow_budget(history, user_message, context_text=""):
        raise BudgetExceeded("token budget 150000 exceeded (151000)")

    sends, reflections = _wire_worker(monkeypatch, pool, blow_budget)

    assert await worker._process_one() is True

    failed = [(sql, args) for sql, args in pool.executed
              if "status='failed'" in sql]
    assert failed and failed[0][1] == (7, "token budget exceeded")
    # short apology went out through the normal reply path
    assert sends and sends[0][0] == pool.thread_id
    assert sends[0][1].startswith("Sorry")
    # the run is NOT retried: nothing requeued it
    assert not any("'queued'" in sql for sql, _ in pool.executed)
    # and reflection never ran on the aborted task
    assert reflections == []


async def test_reflection_skipped_for_unresolved_sender(monkeypatch):
    pool = FakePool(person_id=None)

    async def ok_run(history, user_message, context_text=""):
        return "All good."

    sends, reflections = _wire_worker(monkeypatch, pool, ok_run)

    assert await worker._process_one() is True
    assert sends and sends[0][1] == "All good."
    assert reflections == []


async def test_reflection_carries_person_source_when_resolved(monkeypatch):
    pool = FakePool(person_id=42)

    async def ok_run(history, user_message, context_text=""):
        return "All good."

    sends, reflections = _wire_worker(monkeypatch, pool, ok_run)

    assert await worker._process_one() is True
    assert reflections == [{"run_id": 7, "source": "person:42"}]
    # Quick 020: a preference from this run applies to this person (by number)
    assert reflections_scopes[id(reflections)] == ["wa:911234567890"]


async def test_reflection_skipped_for_group_threads(monkeypatch):
    pool = FakePool(thread_id="123@g.us", person_id=42)

    async def ok_run(history, user_message, context_text=""):
        return "All good."

    sends, reflections = _wire_worker(monkeypatch, pool, ok_run)

    assert await worker._process_one() is True
    assert reflections == []  # sender not derivable per-message in a group


async def test_reflection_runs_for_dashboard_thread(monkeypatch):
    """The 'dashboard' thread (operator surface: /api/test-message, scenario
    scripts) has no phone to resolve but is trusted by construction — reflection
    must still run, stamped with source='dashboard'."""
    pool = FakePool(thread_id="dashboard", person_id=None)

    async def ok_run(history, user_message, context_text=""):
        return "All good."

    sends, reflections = _wire_worker(monkeypatch, pool, ok_run)

    assert await worker._process_one() is True
    assert reflections == [{"run_id": 7, "source": "dashboard"}]
    assert reflections_scopes[id(reflections)] == ["owner"]      # Quick 020


async def test_budget_apology_recorded_in_history(monkeypatch):
    """The BudgetExceeded apology must land in messages like every other reply
    path, so the next turn's history shows the user was already answered."""
    pool = FakePool()

    async def blow_budget(history, user_message, context_text=""):
        raise BudgetExceeded("token budget 150000 exceeded (151000)")

    sends, _ = _wire_worker(monkeypatch, pool, blow_budget)

    assert await worker._process_one() is True
    inserts = [(sql, args) for sql, args in pool.executed
               if "INSERT INTO messages" in sql]
    assert len(inserts) == 1
    _, args = inserts[0]
    assert args[0] == pool.thread_id
    assert args[1] == sends[0][1]  # exact apology text that went out


# ── Phase 12 (CTX-09a): neutral identifier rule ──────────────────────────

def test_no_first_client_identifiers_in_prompts_or_tools():
    texts = list(graph.DEFAULT_PROMPTS.values()) + [graph.run_sql.description]
    for t in texts:
        for bad in ("PascalCase", "MachineId", "DailyFuelReports"):
            assert bad not in t


def test_sql_rules_is_neutral_and_keeps_retry_and_select_only():
    r = graph.SQL_RULES
    assert "exactly as" in r and "lowercase" in r and "double-quote" in r
    assert "retry" in r and "SELECT only" in r
    assert graph.SQL_RULES in graph.DEFAULT_PROMPTS["data"]
    assert graph.SQL_RULES in graph.DEFAULT_PROMPTS["supervisor"]


def test_dashboard_prompt_clause_replaced_once():
    d = graph.DEFAULT_PROMPTS["dashboard"]
    assert "the business DB uses lowercase snake_case" not in d
    assert d.count(graph._NEW_DASH_CLAUSE) == 1


async def test_reflection_follows_the_memory_identity(monkeypatch):
    """Quick 020: a resolved People row alone no longer decides reflection; the
    run's memscope identity does. When the identity lookup fails the run is
    unattributed (held), so nothing is reflected, even with a person on file."""
    from app import memscope
    pool = FakePool(person_id=42)

    async def ok_run(history, user_message, context_text=""):
        return "All good."

    async def broken(t, pool=None):
        raise OSError("db blip")

    sends, reflections = _wire_worker(monkeypatch, pool, ok_run)
    monkeypatch.setattr(memscope, "_identify", broken)
    monkeypatch.setattr(memscope, "get_settings", lambda: SimpleNamespace(owner_number=""))
    assert await worker._process_one() is True
    assert sends and reflections == []
