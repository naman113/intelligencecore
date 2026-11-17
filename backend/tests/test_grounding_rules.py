"""GROUNDING_RULES injection + send_document stale-artifact provenance
(agents/graph.py).

Two failure classes from the TC-006 incident:
- a context-starved delegate FABRICATED an invoice instead of refusing —
  GROUNDING_RULES is prepended to EVERY agent's prompt at build time (_mk), so
  a dashboard prompt override can never strip it;
- asked to fix the fake, the agent RE-SENT the same stale carried-over file
  with zero tool calls and claimed success — send_document now appends a
  provenance note when the file was copied in by sandbox.carry_over and
  nothing in THIS run rewrote it (the carried record pins the copy's
  mtime_ns; the check is a same-file equality, never a wall-clock
  comparison, so intake files that arrive WITH the message and clock skew
  between gateway/sandbox containers can never misfire it).

Pure logic: create_react_agent/_make_model/whatsapp/_log_tool are faked;
no DB/LLM/network."""

import os
import time
from datetime import datetime, timezone

from app import sandbox
from app.agents import graph

AGENT_KEYS = ("supervisor", "data", "analysis", "document", "drawing")
# Quick 012: never-stored tails after the editable prompt (the supervisor's SQL
# routing rule while delegate_heavy_sql is on, the Data Agent's output contract).
TAILS = {"supervisor": "\n\n" + graph._supervisor_rules(   # Quick 029: routing rules, then SQL routing
             {**graph.DEFAULT_CONFIGS["supervisor"], "prompt": "OVERRIDE::supervisor"})
         + "\n\n" + graph.SQL_ROUTING_RULE,
         "data": "\n\n" + graph.DATA_OUTPUT_CONTRACT}


def _layout(k, middle=""):
    return graph.GROUNDING_RULES + middle + "\n\n" + f"OVERRIDE::{k}" + TAILS.get(k, "")


# ── GROUNDING_RULES content pins ─────────────────────────────────────────

def test_grounding_rules_content_pins():
    # Each needle anchors one incident failure class: fabrication, silent
    # guessing instead of refusal, claiming to have read an image, and the
    # identical-failure retry spiral.
    for needle in ("Never invent business values",
                   "MISSING DATA:",
                   "You cannot see images",
                   "stop retrying"):
        assert needle in graph.GROUNDING_RULES, f"missing pin: {needle!r}"


# ── build-time injection: no override can strip the rules ────────────────

class _FakeModel:
    """Not named ChatAnthropic → _cached_prompt passes the plain string
    through, so the recorded prompt is directly assertable."""


def test_build_prepends_grounding_rules_to_every_agent(monkeypatch):
    recorded = []

    def fake_cra(model, tools, prompt=None):
        recorded.append(prompt)
        return object()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: _FakeModel())

    # Simulate a dashboard override for EVERY agent: the stored prompt is the
    # override alone — no grounding text anywhere in the config.
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
               for k in AGENT_KEYS}
    try:
        graph._build(configs)
    finally:
        graph.reset_agents()   # drop the fake agents the build cached

    assert len(recorded) == len(AGENT_KEYS)
    assert set(recorded) == {_layout(k) for k in AGENT_KEYS}, \
        "every agent's prompt = GROUNDING_RULES + blank line + its config prompt (+ its tail)"
    for prompt in recorded:
        assert prompt.startswith(graph.GROUNDING_RULES)
    # The stored config prompt stays CLEAN — the rules are prepended at build
    # time only, so the dashboard editor never shows (or can delete) them.
    for k in AGENT_KEYS:
        assert configs[k]["prompt"] == f"OVERRIDE::{k}"


def _record_build(monkeypatch, configs, skills_index):
    """Run _build with faked create_react_agent; prompts keyed by agent."""
    recorded = {}

    def fake_cra(model, tools, prompt=None):
        for k in AGENT_KEYS:
            if f"OVERRIDE::{k}" in (prompt or ""):
                recorded[k] = prompt
        return object()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: _FakeModel())
    try:
        graph._build(configs, skills_index=skills_index)
    finally:
        graph.reset_agents()
    assert set(recorded) == set(AGENT_KEYS)
    return recorded


