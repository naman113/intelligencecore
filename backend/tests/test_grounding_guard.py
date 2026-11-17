"""Quick 002: replies that state figures must rest on a data tool call in
their own turn.

History marks earlier replies with figures as past tool snapshots; the rolling
summary stops keeping figures; worker._guard_ungrounded re-asks once and marks
a still-ungrounded reply as not checked against data. No DB, LLM or network.
"""

import pytest

from app import worker


# ── history and summary ──────────────────────────────────────────────────

def test_note_reply_marks_only_replies_with_figures():
    assert worker._note_reply("Barges: 3,014.16 L").startswith(worker.HISTORY_REPLY_NOTE)
    assert worker._note_reply("Hello, how can I help?") == "Hello, how can I help?"
    marked = worker.UNVERIFIED_PREFIX + "Barges: 12 L"
    assert worker._note_reply(marked) == marked


class _HistConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        mid = args[2] if len(args) > 2 else None       # Quick 019 filter on $3
        rows = [r for r in self.rows if mid is None or "id" not in r
                or (r["id"] != mid and not (r["role"] == "user" and r["id"] > mid))]
        return rows[: args[1]]

    async def fetchval(self, sql, *args):
        return None


async def test_load_history_notes_assistant_figures_not_user_messages():
    rows = [{"role": "assistant", "content": "*24 Oct:* 96.67 L"},       # newest first
            {"role": "user", "content": "Loader use over 5 days?"},
            {"role": "assistant", "content": "Sure."}]
    _, hist = await worker._load_history(_HistConn(rows), "t")
    assert hist[0] == {"role": "assistant", "content": "Sure."}
    assert hist[1] == {"role": "user", "content": "Loader use over 5 days?"}
    assert hist[2]["content"] == worker.HISTORY_REPLY_NOTE + "*24 Oct:* 96.67 L"


def test_summary_keeps_no_figures_and_prefix_frames_it():
    assert "Do not copy business figures" in worker.THREAD_SUMMARY_PROMPT
    assert "not a data source" in worker.SUMMARY_HISTORY_PREFIX


# ── figure detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,user,want", [
    ("Barges used 3,014.16 L and others 509.78 L.", "barges vs others?", ["3,014.16", "509.78"]),
    ("Which of the 5 days do you mean?", "last 5 days", []),
    ("1. Fuel\n2. Certificates\n3) Alerts", "what can you do?", []),
    ("Hi! How can I help?", "hi", []),
    ("*1 certificate* is expired.", "how many expired?", ["1"]),
])
def test_figures(reply, user, want):
    assert worker._figures(reply, user) == want


@pytest.mark.parametrize("reply,want", [
    ("Barges 3,014.16 L, others 509.78 L.", True),
    ("I can list the top 100 customers for you.", False),   # one stray number
    ("Total: 12 L and again 12 L", False),                  # distinct figures count
    ("No numbers here.", False),
    ("Draft saved as action #431 for case #19.", False),    # system ids are not figures
])
def test_states_figures_needs_two_distinct(reply, want):
    assert worker._states_figures(reply, "q") is want


# ── guard ────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, tools):
        self.tools = tools

    async def fetch(self, sql, *args):
        return [{"tool": t} for t in self.tools]


@pytest.fixture
def env(monkeypatch):
    state = {"tools": [], "runs": [], "retry": "24 Oct: 112.44 L", "ground_retry": True,
             "raise": None}
    pool = _Pool(state["tools"])

    async def get_pool():
        return pool

    async def run(history, message, context_text=""):
        state["runs"].append((history, message, context_text))
        if state["raise"]:
            raise state["raise"]
        if state["ground_retry"]:
            state["tools"].append("run_sql")
        return state["retry"]

    monkeypatch.setattr(worker, "get_pool", get_pool)
    monkeypatch.setattr(worker.graph, "run", run)
    return state


Q = "Loader CAT 950 diesel for the last 5 days?"


async def test_reply_without_figures_passes_untouched(env):
    assert await worker._guard_ungrounded(1, "Which loader do you mean?", [], Q) == \
        "Which loader do you mean?"
    assert env["runs"] == []


async def test_reply_with_a_data_tool_passes_untouched(env):
    env["tools"].extend(["search_business_context", "run_sql"])
    assert await worker._guard_ungrounded(1, "24 Oct: 96.67 L", [], Q) == "24 Oct: 96.67 L"
    assert env["runs"] == []


async def test_ungrounded_reply_is_reasked_and_grounded_retry_wins(env):
    hist = [{"role": "user", "content": "earlier"}]
    out = await worker._guard_ungrounded(7, "24 Oct: 45.2 L", hist, Q, "mem")
    assert out == "24 Oct: 112.44 L"
    (h, message, ctx), = env["runs"]
    assert h is hist and ctx == "mem"
    assert message == Q + worker._reask_note(Q)


async def test_progress_refused_and_memory_tools_are_no_evidence(env):
    env["tools"].extend(["send_progress", "run_sql:refused", "remember_fact", "use_skill"])
    await worker._guard_ungrounded(1, "24 Oct: 45.2 L", [], Q)
    assert len(env["runs"]) == 1


async def test_still_ungrounded_retry_is_marked_unverified(env):
    env["ground_retry"] = False
    out = await worker._guard_ungrounded(1, "24 Oct: 45.2 L", [], Q)
    assert out == worker.UNVERIFIED_PREFIX + "24 Oct: 112.44 L"


async def test_capability_answer_with_one_number_is_not_reasked(env):
    out = await worker._guard_ungrounded(1, "I answer from your database, e.g. the top 100 rows.", [], "hi")
    assert out.startswith("I answer") and env["runs"] == []


async def test_retry_without_figures_is_accepted(env):
    env["ground_retry"], env["retry"] = False, "I need the machine's plate to look that up."
    out = await worker._guard_ungrounded(1, "24 Oct: 45.2 L", [], Q)
    assert out == "I need the machine's plate to look that up."


async def test_failed_reask_marks_the_first_reply(env):
    env["raise"] = RuntimeError("model down")
    out = await worker._guard_ungrounded(1, "24 Oct: 45.2 L", [], Q)
    assert out == worker.UNVERIFIED_PREFIX + "24 Oct: 45.2 L"


async def test_already_marked_reply_is_not_reasked(env):
    marked = worker.UNVERIFIED_PREFIX + "24 Oct: 45.2 L"
    assert await worker._guard_ungrounded(1, marked, [], Q) == marked
    assert env["runs"] == []


def test_guard_runs_before_the_claim_guards_and_skips_reflection():
    import inspect

    src = inspect.getsource(worker._process_one)
    assert src.index("_guard_ungrounded(") < src.index("_guard_false_send(")
    assert "reply.startswith(UNVERIFIED_PREFIX)" in src
