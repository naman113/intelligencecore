"""Phase 8: owner edit and hide routes."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main
from app import contextstore, ownerapi


def norm(s):
    return " ".join(s.split())


class EditPool:
    def __init__(self, ret):
        self.ret, self.calls = ret, []

    async def fetchrow(self, sql, *args):
        self.calls.append((norm(sql), args))
        return self.ret


def _wire_edit(monkeypatch, ret):
    pool = EditPool(ret)

    async def gp():
        return pool
    monkeypatch.setattr(ownerapi, "db", SimpleNamespace(get_pool=gp))
    return pool


async def test_edit_ok(monkeypatch):
    pool = _wire_edit(monkeypatch, {"id": 7})
    assert await ownerapi.edit_proposal(7, {"text": "Customers who buy from us."}) == {"edited": 7}
    sql, args = pool.calls[0]
    assert "WHERE id = $1 AND kind = 'context_change' AND status = 'proposed'" in sql
    for k in ("'text'", "'edited_by'", "'agent_text'"):
        assert k in sql
    assert args == (7, "Customers who buy from us.")


async def test_edit_409(monkeypatch):
    _wire_edit(monkeypatch, None)
    with pytest.raises(HTTPException) as e:
        await ownerapi.edit_proposal(7, {"text": "ok"})
    assert e.value.status_code == 409


@pytest.mark.parametrize("text", ["", "   ", "x" * 1501, "a​b"])
async def test_edit_422(monkeypatch, text):
    pool = _wire_edit(monkeypatch, {"id": 1})
    with pytest.raises(HTTPException) as e:
        await ownerapi.edit_proposal(1, {"text": text})
    assert e.value.status_code == 422 and pool.calls == []


def test_routes_wired_with_key():
    found = {}
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                found[s.path] = inh + list(s.dependencies)
    for p in ("/api/actions/{action_id}/edit", "/api/schema/tables/hide"):
        assert any(getattr(d, "dependency", None) is main._require_ops_key
                   for d in found[p]), p
        assert not p.startswith("/api/context")


# -- hide --
REF = "business_db.public.customers"


class HConn:
    def __init__(self, log, rejected):
        self.log, self.rejected = log, rejected

    def transaction(self):
        class T:
            async def __aenter__(s):
                return None

            async def __aexit__(s, *a):
                return False
        return T()

    async def execute(self, sql, *args):
        self.log.append((norm(sql), args))

    async def fetch(self, sql, *args):
        self.log.append((norm(sql), args))
        return [{"id": i} for i in self.rejected]

    async def fetchval(self, sql, *args):
        self.log.append((norm(sql), args))
        return 99


class HPool:
    def __init__(self, table_row=None, rejected=()):
        self.log, self.table_row, self.rejected = [], table_row, rejected

    async def fetchrow(self, sql, *args):
        return self.table_row

    def acquire(self):
        conn = HConn(self.log, self.rejected)

        class A:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False
        return A()


def _wire_hide(monkeypatch, pool, primary=None, resolved=None,
               truncated=False, raises=None):
    async def gp():
        return pool

    async def pp():
        return primary

    async def rs(ref, cat=None):
        return resolved
    monkeypatch.setattr(contextstore, "get_pool", gp)
    from app import people, schemadiscovery
    monkeypatch.setattr(people, "primary_person", pp)
    monkeypatch.setattr(schemadiscovery, "resolve", rs)

    async def rc(ref):
        if raises is not None:
            raise raises
        return resolved, truncated
    monkeypatch.setattr(schemadiscovery, "resolve_checked", rc)


async def test_hide_existing(monkeypatch):
    pool = HPool({"id": 5, "source_id": 1}, rejected=(3, 4))
    _wire_hide(monkeypatch, pool, primary={"id": 2})
    out = await contextstore.set_table_hidden(REF, True)
    assert out == {"ref": REF, "hidden": True, "rejected": 2, "action_id": 99}
    sqls = [s for s, _ in pool.log]
    assert len(sqls) == 5
    assert sqls[0].startswith("UPDATE context_tables")
    assert sqls[1].startswith("UPDATE context_columns")
    assert sqls[2].startswith("UPDATE business_context")
    assert sqls[3].startswith("UPDATE actions SET status = 'rejected'")
    assert sqls[4].startswith("INSERT INTO actions")
    assert all(" LIKE " not in s for s in sqls)
    assert "left(ref, length($1) + 1) = $1 || '.'" in sqls[2]
    assert "'operator', 'executed'" in sqls[4]
    payload = json.loads(pool.log[4][1][0])
    assert payload["op"] == "hide" and payload["rejected_action_ids"] == [3, 4]
    assert pool.log[4][1][1] == 2  # primary-person fallback


async def test_unhide_rejects_nothing(monkeypatch):
    pool = HPool({"id": 5, "source_id": 1})
    _wire_hide(monkeypatch, pool)
    out = await contextstore.set_table_hidden(REF, False)
    assert out["hidden"] is False and out["rejected"] == 0
    sqls = [s for s, _ in pool.log]
    assert len(sqls) == 4 and not any("rejected" in s for s in sqls[:3])
    assert json.loads(pool.log[3][1][0])["op"] == "unhide"
    assert pool.log[0][1] == (5, "hidden", "active")
    assert pool.log[2][1][1:] == ("hidden", "approved")
    assert pool.log[3][1][1] is None


async def test_hide_uncatalogued_resolves(monkeypatch):
    pool = HPool(None)
    info = {"kind": "table", "source": "business_db", "schema": "public",
            "table": "customers", "object_kind": "table"}
    _wire_hide(monkeypatch, pool, resolved=info)
    out = await contextstore.set_table_hidden(REF, True, 4)
    assert out["action_id"] == 99
    assert any(s.startswith("INSERT INTO context_tables") and "'hidden'" in s
               for s, _ in pool.log)


async def test_hide_unknown_or_column(monkeypatch):
    _wire_hide(monkeypatch, HPool(None), resolved=None)
    assert await contextstore.set_table_hidden(REF, True) is None
    _wire_hide(monkeypatch, HPool(None), resolved={"kind": "column"})
    assert await contextstore.set_table_hidden(REF + ".id", True) is None
    assert await contextstore.set_table_hidden(REF, False) is None


@pytest.mark.parametrize("body", [None, {}, {"ref": 5}, {"ref": "x" * 301},
                                  {"ref": "other.public.t"}, {"ref": "business_db.t"},
                                  {"ref": "business_db.a.b.c"},
                                  {"ref": REF, "decided_by": "me"}])
async def test_hide_route_422(body):
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table(body)
    assert e.value.status_code == 422


async def test_hide_route_404_and_default(monkeypatch):
    seen = {}

    async def fake(ref, hidden, decided_by=None):
        seen["a"] = (ref, hidden, decided_by)
        return None if ref.endswith("gone") else {"ref": ref, "hidden": hidden}
    monkeypatch.setattr(contextstore, "set_table_hidden", fake)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": "business_db.public.gone"})
    assert e.value.status_code == 404
    out = await ownerapi.hide_table({"ref": REF})
    assert out["hidden"] is True and seen["a"] == (REF, True, None)



# Phase 11: WR-04 -- readable errors on the hide route
class _People:
    def __init__(self, known):
        self.known = known

    async def fetchval(self, sql, *a):
        return 1 if a[0] in self.known else None


def _wire_people(monkeypatch, known=(2,)):
    async def gp():
        return _People(known)
    monkeypatch.setattr(ownerapi.db, "get_pool", gp)


async def test_hide_route_unknown_person_422(monkeypatch):
    _wire_people(monkeypatch)
    called = []

    async def fake(*a, **k):
        called.append(a)
    monkeypatch.setattr(contextstore, "set_table_hidden", fake)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF, "decided_by": 999})
    assert e.value.status_code == 422 and not called
    assert "known person" in e.value.detail


async def test_hide_route_known_person_proceeds(monkeypatch):
    _wire_people(monkeypatch)
    seen = {}

    async def fake(ref, hidden, decided_by=None):
        seen["a"] = decided_by
        return {"ref": ref}
    monkeypatch.setattr(contextstore, "set_table_hidden", fake)
    await ownerapi.hide_table({"ref": REF, "decided_by": 2})
    assert seen["a"] == 2


@pytest.mark.parametrize("exc,name", [
    (OSError("postgres://u:pw@h/db"), "OSError"),
    (__import__("asyncio").TimeoutError(), "TimeoutError"),
    (__import__("app.guardrails", fromlist=["x"]).SQLGuardrailError("no url"),
     "SQLGuardrailError"),
])
async def test_hide_route_503(monkeypatch, exc, name):
    _wire_hide(monkeypatch, HPool(None), raises=exc)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF})
    assert e.value.status_code == 503
    assert e.value.detail == f"business database unavailable: {name}"


async def test_hide_route_409_truncated(monkeypatch):
    _wire_hide(monkeypatch, HPool(None), resolved=None, truncated=True)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF})
    assert e.value.status_code == 409 and "sql_row_limit" in e.value.detail


async def test_hide_route_404_not_truncated_and_column(monkeypatch):
    _wire_hide(monkeypatch, HPool(None), resolved=None, truncated=False)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF})
    assert e.value.status_code == 404
    _wire_hide(monkeypatch, HPool(None), resolved={"kind": "column"}, truncated=True)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF})
    assert e.value.status_code == 404


async def test_hide_route_fk_race_422(monkeypatch):
    import asyncpg

    async def fake(*a, **k):
        raise asyncpg.ForeignKeyViolationError("x")
    monkeypatch.setattr(contextstore, "set_table_hidden", fake)
    with pytest.raises(HTTPException) as e:
        await ownerapi.hide_table({"ref": REF})
    assert e.value.status_code == 422