def test_skills_index_injects_between_rules_and_prompt_supervisor_only(monkeypatch):
    """The enabled-skills index rides the SUPERVISOR prompt only, BETWEEN
    GROUNDING_RULES and the editable config prompt — like the rules, it is
    never stored, so no dashboard override can strip or fake it. Delegates see
    only their task string; leaking the catalog there would invite them to
    call use_skill on procedures the supervisor never chose."""
    marker = "SKILLS-INDEX-MARKER::catalog"
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
               for k in AGENT_KEYS}
    recorded = _record_build(monkeypatch, configs, marker)

    sup = recorded["supervisor"]
    assert sup.startswith(graph.GROUNDING_RULES), \
        "the index must never displace GROUNDING_RULES from the front"
    assert sup.index(graph.GROUNDING_RULES) < sup.index(marker) \
        < sup.index("OVERRIDE::supervisor"), \
        "order must be rules, then index, then editable prompt"

    for k in ("data", "analysis", "document", "drawing"):
        assert marker not in recorded[k], \
            f"the skills index leaked into the {k} delegate's prompt"
        assert recorded[k] == _layout(k), \
            "delegate prompts keep the exact rules+prompt(+tail) layout"


def test_empty_skills_index_reproduces_the_exact_old_layout(monkeypatch):
    """No enabled skills (or any skills failure — index_lines fails soft to
    '') must yield byte-identical prompts to the pre-skills build: no stray
    separators, so prompt caching stays stable."""
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
               for k in AGENT_KEYS}
    recorded = _record_build(monkeypatch, configs, "")
    for k in AGENT_KEYS:
        assert recorded[k] == _layout(k)
    # Quick 012 switch off: the supervisor is byte-identical to the pre-012 layout.
    configs["supervisor"]["delegate_heavy_sql"] = False
    recorded = _record_build(monkeypatch, configs, "")
    assert recorded["supervisor"] == (graph.GROUNDING_RULES + "\n\n" + "OVERRIDE::supervisor"
                                      + "\n\n" + graph._supervisor_rules(configs["supervisor"]))


def test_default_configs_do_not_bake_in_grounding_rules():
    # If the rules leaked into DEFAULT_PROMPTS, a dashboard override would
    # replace (i.e. remove) them — prepending in _mk only is the guarantee.
    for key, cfg in graph.DEFAULT_CONFIGS.items():
        assert graph.GROUNDING_RULES not in (cfg["prompt"] or ""), \
            f"GROUNDING_RULES must not be stored in DEFAULT_CONFIGS[{key!r}]"


# ── default prompt content pins (delegation contract) ────────────────────

def test_supervisor_prompt_pins_self_contained_delegation():
    sup = graph.DEFAULT_PROMPTS["supervisor"]
    assert "self-contained" in sup, \
        "supervisor must be told delegates see ONLY the task string"
    assert "MISSING DATA" in sup, \
        "supervisor must relay/handle a delegate's MISSING DATA refusal"


def test_document_prompt_pins_grounded_replication():
    doc = graph.DEFAULT_PROMPTS["document"]
    assert "get_document" in doc, \
        "document agent must be pointed at get_document for document_id refs"
    assert "cannot replicate" in doc, \
        "document agent must know house-style rendering cannot clone an original"


# ── send_document stale-artifact provenance ──────────────────────────────

def _stale_note(filename: str) -> str:
    return (f" (note: {filename} was produced by an earlier run, not this one "
            "— if the user asked for a new or changed version, create it "
            "first, then re-send)")


RUN, PREV = 77, 42


def _wire(monkeypatch, tmp_path):
    # Real scratch dirs under tmp_path (run_77, run_42, ...) so the REAL
    # sandbox.carry_over runs and records provenance; fresh carried record.
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "_carried", {})
    calls = []

    async def fake_log(name, args, result, start, refused=False):
        calls.append({"name": name, "args": args, "result": result})

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    sends = []

    async def fake_send(phone, path, caption):
        sends.append((phone, path, caption))
        return True

    monkeypatch.setattr(graph.whatsapp, "send_document", fake_send)
    return calls, sends


