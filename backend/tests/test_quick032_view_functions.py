"""Quick 032: model views accept AND/OR and the text-cleaning functions real client data needs.

Pure tests of modelcompiler's derived-SELECT check (no database). The real-Postgres proof that
every accepted expression's regenerated SQL gives the same values is
test_quick032_view_functions_db.py.
"""
import pytest

from app import modelcompiler
from app.modelcompiler import InvalidRequest, derived_select_refs, normalize_definition, validate_expr


def _view(expr: str) -> str:
    return normalize_definition("view", {"name": "v", "sql": f"SELECT {expr} AS x FROM public.t"})["sql"]


def _refusal(sql: str) -> str:
    with pytest.raises(InvalidRequest) as ei:
        derived_select_refs(sql)
    return str(ei.value)


# ---------------------------------------------------------------- Bug 1: AND / OR / NOT

@pytest.mark.parametrize("sql", [
    "SELECT a FROM public.t WHERE x > 1 AND y < 2",
    "SELECT a FROM public.t WHERE x > 1 OR y < 2",
    "SELECT a FROM public.t WHERE NOT (x > 1) AND (y < 2 OR z IS NULL)",
    "SELECT a FROM public.t WHERE a > 1 AND b < 2 OR NOT (c = 3 AND d = 4)",
    "SELECT CASE WHEN a > 1 AND b < 2 THEN 'x' WHEN a > 1 OR NOT b < 2 THEN "
    "CASE WHEN c = 1 AND d = 2 THEN 'y' END ELSE 'z' END AS k FROM public.t",
    "SELECT COUNT(*) FILTER (WHERE a > 1 AND b < 2) AS n FROM public.t",
    "SELECT a FROM public.t WHERE EXISTS (SELECT 1 FROM public.u WHERE u.id = t.id AND u.flag)",
    "SELECT a FROM public.t WHERE a = ANY(ARRAY[1, 2]) AND b IN (1, 2)",
])
def test_and_or_not_accepted(sql):
    assert derived_select_refs(sql)


def test_and_or_normalized_view_keeps_logic():
    out = normalize_definition("view", {"name": "v", "sql":
                                        "SELECT a FROM public.t WHERE x > 1 AND (y < 2 OR NOT z)"})
    assert out["sql"] == "SELECT a FROM public.t WHERE x > 1 AND (y < 2 OR NOT z)"


def test_measure_expressions_accept_and_or():
    cols = {"status": "text", "amount": "numeric", "refunded": "boolean"}
    e = "CASE WHEN status = 'paid' AND amount > 0 OR NOT refunded THEN amount ELSE 0 END"
    assert validate_expr(e, cols) is not None
    m = normalize_definition("measure", dict(name="m", type="simple", base_relation="public.t",
                                             agg="sum", expr=e))
    assert m["expr"] == e


# ---------------------------------------------------------------- Bug 2: functions views may use

