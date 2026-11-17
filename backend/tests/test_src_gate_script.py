"""Phase 15 (15-16): pure helpers of scripts/src_gate.py (no database)."""
import json

import pytest

from scripts import src_gate as g


def test_parser_accepts_every_step():
    for step in g.STEPS:
        assert g.build_parser().parse_args([step]).step == step


def test_parser_options():
    a = g.build_parser().parse_args(
        ["test", "--name", "n", "--target", "cred:n", "--tables", "a, b,,c", "--run-id", "7",
         "--timeout", "30", "--after", "4"])
    assert a.run_id == 7 and a.timeout == 30.0 and a.after == 4
    assert g.split_tables(a.tables) == ["a", "b", "c"]


def test_parser_rejects_unknown_step():
    with pytest.raises(SystemExit):
        g.build_parser().parse_args(["nope"])


def test_parse_json_arg():
    assert g.parse_json_arg("") == {}
    assert g.parse_json_arg('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        g.parse_json_arg("[1]")
    with pytest.raises(ValueError):
        g.parse_json_arg("{bad")


def test_classify_flags():
    assert g.classify("Probe #1 ...")["ok"] is True
    r = g.classify("REFUSED: nope")
    assert r["refused"] and r["ok"]
    t = g.classify("TOOL ERROR: boom")
    assert t["tool_error"] and not t["ok"]


def test_shape_is_one_json_line_and_redacts_urls():
    line = g.shape({"ok": True, "text": "dsn mysql://u:topsecretpw@h:3306/d here", "n": {"a": 1}})
    assert "\n" not in line
    assert "topsecretpw" not in line
    assert json.loads(line)["n"] == {"a": 1}


def test_exit_code_and_finished():
    assert g.exit_code({"ok": True}) == 0
    assert g.exit_code({"ok": False}) == 1
    assert g.exit_code({}) == 0
    assert g.finished("partial") and not g.finished("running") and not g.finished("queued")


def test_emit_prints_single_line(capsys):
    assert g.emit({"ok": False, "error": "x"}) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out)["error"] == "x"


# Phase 15 gap closure (15-21)
def test_thread_for_ctx():
    assert g.thread_for_ctx("ui:owner") == "ui:owner"
    assert g.thread_for_ctx(None) == "ui:gate"
    assert g.thread_for_ctx("") == "ui:gate"


class _FakeConn:
    def __init__(self, tid):
        self.tid = tid

    async def fetchval(self, *a):
        return self.tid


class _FakePool:
    def __init__(self, tid):
        self.tid = tid

    def acquire(self):
        tid = self.tid

        class _Cm:
            async def __aenter__(s):
                return _FakeConn(tid)

            async def __aexit__(s, *a):
                return False
        return _Cm()


@pytest.mark.parametrize("tid,expected", [("ui:owner", "ui:owner"), (None, "ui:gate")])
def test_ctx_async_sets_phone(monkeypatch, tid, expected):
    import asyncio
    from app import db
    from app.agents import graph

    async def fake_pool():
        return _FakePool(tid)
    monkeypatch.setattr(db, "get_pool", fake_pool)
    args = g.build_parser().parse_args(["sync", "--run-id", "9"])

    async def go():
        await g._ctx_async(args)
        return graph.ctx_run_id.get(), graph.ctx_phone.get()
    rid, phone = asyncio.run(go())
    assert rid == 9 and phone == expected


def test_ctx_async_without_run_id_sets_nothing():
    import asyncio
    from app.agents import graph
    args = g.build_parser().parse_args(["sync"])
    before = graph.ctx_phone.get()
    asyncio.run(g._ctx_async(args))
    assert graph.ctx_phone.get() == before
