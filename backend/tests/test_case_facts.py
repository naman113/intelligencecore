"""Supersedable case facts + case aliases (cases_util) — pure logic, no DB.

Append-only notes cannot handle "₹18 lakh pending" later corrected to
"10 lakh": the reactive context would show both and the model would have to
guess. `case_facts` is latest-wins-with-history (graphiti-style t_valid /
t_invalid collapsed into one Postgres table): every INSERT supersedes ALL
currently-unsuperseded rows of the same (case_id, field), so exactly one row
per field is ever current while the full history stays queryable.

`aliases` is the misattachment fix: triage once attached events to cases on
title words alone; a case now accumulates the identifiers events actually use
(invoice number, doc ref, nickname), guarded in SQL against duplicates and
unbounded growth.

The FactsConn double interprets the exact INSERT/UPDATE the helper issues
against an in-memory table, so the supersession CHAIN semantics (not just the
SQL text) are pinned."""

import pytest

from app import cases_util

CASE = 500  # deliberately far from fact ids so an int arg is unambiguous


class FactsConn:
    """In-memory case_facts table driven by the helper's own SQL."""

    def __init__(self):
        self.rows = []          # id, case_id, field, value, source_event_id, superseded_by
        self.next_id = 1
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        assert "INSERT INTO case_facts" in sql, f"unexpected fetchval: {sql}"
        case_id, field, value, source_event_id = args
        row = {"id": self.next_id, "case_id": case_id, "field": field,
               "value": value, "source_event_id": source_event_id,
               "superseded_by": None}
        self.rows.append(row)
        self.next_id += 1
        return row["id"]

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        if "pg_advisory_xact_lock" in sql:
            # concurrency guard: serializes same-(case, field) writers so two
            # overlapping insert-then-supersede pairs can't both stay current;
            # keyed on (field, case_id) — both must reach the lock call
            assert len(args) == 2, "lock must be keyed on (case_id, field)"
            return "SELECT 1"
        assert "UPDATE case_facts" in sql, f"unexpected execute: {sql}"
        # the guards the contract requires, pinned in the SQL itself
        assert "superseded_by IS NULL" in sql, \
            "must only supersede CURRENT rows (history keeps its pointers)"
        assert "id !=" in sql or "id <>" in sql, \
            "the new row must not supersede itself"
        field = next(a for a in args if isinstance(a, str))
        new_id = self.rows[-1]["id"]
        assert new_id in [a for a in args if isinstance(a, int)], \
            "UPDATE must reference the freshly inserted id"
        case_id = next(a for a in args if isinstance(a, int) and a != new_id)
        for r in self.rows:
            if (r["case_id"] == case_id and r["field"] == field
                    and r["superseded_by"] is None and r["id"] != new_id):
                r["superseded_by"] = new_id

    def current(self, field):
        return [r for r in self.rows
                if r["field"] == field and r["superseded_by"] is None]


# ── record_case_fact: supersession chain ──────────────────────────────────

async def test_supersession_chain_marks_each_predecessor_once():
    """A→B→C: A was superseded when B landed and KEEPS that pointer (history);
    C's arrival marks only B. Exactly one current row survives."""
    conn = FactsConn()
    a = await cases_util.record_case_fact(conn, CASE, "amount_pending", "18 lakh")
    b = await cases_util.record_case_fact(conn, CASE, "amount_pending", "≈18 lakh")
    c = await cases_util.record_case_fact(conn, CASE, "amount_pending", "10 lakh")
    assert None not in (a, b, c) and len({a, b, c}) == 3

    by_id = {r["id"]: r for r in conn.rows}
    assert by_id[a]["superseded_by"] == b, "A points at B, not at C"
    assert by_id[b]["superseded_by"] == c
    assert by_id[c]["superseded_by"] is None
    assert [r["id"] for r in conn.current("amount_pending")] == [c], \
        "only the latest value is current"