# (expression as the agent writes it, the SQL sqlglot regenerates and Postgres runs)
ACCEPTED = [
    ("split_part(qty, ' ', 1)", "SPLIT_PART(qty, ' ', 1)"),
    ("CAST(split_part(qty, ' ', 1) AS NUMERIC)", "CAST(SPLIT_PART(qty, ' ', 1) AS DECIMAL)"),
    ("substring(code from 4 for 3)", "SUBSTRING(code FROM 4 FOR 3)"),
    ("substr(code, 4, 3)", "SUBSTRING(code FROM 4 FOR 3)"),
    ("substring(qty from '^[0-9.]+')", "SUBSTRING(qty FROM '^[0-9.]+')"),
    ("position('-' in amount)", "POSITION('-' IN amount)"),
    ("strpos(amount, '-')", "POSITION('-' IN amount)"),
    ("replace(amount, ',', '')", "REPLACE(amount, ',', '')"),
    ("translate(amount, '$,', '')", "TRANSLATE(amount, '$,', '')"),
    ("regexp_replace(amount, '[^0-9.]', '', 'g')", "REGEXP_REPLACE(amount, '[^0-9.]', '', 'g')"),
    ("regexp_replace(qty, '\\s+', ' ', 'g')", "REGEXP_REPLACE(qty, '\\s+', ' ', 'g')"),
    ("regexp_count(qty, '[0-9]+')", "REGEXP_COUNT(qty, '[0-9]+')"),
    ("regexp_instr(qty, '[A-Z]')", "REGEXP_INSTR(qty, '[A-Z]')"),
    ("regexp_substr(qty, '[0-9]+')", "REGEXP_SUBSTR(qty, '[0-9]+')"),
    ("(regexp_match(qty, '^([0-9]+) ([A-Z]+)'))[2]", "(REGEXP_MATCH(qty, '^([0-9]+) ([A-Z]+)'))[2]"),
    ("qty ~ '^[0-9]+ [A-Z]+'", "qty ~ '^[0-9]+ [A-Z]+'"),
    ("unit ~* 'bag'", "unit ~* 'bag'"),
    ("unit !~ '^[0-9]'", "NOT unit ~ '^[0-9]'"),
    ("regexp_like(unit, 'BAG')", "unit ~ 'BAG'"),
    ("to_date(order_date, 'MM/DD/YYYY')", "TO_DATE(order_date, 'MM/DD/YYYY')"),
    ("to_date(order_date, 'FMMM/FMDD/YYYY')", "TO_DATE(order_date, 'FMMM/FMDD/YYYY')"),
    ("to_timestamp(ts, 'MM/DD/YYYY HH24:MI:SS')", "TO_TIMESTAMP(ts, 'MM/DD/YYYY HH24:MI:SS')"),
    ("to_timestamp(epoch)", "TO_TIMESTAMP(epoch)"),
    ("to_number(amount, '999999999.99MI')", "TO_NUMBER(amount, '999999999.99MI')"),
    ("to_char(d, 'YYYY-MM')", "TO_CHAR(d, 'YYYY-MM')"),
    ("to_char(d, 'DD-Mon-YYYY')", "TO_CHAR(d, 'DD-Mon-YYYY')"),
    ("to_char(n, 'FM999,990.00')", "TO_CHAR(n, 'FM999,990.00')"),
    ("left(code, 3)", "LEFT(code, 3)"),
    ("right(code, 2)", "RIGHT(code, 2)"),
    ("lpad(code, 8, '0')", "LPAD(code, 8, '0')"),
    ("rpad(code, 8)", "RPAD(code, 8)"),
    ("btrim(code, '-')", "BTRIM(code, '-')"),
    ("rtrim(amount, '-')", "TRIM(TRAILING '-' FROM amount)"),
    ("ltrim(code)", "LTRIM(code)"),
    ("trim(both ' ' from name)", "TRIM(BOTH ' ' FROM name)"),
    ("initcap(name)", "INITCAP(name)"),
    ("concat_ws('-', a, b)", "CONCAT_WS('-', a, b)"),
    ("nullif(trim(a), '?')", "NULLIF(TRIM(a), '?')"),
    ("age(d2, d1)", "AGE(d2, d1)"),
    ("make_date(y, m, 1)", "MAKE_DATE(y, m, 1)"),
    ("date(ts2)", "DATE(ts2)"),
    ("d + INTERVAL '1 day'", "d + INTERVAL '1 DAY'"),
    ("d + INTERVAL '1' DAY", "d + INTERVAL '1 DAY'"),
    ("d - INTERVAL '1 day 2 hours'", "d - INTERVAL '1 day 2 hours'"),
    ("d2 - d1", "d2 - d1"),
    ("now()", "CURRENT_TIMESTAMP"),
    ("mod(y, 7)", "y % 7"),
    ("trunc(n, 2)", "TRUNC(n, 2)"),
    ("sign(n)", "SIGN(n)"),
    ("n ^ 2", "POWER(n, 2)"),
    ("payload ->> 'id'", "payload ->> 'id'"),
    ("payload -> 'a' ->> 'b'", "payload -> 'a' ->> 'b'"),
    ("payload #>> '{a,b}'", "payload #>> '{a,b}'"),
    ("payload ? 'id'", "payload ? 'id'"),
    ("payload @> '{\"id\": 7}'", "payload @> '{\"id\": 7}'"),
    ("tags && ARRAY['x', 'z']", "tags && ARRAY['x', 'z']"),
    ("code COLLATE \"C\"", "code COLLATE \"C\""),
    ("CASE WHEN amount LIKE '%-' THEN -CAST(rtrim(amount, '-') AS NUMERIC) "
     "ELSE CAST(amount AS NUMERIC) END",
     "CASE WHEN amount LIKE '%-' THEN -CAST(TRIM(TRAILING '-' FROM amount) AS DECIMAL) "
     "ELSE CAST(amount AS DECIMAL) END"),
    ("CASE WHEN order_date ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$' AND order_date <> '?' "
     "THEN to_date(order_date, 'MM/DD/YYYY') END",
     "CASE WHEN order_date ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$' AND order_date <> '?' "
     "THEN TO_DATE(order_date, 'MM/DD/YYYY') END"),
]