def _carry(tmp_path, name, content=b"%PDF stale"):
    """Create `name` in a PREVIOUS run's /work and carry it into RUN's via the
    real carry_over — the only path that records carried-file provenance."""
    prev_dir = tmp_path / f"run_{PREV}"
    prev_dir.mkdir(parents=True, exist_ok=True)
    f = prev_dir / name
    f.write_bytes(content)
    _backdate(f)                      # carried files genuinely predate the run
    assert sandbox.carry_over(RUN, [PREV]) >= 1
    return tmp_path / f"run_{RUN}" / name


async def _send(filename, *, run_started, phone="911234567890"):
    fn = graph.send_document.coroutine or graph.send_document.func
    tok_run = graph.ctx_run_id.set(RUN)
    tok_phone = graph.ctx_phone.set(phone)
    tok_started = graph.ctx_run_started.set(run_started)
    try:
        return await fn(filename=filename, caption="")
    finally:
        graph.ctx_run_id.reset(tok_run)
        graph.ctx_phone.reset(tok_phone)
        graph.ctx_run_started.reset(tok_started)


def _backdate(path, seconds=3600):
    past = time.time() - seconds
    os.utime(path, (past, past))


async def test_send_document_flags_stale_carried_over_file(monkeypatch, tmp_path):
    _, sends = _wire(monkeypatch, tmp_path)
    _carry(tmp_path, "inv.pdf")

    result = await _send("inv.pdf", run_started=datetime.now(timezone.utc))

    assert len(sends) == 1, "the file IS still sent — the note is a warning"
    assert result == "sent" + _stale_note("inv.pdf"), \
        "an untouched carried-over file must earn the exact provenance note"


async def test_send_document_fresh_file_has_no_note(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    f = sandbox.scratch_dir(RUN) / "inv.pdf"
    f.write_bytes(b"%PDF fresh")            # produced by THIS run

    result = await _send("inv.pdf", run_started=datetime.now(timezone.utc))

    assert result == "sent", \
        "a file made this run must come back as bare 'sent' — no noise"


async def test_send_document_failsoft_without_run_started(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    _carry(tmp_path, "inv.pdf")

    # ctx_run_started unset (ad-hoc invocations, older tests): never guess.
    result = await _send("inv.pdf", run_started=None)

    assert result == "sent"


async def test_send_document_dashboard_branch_gets_note_too(monkeypatch, tmp_path):
    _, sends = _wire(monkeypatch, tmp_path)
    _carry(tmp_path, "report.pdf")

    result = await _send("report.pdf", run_started=datetime.now(timezone.utc),
                         phone="dashboard")

    assert sends == [], "non-WhatsApp intake never hits the connector"
    assert result.startswith("saved to the workspace as report.pdf")
    assert result.endswith(_stale_note("report.pdf")), \
        "the stale note rides the workspace branch as well"


async def test_send_document_no_note_for_file_that_arrived_with_this_message(
        monkeypatch, tmp_path):
    """Inbound media / dashboard uploads / inbox files are written into the
    run's /work at INTAKE time, before the worker claims the run — often well
    before, on a busy thread. They must never be called 'produced by an
    earlier run', however old their mtime looks (intake-to-claim lag, or a
    drifted container clock). carry_over never overwrites an existing file,
    so an intake file never enters the carried record — even when an earlier
    run happens to hold a file with the same name."""
    _wire(monkeypatch, tmp_path)
    intake = sandbox.scratch_dir(RUN) / "upload.pdf"
    intake.write_bytes(b"%PDF just uploaded")
    _backdate(intake)                     # looks ancient to any wall clock
    prev_dir = tmp_path / f"run_{PREV}"
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / "upload.pdf").write_bytes(b"%PDF older twin")
    sandbox.carry_over(RUN, [PREV])       # skips the existing intake file

    result = await _send("upload.pdf", run_started=datetime.now(timezone.utc))

    assert result == "sent", \
        "a file that arrived WITH the current message must never be flagged"


async def test_send_document_regenerated_carried_file_loses_note(
        monkeypatch, tmp_path):
    """Rebuilding a carried file this run (create_* tools unlink first, the
    sandbox rewrites in place) changes its mtime_ns away from the recorded
    copy-time value, so the note clears — even if the rewriting clock is far
    behind the gateway's (Docker Desktop drift): the check is a same-file
    equality, not a wall-clock comparison."""
    _wire(monkeypatch, tmp_path)
    path = _carry(tmp_path, "inv.pdf")
    path.unlink()                              # what _generate_document does
    path.write_bytes(b"%PDF rebuilt this run")
    _backdate(path, seconds=7200)              # sandbox clock hours behind

    result = await _send("inv.pdf", run_started=datetime.now(timezone.utc))

    assert result == "sent", \
        "a carried file REBUILT this run must not keep the stale note"


# ── ctx_run_started wiring: worker (user runs) and playbooks ─────────────
# _stale_file_note is deliberately fail-soft (silent when ctx_run_started is
# None), so deleting either production set() would disable the provenance
# note with the whole suite green. These pin the wiring itself.

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _WConn:
    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 9, "thread_id": "911234567890", "message_id": 3,
                    "kind": "user", "playbook_id": None, "case_id": None,
                    "expectation_id": None}
        if "SELECT content FROM messages" in sql:
            return {"content": "resend that invoice"}
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return None