async def test_supersession_is_per_field_and_per_case():
    conn = FactsConn()
    await cases_util.record_case_fact(conn, CASE, "amount_pending", "18 lakh")
    await cases_util.record_case_fact(conn, CASE, "due_date", "Sep 15")
    await cases_util.record_case_fact(conn, 501, "amount_pending", "7 lakh")
    await cases_util.record_case_fact(conn, CASE, "amount_pending", "10 lakh")

    assert len(conn.current("due_date")) == 1, "other fields untouched"
    currents = conn.current("amount_pending")
    assert {(r["case_id"], r["value"]) for r in currents} == \
        {(CASE, "10 lakh"), (501, "7 lakh")}, "other cases' facts untouched"


# ── record_case_fact: normalization and refusal ───────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Amount Pending!", "amount_pending"),
    ("  DUE date ", "due_date"),
    ("payment-status", "payment_status"),
    ("amount   (INR)", "amount_inr"),
])
async def test_field_normalizes_to_snake_case(raw, expected):
    conn = FactsConn()
    assert await cases_util.record_case_fact(conn, CASE, raw, "x") is not None
    assert conn.rows[-1]["field"] == expected


async def test_field_caps_at_64():
    conn = FactsConn()
    await cases_util.record_case_fact(conn, CASE, "a" * 200, "x")
    assert len(conn.rows[-1]["field"]) == 64


class NeverConn:
    async def fetchval(self, *a):
        raise AssertionError("must not write")

    async def execute(self, *a):
        raise AssertionError("must not write")


@pytest.mark.parametrize("field", ["", "   ", "!!!", "—", None])
async def test_unusable_field_writes_nothing(field):
    assert await cases_util.record_case_fact(NeverConn(), CASE, field, "x") is None


async def test_blank_value_writes_nothing():
    assert await cases_util.record_case_fact(NeverConn(), CASE, "amount", "") is None
    assert await cases_util.record_case_fact(NeverConn(), CASE, "amount", "   ") is None


async def test_falsy_case_id_writes_nothing():
    assert await cases_util.record_case_fact(NeverConn(), 0, "amount", "x") is None
    assert await cases_util.record_case_fact(NeverConn(), None, "amount", "x") is None


async def test_value_capped_and_stripped():
    conn = FactsConn()
    await cases_util.record_case_fact(conn, CASE, "notes", "  " + "v" * 999)
    assert len(conn.rows[-1]["value"]) == 300
    assert not conn.rows[-1]["value"].startswith(" ")


async def test_source_event_id_passes_through():
    conn = FactsConn()
    await cases_util.record_case_fact(conn, CASE, "amount", "x", source_event_id=136)
    assert conn.rows[-1]["source_event_id"] == 136
    await cases_util.record_case_fact(conn, CASE, "amount", "y")
    assert conn.rows[-1]["source_event_id"] is None, "default is NULL"


# ── add_alias ─────────────────────────────────────────────────────────────

class AliasConn:
    def __init__(self, result="UPDATE 1"):
        self.result = result
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return self.result


async def test_add_alias_sql_guards_dupes_and_growth():
    conn = AliasConn()
    assert await cases_util.add_alias(conn, CASE, "TC-006") is True
    (sql, args), = conn.executed
    assert "array_append" in sql
    assert "cardinality(aliases) < 12" in sql, "growth cap must live in the SQL"
    assert "lower(a) = lower($2)" in sql, \
        "case-insensitive dupe guard must live in the SQL (no read-then-write race)"
    assert "TC-006" in args


async def test_add_alias_false_when_guard_rejects():
    assert await cases_util.add_alias(AliasConn("UPDATE 0"), CASE, "TC-006") is False


async def test_add_alias_trims_and_caps():
    conn = AliasConn()
    await cases_util.add_alias(conn, CASE, "  " + "x" * 200)
    (_, args), = conn.executed
    alias = next(a for a in args if isinstance(a, str))
    assert len(alias) == 80 and not alias.startswith(" ")