@pytest.mark.parametrize("expr,regen", ACCEPTED)
def test_function_accepted_and_round_trip_pinned(expr, regen):
    assert _view(expr) == f"SELECT {regen} AS x FROM public.t"


@pytest.mark.parametrize("expr,regen", ACCEPTED)
def test_normalize_is_idempotent(expr, regen):
    """semanticmodel.apply re-validates the stored (regenerated) SQL at approval time."""
    once = _view(expr)
    assert normalize_definition("view", {"name": "v", "sql": once})["sql"] == once


def test_cleaning_view_refs():
    sql = ("SELECT to_date(order_date, 'MM/DD/YYYY') AS order_day, "
           "CAST(split_part(qty, ' ', 1) AS NUMERIC) AS qty_n, split_part(qty, ' ', 2) AS unit, "
           "CASE WHEN amount LIKE '%-' THEN -CAST(rtrim(amount, '-') AS NUMERIC) "
           "ELSE CAST(NULLIF(amount, '?') AS NUMERIC) END AS amount_n "
           "FROM src_distributor.orders WHERE amount <> '?' AND qty ~ '^[0-9]+ ' OR unit IS NULL")
    assert derived_select_refs(sql) == ("src_distributor.orders",)


# ---------------------------------------------------------------- still refused

@pytest.mark.parametrize("sql", [
    "SELECT pg_read_file('/etc/passwd') FROM public.t",
    "SELECT dblink('host=x', 'select 1') FROM public.t",
    "SELECT lo_import('/etc/passwd') FROM public.t",
    "SELECT foo(a) FROM public.t",
    "SELECT current_setting('x') FROM public.t",
    "SELECT nextval('s') FROM public.t",
    "SELECT version() FROM public.t",
    "SELECT txid_current() FROM public.t",
    "SELECT random() FROM public.t",
    "SELECT generate_series(1, 3) FROM public.t",
    "SELECT unnest(tags) FROM public.t",
    "SELECT * FROM generate_series(1, 3) g",
    "SELECT * FROM public.t, regexp_split_to_table(a, ' ') x",
    "SELECT * FROM public.t CROSS JOIN unnest(tags) u",
    "SELECT regexp_matches(a, 'x') FROM public.t",
    "SELECT public.age(d) FROM public.t",
    "SELECT pg_catalog.lower(a) FROM public.t",
    "SELECT \"age\"(d) FROM public.t",
    "SELECT row_number() OVER () FROM public.t",
    "SELECT a FROM public.t WHERE payload @? '$.a'",
])
def test_unsafe_still_refused(sql):
    with pytest.raises(InvalidRequest):
        derived_select_refs(sql)


# ---------------------------------------------------------------- round trips that would change meaning

@pytest.mark.parametrize("expr,hint", [
    ("regexp_like(unit, 'bag', 'i')", "~*"),                    # sqlglot drops the flag
    ("to_char(d, 'day')", "'Day'"),                              # 'tuesday' would become 'Tuesday'
    ("to_date(order_date, 'mm/dd/yyyy')", "'MM/DD/YYYY'"),      # rewritten format: write it as emitted
    ("to_date(order_date, fmt)", "literal"),
    ("unit ~ E'^\\\\d+$'", "E'"),                                # E'^\\d+$' would run as e'^\d+$'
    ("replace(a, E'\\\\', '')", "E'"),
    ("d + INTERVAL '1 day ago'", "INTERVAL"),                   # would run as INTERVAL '1 DAY'
    ("TRY_CAST(a AS INT)", "TRY_CAST"),                         # would run as CAST and fail on bad rows
    ("payload -> k", "literal"),                                # would run as JSON_EXTRACT_PATH(payload, k)
])
def test_meaning_changing_round_trips_refused(expr, hint):
    msg = _refusal(f"SELECT {expr} AS x FROM public.t")
    assert hint in msg, msg


# ---------------------------------------------------------------- bounded cost

