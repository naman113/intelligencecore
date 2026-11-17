"""check and probe: read-only inspection of a Postgres, MySQL or SQL Server source."""
from collections.abc import Mapping

from .conn import source_engine

LIST_TABLES_MAX = 200
SAMPLE_TABLES_MAX = 50
SAMPLE_ROWS = 3
SAMPLE_CHARS = 60
STATEMENT_TIMEOUT_S = 30

PREFERRED_CURSORS = ("updated_at", "modified_at", "last_modified", "updated", "modified")
MYSQL_WRITE_PRIVS = {"ALL", "ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "SUPER",
                    "TRIGGER", "EVENT", "CREATE ROUTINE", "ALTER ROUTINE", "EXECUTE", "FILE", "INDEX",
                    "CREATE VIEW", "CREATE USER", "CREATE ROLE", "DROP ROLE", "TRUNCATE"}


# ---- pure helpers (unit tested) -------------------------------------------------------------

def _split_privs(head: str) -> list[str]:
    """Split a privilege list on commas outside parentheses, so column lists stay attached."""
    out, depth, cur = [], 0, ""
    for ch in head:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def mysql_grants_read_only(grants: list[str]) -> bool:
    # Quick 028 (WR-12): conservative. Column-level privileges count by their name, more
    # write-capable privileges are listed, and a role grant (GRANT `r`@`%` TO u, no ON) is
    # unknown so it counts as not read-only. This can only turn "read-only" into "not".
    for line in grants:
        up = str(line).strip()
        if not up.upper().startswith("GRANT "):
            continue
        head = up[6:]
        idx = head.upper().find(" ON ")
        if idx < 0:
            if " TO " in head.upper():
                return False
            continue
        tail = head[idx:].upper()
        if "WITH GRANT OPTION" in tail:
            return False
        for p in _split_privs(head[:idx]):
            name = p.split("(")[0].strip().upper()
            if name in MYSQL_WRITE_PRIVS:
                return False
    return True


def _is_time_type(t: str) -> bool:
    t = (t or "").lower()
    return t.startswith(("date", "time", "smalldatetime", "rowversion"))


def cursor_candidates(columns: list[dict], indexes: list[list[str]]) -> list[dict]:
    first = {ix[0] for ix in indexes if ix}
    out = [{"column": c["name"], "type": c["type"], "indexed": c["name"] in first}
           for c in columns if _is_time_type(c.get("type", ""))]

    def rank(c):
        n = c["column"].lower()
        return (PREFERRED_CURSORS.index(n) if n in PREFERRED_CURSORS else len(PREFERRED_CURSORS), 0)
    out.sort(key=rank)
    return out


def canonical_mssql_type(name: str, max_length: int, precision: int, scale: int) -> str:
    n = (name or "").lower()
    if n in ("nvarchar", "nchar"):
        return f"{n}(max)" if max_length == -1 else f"{n}({max_length // 2})"
    if n in ("varchar", "char", "varbinary", "binary"):
        return f"{n}(max)" if max_length == -1 else f"{n}({max_length})"
    if n in ("decimal", "numeric"):
        return f"{n}({precision},{scale})"
    if n in ("datetime2", "time", "datetimeoffset"):
        return f"{n}({scale})"
    return n


def canonical_mysql_type(column_type: str) -> str:
    return (column_type or "").lower()