async def test_add_alias_refuses_blank_or_no_case():
    class Never:
        async def execute(self, *a):
            raise AssertionError("must not write")
    assert await cases_util.add_alias(Never(), CASE, "   ") is False
    assert await cases_util.add_alias(Never(), CASE, None) is False
    assert await cases_util.add_alias(Never(), 0, "TC-006") is False


# ── current_facts ─────────────────────────────────────────────────────────

class FetchOnly:
    """Both an asyncpg pool and an acquired conn expose .fetch — the helper
    must work with either (contextassembler passes the pool)."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def _src_row(field="amount_pending", value="10 lakh", observed_at=None,
             source_event_id=None, source=None, actor_name=None,
             actor_raw=None):
    """A raw current_facts query row: the fact columns plus the LEFT-JOINed
    provenance columns (events e ON e.id = f.source_event_id, people p ON
    p.id = e.actor_id) the helper composes `src` from in Python — the seam
    these tests define."""
    return {"field": field, "value": value, "observed_at": observed_at,
            "source_event_id": source_event_id, "source": source,
            "actor_name": actor_name, "actor_raw": actor_raw}


async def test_current_facts_selects_only_unsuperseded():
    handle = FetchOnly([_src_row(value="10 lakh")])
    out = await cases_util.current_facts(handle, CASE)
    (sql, args), = handle.calls
    assert "superseded_by IS NULL" in sql, "history rows must never surface"
    assert "ORDER BY field" in sql
    assert args[0] == CASE
    assert out and out[0]["value"] == "10 lakh"


async def test_current_facts_limit_is_a_parameter():
    handle = FetchOnly()
    await cases_util.current_facts(handle, CASE, limit=6)
    (sql, args), = handle.calls
    assert 6 in args, "limit must reach the query"


# ── current_facts: provenance (src) ───────────────────────────────────────
# L2: a fact the assistant reports must say WHO put it there — a fictional
# payment quoted from an unattributed fact is the incident under test. src
# shapes: 'unattributed' (no source event), '<source>:<name>' (speaker's
# registry name, else the raw actor, else 'unknown'), and bare '<source>'
# for actor-less system/db rows (never 'system:unknown' noise).


async def test_current_facts_joins_event_and_person_for_src():
    handle = FetchOnly()
    await cases_util.current_facts(handle, CASE)
    (sql, _), = handle.calls
    assert "LEFT JOIN events" in sql, "src needs the source event"
    assert "LEFT JOIN people" in sql, "src needs the speaker's registry name"


async def test_src_unattributed_without_source_event():
    out = await cases_util.current_facts(FetchOnly([_src_row()]), CASE)
    assert out[0]["src"] == "unattributed"


async def test_src_names_the_speaker():
    out = await cases_util.current_facts(FetchOnly(
        [_src_row(source_event_id=136, source="whatsapp",
                  actor_name="Vinayak")]), CASE)
    assert out[0]["src"] == "whatsapp:Vinayak"


async def test_src_falls_back_to_raw_actor_then_unknown():
    rows = [_src_row(field="a", source_event_id=1, source="whatsapp",
                     actor_raw="919876543210"),
            _src_row(field="b", source_event_id=2, source="whatsapp")]
    out = await cases_util.current_facts(FetchOnly(rows), CASE)
    assert {f["field"]: f["src"] for f in out} == \
        {"a": "whatsapp:919876543210", "b": "whatsapp:unknown"}


async def test_src_collapses_pure_system_rows():
    """An actor-less system/db row IS its source; one WITH an actor keeps the
    speaker."""
    rows = [_src_row(field="a", source_event_id=1, source="system"),
            _src_row(field="b", source_event_id=2, source="db"),
            _src_row(field="c", source_event_id=3, source="system",
                     actor_name="Naman")]
    out = await cases_util.current_facts(FetchOnly(rows), CASE)
    assert {f["field"]: f["src"] for f in out} == \
        {"a": "system", "b": "db", "c": "system:Naman"}