class _WPool:
    def __init__(self):
        self.conn = _WConn()

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return None


async def test_worker_stamps_ctx_run_started_on_claimed_user_run(monkeypatch):
    from app import worker

    pool = _WPool()
    seen = {}

    async def fake_get_pool():
        return pool

    async def none1(a, b):
        return None

    async def none2(a, b, c):
        return None

    async def fake_assemble(content, thread_id, sender_name=None):
        return "", {}

    async def fake_run(history, user_message, context_text=""):
        # Capture what the tools would see mid-run.
        seen["run_id"] = graph.ctx_run_id.get()
        seen["phone"] = graph.ctx_phone.get()
        seen["started"] = graph.ctx_run_started.get()
        return "ok"

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

    before = datetime.now(timezone.utc)
    assert await worker._process_one() is True
    after = datetime.now(timezone.utc)

    assert seen["run_id"] == 9
    assert seen["phone"] == "911234567890"
    started = seen["started"]
    assert started is not None, \
        "worker must stamp ctx_run_started alongside ctx_run_id/ctx_phone"
    assert started.tzinfo is not None, "the stamp must be tz-aware UTC"
    assert before <= started <= after


async def test_playbook_run_stamps_fresh_ctx_run_started(monkeypatch):
    """contextvars persist across the worker loop's iterations: a playbook run
    on a consumer task that previously processed a kind='user' run must not
    inherit that run's ctx_run_started — run_playbook_task stamps its own."""
    from app import playbooks

    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    graph.ctx_run_started.set(stale)     # leftover from a prior user run
    seen = {}

    class _PBPool:
        async def fetchrow(self, sql, *args):
            return {"id": 5, "name": "pb", "status": "active", "goal": "g",
                    "escalation": None, "tool_grants": [],
                    "autonomy_tier": "draft"}

        async def execute(self, sql, *args):
            return None

    async def fake_get_pool():
        return _PBPool()

    async def fake_ctx(task, pb):
        return "CTX"

    class _Agent:
        async def ainvoke(self, payload, config=None):
            seen["started"] = graph.ctx_run_started.get()
            return {"messages": [type("_M", (), {"content": "done"})()]}

    monkeypatch.setattr(playbooks, "get_pool", fake_get_pool)
    monkeypatch.setattr(playbooks, "_assemble_context", fake_ctx)
    monkeypatch.setattr(playbooks, "_model", lambda: object())
    monkeypatch.setattr(playbooks, "create_react_agent",
                        lambda model, tools, prompt=None: _Agent())
    monkeypatch.setattr(playbooks.graph, "_callbacks", lambda: {})

    before = datetime.now(timezone.utc)
    await playbooks.run_playbook_task(
        {"id": 88, "playbook_id": 5, "case_id": None, "expectation_id": None})

    started = seen["started"]
    assert started is not None and started != stale, \
        "playbook run must stamp its OWN start, not a prior user run's"
    assert started.tzinfo is not None
    assert started >= before