def sample_value(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()[:SAMPLE_CHARS]
    return str(v)[:SAMPLE_CHARS]


# ---- per-dialect catalog SQL (fixed text, values bound) -------------------------------------

def _sql(sql: str):
    from sqlalchemy import text
    return text(sql)


def _set_timeouts(conn, kind: str) -> None:
    if kind == "postgres":
        conn.exec_driver_sql(f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
    elif kind == "mysql":
        conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={STATEMENT_TIMEOUT_S * 1000}")


def _pg_tables(conn, schema):
    sql = """
        SELECT n.nspname, c.relname, c.reltuples::bigint, c.relpages::bigint * 8 / 1024.0
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
          AND has_table_privilege(c.oid, 'SELECT')
          AND (CAST(:schema AS text) IS NULL OR n.nspname = CAST(:schema AS text))
        ORDER BY 1, 2"""
    return [(s, t, (None if r is None or r < 0 else int(r)), float(m or 0))
            for s, t, r, m in conn.execute(_sql(sql), {"schema": schema})]


def _my_tables(conn, schema):
    sql = """
        SELECT table_schema, table_name, table_rows, (data_length + index_length) / 1048576.0
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE' AND table_schema = DATABASE()
        ORDER BY 1, 2"""
    return [(s, t, (None if r is None else int(r)), float(m or 0)) for s, t, r, m in conn.execute(_sql(sql))]


def _ms_tables(conn, schema):
    base = """
        SELECT s.name, t.name, SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END),
               SUM(p.used_page_count) * 8 / 1024.0
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        LEFT JOIN sys.dm_db_partition_stats p ON p.object_id = t.object_id
        WHERE (:schema IS NULL OR s.name = :schema)
          AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', 'SELECT') = 1
        GROUP BY s.name, t.name ORDER BY 1, 2"""
    try:
        rows = conn.execute(_sql(base), {"schema": schema}).fetchall()
        return [(s, t, (None if r is None else int(r)), float(m or 0)) for s, t, r, m in rows]
    except Exception:  # noqa: BLE001 - DMV needs VIEW DATABASE STATE; fall back without estimates
        fb = """
            SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE (:schema IS NULL OR s.name = :schema)
              AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', 'SELECT') = 1
            ORDER BY 1, 2"""
        return [(s, t, None, 0.0) for s, t in conn.execute(_sql(fb), {"schema": schema})]


def _list_tables(conn, kind, schema):
    return {"postgres": _pg_tables, "mysql": _my_tables, "mssql": _ms_tables}[kind](conn, schema)


def _columns(conn, kind, schema, table) -> list[dict]:
    p = {"s": schema, "t": table}
    if kind == "postgres":
        sql = """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum"""
        return [{"name": n, "type": t, "nullable": bool(nl), "pk": False} for n, t, nl in conn.execute(_sql(sql), p)]
    if kind == "mysql":
        sql = """
            SELECT column_name, column_type, is_nullable, column_key
            FROM information_schema.columns WHERE table_schema = :s AND table_name = :t
            ORDER BY ordinal_position"""
        return [{"name": n, "type": canonical_mysql_type(t), "nullable": nl == "YES", "pk": k == "PRI"}
                for n, t, nl, k in conn.execute(_sql(sql), p)]
    sql = """
        SELECT c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable
        FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(QUOTENAME(:s) + '.' + QUOTENAME(:t))
        ORDER BY c.column_id"""
    return [{"name": n, "type": canonical_mssql_type(tn, ml, pr, sc), "nullable": bool(nl), "pk": False}
            for n, tn, ml, pr, sc, nl in conn.execute(_sql(sql), p)]


def _read_only(conn, kind) -> bool:
    if kind == "postgres":
        sup = conn.execute(_sql("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar()
        if sup:
            return False
        w = conn.execute(_sql("""
            SELECT EXISTS (
              SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
                AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE')
                  OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE')))""")).scalar()
        return not w
    if kind == "mysql":
        return mysql_grants_read_only([str(r[0]) for r in conn.execute(_sql("SHOW GRANTS"))])
    bad = conn.execute(_sql("""
        SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin') = 1 OR IS_ROLEMEMBER('db_owner') = 1
                      OR IS_ROLEMEMBER('db_datawriter') = 1
                      OR HAS_PERMS_BY_NAME(db_name(), 'DATABASE', 'INSERT') = 1 THEN 1 ELSE 0 END""")).scalar()
    return not bad


def _server_version(conn, kind) -> str:
    q = {"postgres": "SHOW server_version", "mysql": "SELECT VERSION()",
         "mssql": "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(64))"}[kind]
    return str(conn.execute(_sql(q)).scalar())


# ---- commands --------------------------------------------------------------------------------

def check(job: dict, env: Mapping[str, str]) -> dict:
    kind = job["kind"]
    eng = source_engine(kind, env)
    try:
        with eng.connect() as conn:
            _set_timeouts(conn, kind)
            version = _server_version(conn, kind)
            tables = _list_tables(conn, kind, job.get("remote_schema"))
            ro = _read_only(conn, kind)
    finally:
        eng.dispose()
    return {"status": "ok", "error": None, "read_only": bool(ro), "server_version": version,
            "selectable_tables": len(tables)}


def _sample(conn, schema, table, n) -> list[list]:
    import sqlalchemy as sa
    stmt = sa.select(sa.literal_column("*")).select_from(sa.table(table, schema=schema)).limit(n)
    return [[sample_value(v) for v in row] for row in conn.execute(stmt).fetchall()]


def probe(job: dict, env: Mapping[str, str]) -> dict:
    import sqlalchemy as sa
    kind = job["kind"]
    eng = source_engine(kind, env)
    out_tables: list[dict] = []
    try:
        with eng.connect() as conn:
            _set_timeouts(conn, kind)
            tables = _list_tables(conn, kind, job.get("remote_schema"))
            total = len(tables)
            insp = sa.inspect(conn)
            for i, (schema, table, est_rows, est_mb) in enumerate(tables[:LIST_TABLES_MAX]):
                cols = _columns(conn, kind, schema, table)
                try:
                    pkc = insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or []
                except Exception:  # noqa: BLE001
                    pkc = []
                for c in cols:
                    c["pk"] = c["pk"] or c["name"] in pkc
                if not pkc:
                    pkc = [c["name"] for c in cols if c["pk"]]
                try:
                    fks = [{"columns": list(f["constrained_columns"]), "ref_schema": f.get("referred_schema"),
                            "ref_table": f["referred_table"], "ref_columns": list(f["referred_columns"])}
                           for f in insp.get_foreign_keys(table, schema=schema)]
                except Exception:  # noqa: BLE001
                    fks = []
                try:
                    idx = [list(x["column_names"]) for x in insp.get_indexes(table, schema=schema)
                           if x.get("column_names")]
                except Exception:  # noqa: BLE001
                    idx = []
                if pkc:
                    idx.append(list(pkc))
                sample: list[list] = []
                if i < SAMPLE_TABLES_MAX:
                    try:
                        sample = _sample(conn, schema, table, int(job.get("sample_rows", SAMPLE_ROWS)))
                    except Exception:  # noqa: BLE001
                        sample = []
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                out_tables.append({
                    "schema": schema, "table": table, "est_rows": est_rows, "est_mb": round(est_mb, 2),
                    "columns": cols, "pk": list(pkc), "fks": fks,
                    "cursor_candidates": cursor_candidates(cols, idx), "sample": sample})
    finally:
        eng.dispose()
    return {"status": "ok", "error": None, "tables": out_tables, "tables_total": total,
            "truncated": total > LIST_TABLES_MAX}
