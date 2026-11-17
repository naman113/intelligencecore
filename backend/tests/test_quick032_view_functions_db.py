"""Quick 032: real-Postgres proof that the regenerated view SQL means what the agent wrote.

For every expression the view check accepts (ACCEPTED in test_quick032_view_functions.py), the
original text and sqlglot's regenerated text (the SQL Postgres actually runs) give the same values
on sample rows shaped like Distributor's text data. A cleaning view built from AND, split_part, to_date,
regexp_replace and the trailing-minus pattern is created as a security_invoker view and read back.

Skipped unless DASH_TEST_DATABASE_URL or WAREHOUSE_TEST_URL points at a loopback Postgres 16, e.g.
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9333/agent_memory
Everything runs in one transaction that is rolled back (a scratch schema, no other state).
"""
import os
import secrets
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import pytest

from app.modelcompiler import normalize_definition
from test_quick032_view_functions import ACCEPTED

_URL = os.getenv("DASH_TEST_DATABASE_URL") or os.getenv("WAREHOUSE_TEST_URL") or ""
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
                       reason="DASH_TEST_DATABASE_URL / WAREHOUSE_TEST_URL not set or not loopback"),
]

_COLUMNS = (
    "id int, qty text, code text, amount text, unit text, order_date text, ts text, "
    "epoch double precision, d date, d1 date, d2 date, ts2 timestamp, y int, m int, n numeric, "
    "name text, a text, b text, payload jsonb, tags text[], k text, fmt text"
)
_ROWS = [
    (1, "189 BAG", "AB-12345", "117.60-", "BAG", "10/27/2026", "10/27/2026 13:05:09", 1730000000.0,
     date(2026, 10, 27), date(2026, 1, 1), date(2026, 10, 27), None, 2026, 10, Decimal("1234.5678"),
     "  hello world ", "x", "y", '{"id": 7, "a": {"b": "c"}}', ["x", "y"], "id", "MM/DD/YYYY"),
    (2, "43 CS 1 EA", "-77-", "42.10", "cs", "1/5/2026", "01/05/2026 00:00:00", 0.0,
     date(2026, 1, 5), date(2025, 12, 31), date(2026, 3, 1), None, 2025, 2, Decimal("-3.14159"),
     "ACME corp", "?", None, '{"id": 8}', ["z"], "a", "MM/DD/YYYY"),
]
_DISTRIBUTOR_ROWS = [
    (1, "189 BAG", "117.60-", "10/27/2026"),
    (2, "43 CS 1 EA", "42.10", "1/5/2026"),
    (3, "?", "?", "?"),
]


@pytest.fixture
async def scratch():
    import asyncpg

    conn = await asyncpg.connect(_URL)
    schema = "q032_" + secrets.token_hex(4)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"CREATE TABLE {schema}.t ({_COLUMNS})")
        await conn.executemany(
            f"INSERT INTO {schema}.t VALUES ({', '.join(f'${i}' for i in range(1, 23))})", _ROWS)
        await conn.execute(f"UPDATE {schema}.t SET ts2 = d + time '13:05:09'")
        await conn.execute(f"CREATE TABLE {schema}.orders (id int, qty text, amount text, order_date text)")
        await conn.executemany(f"INSERT INTO {schema}.orders VALUES ($1, $2, $3, $4)", _DISTRIBUTOR_ROWS)
        yield conn, schema
    finally:
        await tr.rollback()
        await conn.close()


@pytest.mark.parametrize("expr,regen", ACCEPTED)
async def test_regenerated_sql_gives_the_same_values(scratch, expr, regen):
    conn, schema = scratch
    original = f"SELECT ({expr})::text AS x FROM {schema}.t ORDER BY id"
    stored = normalize_definition("view", {"name": "v", "sql": original})["sql"]
    assert regen in stored
    want = [r["x"] for r in await conn.fetch(original)]
    got = [r["x"] for r in await conn.fetch(stored)]
    assert got == want
    assert any(v is not None for v in want), "sample rows should exercise the expression"


async def test_cleaning_view_values(scratch):
    conn, schema = scratch
    sql = (
        "SELECT id, "
        "CASE WHEN amount LIKE '%-' THEN -CAST(rtrim(amount, '-') AS NUMERIC) "
        "ELSE CAST(NULLIF(amount, '?') AS NUMERIC) END AS amount_n, "
        "to_number(NULLIF(amount, '?'), '999999999.99MI') AS amount_tn, "
        "CAST(NULLIF(split_part(qty, ' ', 1), '?') AS NUMERIC) AS qty_n, "
        "NULLIF(split_part(qty, ' ', 2), '') AS unit, "
        "CASE WHEN order_date ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$' "
        "THEN to_date(order_date, 'MM/DD/YYYY') END AS order_day, "
        "regexp_replace(amount, '[^0-9.]', '', 'g') AS amount_digits "
        f"FROM {schema}.orders WHERE (amount <> '?' AND qty <> '?') OR id = 3"
    )
    stored = normalize_definition("view", {"name": "orders_clean", "sql": sql})["sql"]
    await conn.execute(f"CREATE VIEW {schema}.orders_clean WITH (security_invoker = true) AS {stored}")
    rows = [dict(r) for r in await conn.fetch(f"SELECT * FROM {schema}.orders_clean ORDER BY id")]
    assert rows == [
        {"id": 1, "amount_n": Decimal("-117.60"), "amount_tn": Decimal("-117.60"), "qty_n": Decimal(189),
         "unit": "BAG", "order_day": date(2026, 10, 27), "amount_digits": "117.60"},
        {"id": 2, "amount_n": Decimal("42.10"), "amount_tn": Decimal("42.10"), "qty_n": Decimal(43),
         "unit": "CS", "order_day": date(2026, 1, 5), "amount_digits": "42.10"},
        {"id": 3, "amount_n": None, "amount_tn": None, "qty_n": None,
         "unit": None, "order_day": None, "amount_digits": ""},
    ]


async def test_brief_literals(scratch):
    conn, _ = scratch
    one = normalize_definition("view", {"name": "v", "sql": (
        "SELECT CAST(split_part('189 BAG', ' ', 1) AS NUMERIC) AS q, "
        "to_date('10/27/2026', 'MM/DD/YYYY') AS d, "
        "to_number('117.60-', '999999.99MI') AS a, "
        "regexp_replace('117.60-', '[^0-9.]', '', 'g') AS r FROM public.none_needed")})["sql"]
    select = one.replace(" FROM public.none_needed", "")
    row = dict(await conn.fetchrow(select))
    assert row == {"q": Decimal(189), "d": date(2026, 10, 27), "a": Decimal("-117.60"), "r": "117.60"}