def test_regex_pattern_cap():
    cap = modelcompiler._REGEX_MAX_CHARS
    assert cap == 200
    ok = "a" * cap
    derived_select_refs(f"SELECT regexp_replace(a, '{ok}', '') FROM public.t")
    derived_select_refs(f"SELECT a FROM public.t WHERE a ~ '{ok}'")
    long = "a" * (cap + 1)
    for sql in (f"SELECT regexp_replace(a, '{long}', '') FROM public.t",
                f"SELECT a FROM public.t WHERE a ~* '{long}'",
                f"SELECT regexp_count(a, '{long}') FROM public.t",
                f"SELECT regexp_substr(a, '{long}') FROM public.t",
                f"SELECT substring(a from '{long}') FROM public.t",
                f"SELECT a FROM public.t WHERE a SIMILAR TO '{long}'"):
        assert "200" in _refusal(sql)


@pytest.mark.parametrize("sql", [
    "SELECT a FROM public.t WHERE a ~ b",
    "SELECT regexp_replace(a, b, '') FROM public.t",
    "SELECT regexp_match(a, b) FROM public.t",
    "SELECT a FROM public.t WHERE a SIMILAR TO b",
    "SELECT regexp_replace(a, $$x$$, '') FROM public.t",
])
def test_regex_pattern_must_be_plain_literal(sql):
    assert "pattern" in _refusal(sql)


def test_pad_length_cap():
    assert modelcompiler._PAD_MAX == 1000
    derived_select_refs("SELECT lpad(a, 1000, '0') FROM public.t")
    for sql in ("SELECT lpad(a, 1001, '0') FROM public.t", "SELECT rpad(a, n) FROM public.t",
                "SELECT lpad(a, -1) FROM public.t", "SELECT lpad(a, 10.5) FROM public.t"):
        assert "1000" in _refusal(sql)


# ---------------------------------------------------------------- Bug 3: one refusal lists everything

def test_refusal_lists_every_bad_function_once_plus_allowed_list():
    msg = _refusal("SELECT foo(a), bar(b), foo(c), generate_series(1, 2), unnest(tags), "
                   "regexp_like(a, 'x', 'i'), lower(a) FROM public.t WHERE x > 1 AND y < 2")
    for name in ("foo()", "bar()", "generate_series()", "unnest()"):
        assert msg.count(name) == 1, (name, msg)
    assert "~*" in msg
    assert "lower()" not in msg and "AND" not in msg.split("Views may use")[0]
    assert "Views may use" in msg and "split_part" in msg and "to_date" in msg


def test_refusal_combines_function_and_table_problems():
    msg = _refusal("SELECT foo(a), row_number() OVER () FROM t JOIN pg_catalog.pg_class c ON true")
    assert "foo()" in msg
    assert "window functions" in msg
    assert "table t must be schema-qualified" in msg
    assert "schema pg_catalog" in msg


def test_table_only_problem_has_no_function_list():
    msg = _refusal("SELECT a FROM t")
    assert "schema-qualified" in msg and "Views may use" not in msg


def test_help_list_is_short_and_names_the_cleaning_functions():
    h = modelcompiler.VIEW_FUNCTIONS_HELP
    assert len(h) <= 700
    for w in ("AND/OR/NOT", "CASE", "split_part", "substring", "regexp_replace", "to_date",
              "to_number", "replace", "left", "lpad", "age", "make_date"):
        assert w in h, w


# ---------------------------------------------------------------- data-modeling skill

def test_skill_names_view_functions_and_the_right_view_shape():
    import hashlib
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md") \
        .read_bytes().decode("ascii").replace("\r\n", "\n")
    assert "A view may use AND/OR/NOT, CASE, CAST and text-cleaning functions" in text
    assert "split_part" in text and "to_date(d, 'MM/DD/YYYY')" in text and "one retry" in text
    assert 'View: {"object": "view", "name": "orders_clean", "sql": "SELECT ..."}' in text
    assert "derived_sql" not in text
    # the md5-guarded refresh reaches installs that still have the unedited 16-10 body
    v2 = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v2.md").read_bytes().decode("ascii")
    norm = lambda s: s.replace(chr(13), "").strip(" " + chr(10) + chr(9))  # noqa: E731
    h = hashlib.md5(norm(v2).encode()).hexdigest()
    assert norm(v2) != norm(text)
    schema = (root / "backend" / "app" / "schema.sql").read_text(encoding="utf-8")
    guard = re.search(r"WHERE name = 'data-modeling' AND md5\(.*?\) IN \(([^)]*)\);", schema).group(1)
    assert f"'{h}'" in guard and "'2e58f0e3ada2f8f473345c501c51694d'" in guard
