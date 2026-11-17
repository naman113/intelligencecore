"""Phase 17 (CONS-05): signature-preserving view replace, pure parts and _refresh_views wiring."""
import pytest

from app import whsources
from app.warehouse import DEFAULT_ROLES


def _c(n, t):
    return {"name": n, "type": t}


VIEW = [_c("id", "integer"), _c("name", "text"), _c("amount", "numeric(12,2)"),
        _c("st", '"OrderStatus"'), _c("ts", "timestamp without time zone")]
SRC = [_c("id", "bigint"), _c("name", "character varying"), _c("amount", "numeric"),
       _c("st", "text"), _c("ts", "timestamp with time zone"), _c("extra", "text"),
       _c("_dlt_id", "text")]


def test_cast_list_keeps_order_and_appends():
    tpl, vals = whsources.cast_select_list(VIEW, SRC)
    assert vals == ["id", "id", "name", "name", "amount", "amount", "st", "st", "ts", "ts", "extra"]
    assert "CAST(%I AS integer) AS %I" in tpl
    assert 'CAST(%I AS "OrderStatus") AS %I' in tpl
    assert "(%I AT TIME ZONE 'UTC')::timestamp without time zone AS %I" in tpl
    assert tpl.endswith(", %I")
    assert "_dlt_id" not in vals
    assert "COLLATE" not in tpl


def test_same_type_is_plain():
    tpl, vals = whsources.cast_select_list([_c("a", "integer")], [_c("a", "integer")])
    assert (tpl, vals) == ("%I", ["a"])


def test_timestamptz_view_from_naive_source():
    tpl, _ = whsources.cast_select_list([_c("t", "timestamp with time zone")],
                                        [_c("t", "timestamp without time zone")])
    assert "AT TIME ZONE 'UTC'" in tpl and "timestamp with time zone" in tpl


def test_vanished_column():
    with pytest.raises(whsources.ViewColumnsVanished) as e:
        whsources.cast_select_list(VIEW, [c for c in SRC if c["name"] != "name"])
    assert "name" in str(e.value)


@pytest.mark.parametrize("bad", ["integer; DROP TABLE x", "text) --", '"a"b"', "int'", "%I", ""])
def test_unsafe_type_rejected(bad):
    with pytest.raises(ValueError):
        whsources.cast_select_list([_c("a", bad)], [_c("a", "integer")])


@pytest.mark.parametrize("ok", ["numeric(12,2)", "character varying(40)[]", '"OrderStatus"',
                                'ic_types."OrderStatus"', "timestamp without time zone"])
def test_safe_types_pass(ok):
    whsources.cast_select_list([_c("a", ok)], [_c("a", "integer")])


class Conn:
    def __init__(self):
        self.stmts = []
        self.grants = False

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT format($1"):
            tpl, vals = args[0], list(args[1])
            out, i = [], 0
            for p in tpl.split("%")[0:1]:
                out.append(p)
            for p in tpl.split("%")[1:]:
                out.append('"%s"' % vals[i] if p[0] == "I" else "'%s'" % vals[i])
                out.append(p[1:])
                i += 1
            return "".join(out)
        if "has_table_privilege" in sql:
            return True
        return None

    async def execute(self, sql, *a):
        self.stmts.append(sql)

    def transaction(self):
        class T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False
        return T()


@pytest.fixture
def wired(monkeypatch):
    calls = {"rvp": []}

    async def cols(conn, name):
        return {"orders": [_c("id", "bigint"), _c("x", "text")]}

    async def sig(conn, sch, wt):
        return calls["sig"]

    async def rvp(conn, sch, wt, ss, sr, roles=DEFAULT_ROLES, *, check=True):
        calls["rvp"].append((sch, wt, ss, sr, check))
        return {"status": "ok"}

    monkeypatch.setattr(whsources, "import_table_columns", cols)
    monkeypatch.setattr(whsources, "_view_signature", sig)
    monkeypatch.setattr(whsources, "replace_view_preserving", rvp)
    return calls


T = [{"warehouse_table": "orders", "mode": "import", "warehouse_schema": "public"},
     {"warehouse_table": "live_t", "mode": "live"}]


@pytest.mark.asyncio
async def test_refresh_existing_changed_uses_preserving_no_drop(wired):
    wired["sig"] = [["id", "integer"], ["x", "text"]]
    c = Conn()
    res = await whsources._refresh_views(c, "bdb", T, None, DEFAULT_ROLES)
    assert res["refreshed"] == 1
    assert wired["rvp"] == [("public", "orders", "ic_imp_bdb", "orders", True)]
    assert not [s for s in c.stmts if "DROP" in s]


@pytest.mark.asyncio
async def test_refresh_new_view_creates(wired):
    wired["sig"] = None
    c = Conn()
    res = await whsources._refresh_views(c, "bdb", T, None, DEFAULT_ROLES)
    assert res["refreshed"] == 1 and not wired["rvp"]
    assert any(s.startswith('CREATE VIEW "public"."orders"') for s in c.stmts)


@pytest.mark.asyncio
async def test_refresh_same_signature_no_ddl(wired):
    wired["sig"] = [["id", "bigint"], ["x", "text"]]
    c = Conn()
    res = await whsources._refresh_views(c, "bdb", T, None, DEFAULT_ROLES)
    assert res["refreshed"] == 0 and not c.stmts and not wired["rvp"]


@pytest.mark.asyncio
async def test_refresh_refused_is_stale(wired, monkeypatch):
    wired["sig"] = [["id", "integer"]]

    async def refuse(*a, **k):
        return {"status": "refused", "reason": "column x is gone"}
    monkeypatch.setattr(whsources, "replace_view_preserving", refuse)
    res = await whsources._refresh_views(Conn(), "bdb", T, None, DEFAULT_ROLES)
    assert res["stale"] == ["orders"] and res["refreshed"] == 0
