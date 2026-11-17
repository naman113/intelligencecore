"""Phase 4 — investigation findings rendering (pure; no DB/LLM)."""

from app.investigation import _render


def test_render_includes_all_present_sources():
    note = _render({
        "question": "who hasn't submitted expenses?",
        "events": [{"source": "email", "actor": "Priya", "when": "2026-03-20T10:00",
                    "excerpt": "sent my sheet"}],
        "memory": [{"fact": "Priya handles Mumbai expenses",
                    "valid_at": None, "created_at": "2026-03-14T00:00:00"}],
        "sql": {"query": "SELECT 1", "rows": [{"name": "Amit", "submitted": False}]},
    })
    assert "INVESTIGATION: who hasn't submitted expenses?" in note
    assert "Priya" in note and "email" in note
    assert "knowledge graph" in note and "Mumbai" in note
    assert "recorded 2026-03-14" in note  # fact_line temporal stamp
    assert "business DB" in note and "Amit" in note


def test_render_handles_empty_and_sql_error():
    note = _render({"question": "q", "events": [], "memory": [],
                    "sql": {"query": "x", "error": "guardrail rejected: nope"}})
    assert "no matching events" in note
    assert "SQL error: guardrail rejected: nope" in note
    # memory section omitted entirely when empty
    assert "knowledge graph" not in note


def test_render_no_sql_section_when_not_run():
    note = _render({"question": "q", "events": [], "memory": [], "sql": None})
    assert "business DB" not in note


async def test_record_finding_touches_last_activity(monkeypatch):
    """An investigation link is case activity: it must reset the staleness
    clock (cases.last_activity_at), same as triage's link path — otherwise a
    case investigated yesterday can be closed as 'no activity for 21+ days'."""
    from app import investigation
    from app.playbook_tools import ctx_case_id

    executed = []

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Conn:
        def transaction(self):
            return _Tx()

        async def fetchval(self, sql, *args):
            executed.append(sql)
            return 42                       # synthetic event id

        async def execute(self, sql, *args):
            executed.append(sql)

    class _Acq:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Pool:
        def acquire(self):
            return _Acq()

    async def fake_pool():
        return Pool()

    monkeypatch.setattr(investigation, "get_pool", fake_pool)
    ctx_case_id.set(7)
    try:
        await investigation._record_finding("INVESTIGATION: findings")
    finally:
        ctx_case_id.set(None)

    assert any("INSERT INTO case_events" in s for s in executed)
    assert any("last_activity_at = now()" in s for s in executed)
