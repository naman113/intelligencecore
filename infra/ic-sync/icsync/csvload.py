"""csv command: pyarrow streaming CSV reader on stdin, inferred types on test, fixed types on load."""
from __future__ import annotations

import re
import sys

from .jobs import _row, _row_count, classify_error, dlt_env, run_status, table_columns

DELIMITERS = (",", ";", "\t", "|")
ENCODINGS = ("utf-8", "utf8", "utf-8-sig")
BLOCK_SIZE = 1 << 20
MAX_COLUMNS = 1000
MAX_NAME_BYTES = 63
BAD_DELIMITER = "unsupported delimiter"
BAD_ENCODING = "unsupported encoding"


def header_ok(names) -> None:
    names = list(names)
    if len(names) > MAX_COLUMNS:
        raise ValueError("too many columns")
    seen = set()
    for n in names:
        if not isinstance(n, str) or not n:
            raise ValueError("empty column name in header")
        if "\x00" in n or len(n.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError("column name too long or invalid")
        if n in seen:
            raise ValueError("duplicate column name in header")
        seen.add(n)


def arrow_to_pg(t) -> str:
    import pyarrow as pa
    import pyarrow.types as pt
    if pt.is_int64(t):
        return "bigint"
    if pt.is_floating(t):
        return "double precision"
    if pt.is_string(t) or pt.is_large_string(t):
        return "text"
    if pt.is_boolean(t):
        return "boolean"
    if pt.is_date32(t):
        return "date"
    if pt.is_timestamp(t):
        return "timestamp with time zone" if t.tz else "timestamp without time zone"
    if pt.is_decimal(t):
        return f"numeric({t.precision},{t.scale})"
    _ = pa
    return "text"


def type_name(t) -> str:
    import pyarrow.types as pt
    if pt.is_int64(t):
        return "int64"
    if pt.is_floating(t):
        return "double"
    if pt.is_boolean(t):
        return "bool"
    if pt.is_date32(t):
        return "date32"
    if pt.is_timestamp(t):
        return f"timestamp[{t.unit}, tz={t.tz}]" if t.tz else f"timestamp[{t.unit}]"
    if pt.is_decimal(t):
        return f"decimal128({t.precision},{t.scale})"
    return "string"


def type_from_name(name: str):
    import pyarrow as pa
    n = (name or "").strip()
    simple = {"int64": pa.int64(), "double": pa.float64(), "string": pa.string(), "bool": pa.bool_(),
              "date32": pa.date32()}
    if n in simple:
        return simple[n]
    m = re.fullmatch(r"timestamp\[(s|ms|us|ns)(?:, tz=([A-Za-z0-9_/+:\-]+))?\]", n)
    if m:
        return pa.timestamp(m.group(1), tz=m.group(2))
    m = re.fullmatch(r"decimal128\((\d+),\s*(\d+)\)", n)
    if m and int(m.group(1)) <= 38:
        return pa.decimal128(int(m.group(1)), int(m.group(2)))
    return pa.string()


def _validate(csv: dict) -> None:
    if csv.get("delimiter", ",") not in DELIMITERS:
        raise ValueError(BAD_DELIMITER)
    if str(csv.get("encoding", "utf-8")).lower() not in ENCODINGS:
        raise ValueError(BAD_ENCODING)


def _batches(reader, limit):
    import pyarrow as pa
    got = 0
    for b in reader:
        if limit is not None:
            room = limit - got
            if room <= 0:
                return
            if b.num_rows > room:
                b = b.slice(0, room)
        got += b.num_rows
        yield pa.Table.from_batches([b])
        if limit is not None and got >= limit:
            return


def run_csv(job: dict, env, stream=None) -> dict:
    import dlt
    import pyarrow.csv as pacsv

    from . import conn
    from .redact import error_text
    from .jobs import _pipeline
    dlt_env()
    csv = job.get("csv") or {}
    _validate(csv)
    table = csv.get("table") or ""
    if not table:
        raise ValueError("csv table name missing")
    test = csv.get("mode") != "load"
    stream = stream if stream is not None else sys.stdin.buffer
    types = None
    if not test:
        types = {k: type_from_name(v) for k, v in (csv.get("column_types") or {}).items()}
    try:
        reader = pacsv.open_csv(
            stream,
            read_options=pacsv.ReadOptions(block_size=BLOCK_SIZE, encoding="utf8"),
            parse_options=pacsv.ParseOptions(delimiter=csv.get("delimiter", ",")),
            convert_options=pacsv.ConvertOptions(column_types=types) if types else pacsv.ConvertOptions())
        header_ok(reader.schema.names)
    except Exception as e:  # noqa: BLE001 - a bad value or header fails the run loudly, nothing is loaded
        r = _row({"warehouse_table": table}, status="failed", error_class=classify_error(e),
                 error=error_text(e, []))
        return {"status": "failed", "tables": [r], "rows_total": 0}
    limit = min(int(job.get("row_limit") or 1000), 1000) if test else None
    disposition = "replace" if test else (csv.get("disposition") or "replace")
    if disposition not in ("replace", "append"):
        raise ValueError("unsupported disposition")
    schema = reader.schema
    pipe = _pipeline(job, env)
    t = {"warehouse_table": table}

    @dlt.resource(name=table, write_disposition=disposition, schema_contract={"tables": "evolve", "columns": "evolve", "data_type": "freeze"} if not test else None)
    def rows():
        yield from _batches(reader, limit)

    res = rows()
    res.max_table_nesting = 0
    try:
        pipe.run(res, loader_file_format="csv")
    except Exception as e:  # noqa: BLE001
        r = _row(t, status="failed", error_class=classify_error(e), error=error_text(e, []))
        return {"status": "failed", "tables": [r], "rows_total": 0}
    r = _row(t, rows=_row_count(pipe, table))
    out = {"tables": [r], "rows_total": r["rows"]}
    try:
        from .jobs import _analyze
        _analyze(env, job["dataset"], [table])
        if test:
            r["columns"] = table_columns(env, job["dataset"], [table]).get(table, [])
    except Exception:  # noqa: BLE001
        pass
    if test:
        out["column_types"] = {f.name: type_name(f.type) for f in schema}
        if not r.get("columns"):
            r["columns"] = [{"name": f.name, "type": arrow_to_pg(f.type)} for f in schema]
    out["status"] = run_status([r])
    _ = conn
    return out
