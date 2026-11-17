"""16-11: raw_sql_note and the run_sql hook."""
import json

import pytest

from app import db as appdb
from app import modeltools
from app.agents import graph
from app.guardrails import SQLGuardrailError


class _Pool:
    def __init__(self, val=None, exc=None):
        self.val, self.exc, self.sql = val, exc, None

    async def fetchval(self, sql, *a):
        self.sql = sql
        if self.exc:
            raise self.exc
        return self.val


class _NoFetchval:
    pass


def _pool(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(appdb, "get_pool", gp)


async def test_note_when_active(monkeypatch):
    p = _Pool(3)
    _pool(monkeypatch, p)
    out = await modeltools.raw_sql_note()
    assert out == modeltools.UNVERIFIED_NOTE
    assert out.startswith("\n\nUNVERIFIED: ")
    assert "unverified" in out and "governed measure" in out
    for w in ("model_measures", "status = 'active'"):
        assert w in p.sql
    for w in ("INSERT", "UPDATE", "DELETE", "DROP"):
        assert w not in p.sql


@pytest.mark.parametrize("val", [0, None])
async def test_note_empty_when_none_active(monkeypatch, val):
    _pool(monkeypatch, _Pool(val))
    assert await modeltools.raw_sql_note() == ""


async def test_note_get_pool_raises(monkeypatch):
    async def gp():
        raise OSError("down")
    monkeypatch.setattr(appdb, "get_pool", gp)
    assert await modeltools.raw_sql_note() == ""


async def test_note_no_fetchval(monkeypatch):
    _pool(monkeypatch, _NoFetchval())
    assert await modeltools.raw_sql_note() == ""


async def test_note_fetchval_raises(monkeypatch):
    _pool(monkeypatch, _Pool(exc=RuntimeError("x")))
    assert await modeltools.raw_sql_note() == ""


def _setup(monkeypatch, rows=None, note=modeltools.UNVERIFIED_NOTE, exc=None):
    logged = []

    async def rr(sql, limit=None):
        if exc:
            raise exc
        return rows if rows is not None else [{"n": 1}]

    async def gate(sql):
        return ""

    async def lt(name, args, result, start, **kw):
        logged.append(result)

    async def nt():
        if isinstance(note, Exception):
            raise note
        return note

    monkeypatch.setattr(graph, "run_readonly_sql", rr)
    monkeypatch.setattr(graph, "_rules_gate", gate)
    monkeypatch.setattr(graph, "_log_tool", lt)
    monkeypatch.setattr(modeltools, "raw_sql_note", nt)
    return logged


async def test_run_sql_appends_note(monkeypatch):
    logged = _setup(monkeypatch)
    out = await graph.run_sql.ainvoke({"sql": "SELECT 1 AS n"})
    assert out == json.dumps([{"n": 1}]) + modeltools.UNVERIFIED_NOTE
    assert logged == [out]


async def test_run_sql_truncated_keeps_note(monkeypatch):
    _setup(monkeypatch, rows=[{"v": "x" * 100} for _ in range(500)])
    out = await graph.run_sql.ainvoke({"sql": "SELECT 1"})
    assert "TRUNCATED" in out and out.endswith(modeltools.UNVERIFIED_NOTE)


async def test_run_sql_guardrail_no_note(monkeypatch):
    _setup(monkeypatch, exc=SQLGuardrailError("no"))
    out = await graph.run_sql.ainvoke({"sql": "x"})
    assert out.startswith("GUARDRAIL REJECTED") and "UNVERIFIED" not in out


async def test_run_sql_error_no_note(monkeypatch):
    _setup(monkeypatch, exc=RuntimeError("boom"))
    out = await graph.run_sql.ainvoke({"sql": "x"})
    assert out.startswith("SQL ERROR") and "UNVERIFIED" not in out


async def test_run_sql_rules_gate_no_note(monkeypatch):
    _setup(monkeypatch)

    async def gate(sql):
        return "RULES CHECK (query not run yet). x"
    monkeypatch.setattr(graph, "_rules_gate", gate)
    out = await graph.run_sql.ainvoke({"sql": "x"})
    assert out == "RULES CHECK (query not run yet). x"


async def test_run_sql_note_failure_harmless(monkeypatch):
    _setup(monkeypatch, note=RuntimeError("bad"))
    out = await graph.run_sql.ainvoke({"sql": "x"})
    assert out == json.dumps([{"n": 1}])
