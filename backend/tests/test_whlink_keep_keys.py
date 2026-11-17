"""Phase 17 (CONS-05): keep-keys in the business_db link sweep."""
import pytest

from app import warehouse, whlink

K = ("public", "sales_orders")


class Conn:
    def __init__(self, linked):
        self.linked = linked
        self.stmts = []

    async def fetch(self, sql, *a):
        return self.linked if "link_objects" in sql else []

    async def fetchval(self, sql, *a):
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


def _row(h):
    return {"remote_schema": K[0], "remote_table": K[1], "col_hash": h, "private_schema": "ic_fdw_x"}


@pytest.fixture
def ddl(monkeypatch):
    calls = []

    async def drop(conn, *a):
        calls.append("drop")

    async def create(conn, source, t, roles, **kw):
        calls.append("create_ft")
        return "ic_fdw_x", [], []

    async def view(*a):
        calls.append("view")

    async def upsert(*a):
        calls.append("upsert")

    async def delete(*a):
        calls.append("delete")

    monkeypatch.setattr(whlink, "_drop_objects", drop)
    monkeypatch.setattr(whlink, "_create_foreign_table", create)
    monkeypatch.setattr(whlink, "_create_view", view)
    monkeypatch.setattr(whlink, "_upsert_link_object", upsert)
    monkeypatch.setattr(whlink, "_delete_link_object", delete)
    monkeypatch.setattr(whlink, "_tables_of", lambda snap: snap["tables"])
    return calls


def _t(h):
    return {"schema_name": K[0], "table_name": K[1], "col_hash": h, "probe": False}


async def test_kept_key_changed_hash_no_ddl(ddl):
    res = await whlink.apply_changes(Conn([_row("old")]), "business_db", {"tables": [_t("new")]},
                                     keep_keys=frozenset({K}))
    assert ddl == [] and K in res["linked"] and res["reverted"] == []


async def test_kept_key_vanished_from_snapshot_no_drop(ddl):
    res = await whlink.apply_changes(Conn([_row("old")]), "business_db", {"tables": []},
                                     keep_keys=frozenset({K}))
    assert ddl == [] and res["dropped"] == 0 and K in res["linked"]


async def test_kept_key_without_row_is_created_and_reverted(ddl):
    res = await whlink.apply_changes(Conn([]), "business_db", {"tables": [_t("h")]},
                                     keep_keys=frozenset({K}))
    assert "create_ft" in ddl and res["reverted"] == ["public.sales_orders"]


async def test_no_keep_keys_unchanged(ddl):
    res = await whlink.apply_changes(Conn([_row("old")]), "business_db", {"tables": [_t("new")]})
    assert "create_ft" in ddl and res["replaced"] == 1 and res["reverted"] == []


async def test_load_keep_keys_failure_fails_closed(monkeypatch):
    from app import db

    async def boom():
        raise OSError("down")
    monkeypatch.setattr(db, "get_pool", boom)
    assert await warehouse._load_keep_keys() is None


async def test_reverted_marks_reimport(monkeypatch):
    from app import db
    seen = []

    class P:
        async def execute(self, sql, *a):
            seen.append((sql, a))

    async def pool():
        return P()
    monkeypatch.setattr(db, "get_pool", pool)
    await warehouse._mark_reimport(["public.sales_orders"])
    sql, args = seen[0]
    assert "pending_mode = 'import'" in sql and "import_reset = true" in sql
    assert args == ("public", "sales_orders")
