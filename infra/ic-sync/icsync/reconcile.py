"""Nightly key reconcile: delete warehouse rows whose key no longer exists at the source.

Safety guards: nothing is deleted when the key stream fails, returns no keys for a non-empty
table, or returns fewer than half the warehouse rows, or when the keys do not match the warehouse rows
(different text forms across engines). Every name goes through sql.Identifier.
"""
from __future__ import annotations

KEY_TABLE = "_ic_keys"
KEY_CHUNK = 50_000
NOTE_NO_KEYS = "reconcile skipped: no source keys"
NOTE_DROPPED = "reconcile skipped: key count dropped by more than half"
NOTE_MISMATCH = "reconcile skipped: source keys do not match the warehouse key form"


def decide_match(warehouse_rows: int, matched: int) -> tuple[bool, str | None]:
    """Pure rule on rows that actually match a source key (text forms can differ per engine).

    Refuse when fewer than half match, or when the delete would remove more than 10% of the
    table (and more than 100 rows)."""
    if warehouse_rows <= 0:
        return True, None
    if matched * 2 < warehouse_rows or warehouse_rows - matched > max(100, warehouse_rows // 10):
        return False, NOTE_MISMATCH
    return True, None


def decide(source_keys: int, warehouse_rows: int) -> tuple[bool, str | None]:
    """Pure rule: (delete allowed, skip note)."""
    if source_keys <= 0 and warehouse_rows > 0:
        return False, NOTE_NO_KEYS
    if warehouse_rows > 0 and source_keys * 2 < warehouse_rows:
        return False, NOTE_DROPPED
    return True, None


def build_delete(dataset: str, table: str, key_cols: list[str]):
    from psycopg2 import sql

    conds = [
        sql.SQL("k.{k} = d.{c}::text").format(k=sql.Identifier(f"k{i}"), c=sql.Identifier(c))
        for i, c in enumerate(key_cols)
    ]
    return sql.SQL("DELETE FROM {s}.{t} d WHERE NOT EXISTS (SELECT 1 FROM {s}.{kt} k WHERE {w})").format(
        s=sql.Identifier(dataset), t=sql.Identifier(table), kt=sql.Identifier(KEY_TABLE),
        w=sql.SQL(" AND ").join(conds))


def reconcile_table(src_engine, wh_conn, dataset: str, table: str, remote_schema, remote_table: str,
                    key_cols: list[str]) -> dict:
    """Returns {"deleted": int|None, "note": str|None}. Never raises for a failed key stream."""
    from psycopg2 import sql
    from psycopg2.extras import execute_values
    from sqlalchemy import String, cast, column, select
    from sqlalchemy import table as sa_table

    if not key_cols:
        return {"deleted": None, "note": "reconcile skipped: no merge key"}
    ds = sql.Identifier(dataset)
    kt = sql.Identifier(KEY_TABLE)
    cur = wh_conn.cursor()
    try:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {s}.{k}").format(s=ds, k=kt))
        cols = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(f"k{i}")) for i in range(len(key_cols)))
        cur.execute(sql.SQL("CREATE TABLE {s}.{k} ({c})").format(s=ds, k=kt, c=cols))
        wh_conn.commit()
        sat = sa_table(remote_table, *[column(c) for c in key_cols], schema=remote_schema or None)
        stmt = select(*[cast(column(c), String) for c in key_cols]).select_from(sat)
        ins = sql.SQL("INSERT INTO {s}.{k} VALUES %s").format(s=ds, k=kt).as_string(wh_conn)
        total = 0
        try:
            with src_engine.connect().execution_options(stream_results=True, yield_per=KEY_CHUNK) as sc:
                for part in sc.execute(stmt).partitions(KEY_CHUNK):
                    rows = [tuple("" if v is None else str(v) for v in r) for r in part]
                    if rows:
                        execute_values(cur, ins, rows, page_size=10_000)
                        total += len(rows)
        except Exception as e:  # noqa: BLE001 - a failed stream must never turn into a delete
            wh_conn.rollback()
            return {"deleted": None, "note": f"reconcile skipped: key stream failed ({type(e).__name__})"}
        wh_conn.commit()
        cur.execute(sql.SQL("SELECT count(*) FROM {s}.{t}").format(s=ds, t=sql.Identifier(table)))
        wh_rows = cur.fetchone()[0]
        ok, note = decide(total, wh_rows)
        if not ok:
            return {"deleted": None, "note": note}
        conds = sql.SQL(" AND ").join(
            sql.SQL("k.{k} = d.{c}::text").format(k=sql.Identifier(f"k{i}"), c=sql.Identifier(c))
            for i, c in enumerate(key_cols))
        cur.execute(sql.SQL("SELECT count(*) FROM {s}.{t} d WHERE EXISTS "
                            "(SELECT 1 FROM {s}.{kt} k WHERE {w})").format(
            s=ds, t=sql.Identifier(table), kt=kt, w=conds))
        matched = cur.fetchone()[0]
        ok, note = decide_match(wh_rows, matched)
        if not ok:
            return {"deleted": None, "note": note}
        cur.execute(build_delete(dataset, table, key_cols))
        deleted = cur.rowcount
        wh_conn.commit()
        return {"deleted": deleted, "note": None}
    finally:
        try:
            wh_conn.rollback()
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {s}.{k}").format(s=ds, k=kt))
            wh_conn.commit()
        except Exception:  # noqa: BLE001
            pass
