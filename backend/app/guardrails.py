"""Read-only guardrails for CAISAAS access.

Three independent layers — any one alone blocks a write:

1. SESSION LAYER: every CAISAAS connection is opened with
   `default_transaction_read_only=on`. Postgres itself rejects any write
   in that session, no matter what SQL slips through.
2. AST LAYER: sqlglot parses the SQL before execution; anything that is
   not a plain SELECT (or WITH ... SELECT) is rejected. Multi-statement
   payloads are rejected outright.
3. STATEMENT TIMEOUT + ROW LIMIT: runaway queries are cut off server-side.

Ideally add layer 0 later: a dedicated read-only Postgres ROLE (SQL at
bottom of this file).

Phase 14: when the warehouse is configured (warehouse.warehouse_mode()) every
read goes to the warehouse as the read-only role wh_reader, with the same three
layers, and never falls back to the direct business database.
"""

import asyncio
import re
import zoneinfo

import asyncpg
import sqlglot
from sqlglot import expressions as exp

from . import warehouse  # Phase 14 (WH-03)
from .config import get_settings

_caisaas_pool: asyncpg.Pool | None = None

# Expression types allowed at the top level of a statement.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# CTE bodies may still smuggle writes (WITH x AS (DELETE ...) SELECT ...).
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Alter,
    exp.Create, exp.TruncateTable, exp.Grant, exp.Command, exp.Set,
)

# Functions that are read-only to Postgres' own bookkeeping but are NOT
# read-only to us: they read the host filesystem, exfiltrate credentials, or
# open a SECOND connection whose writes the read-only session flag never sees.
# `SELECT pg_read_file('/etc/passwd')` parses as a perfectly ordinary Select.
_FORBIDDEN_FUNCTIONS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_ls_logdir", "pg_ls_waldir", "pg_ls_tmpdir", "pg_ls_archive_statusdir",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "dblink_open", "dblink_connect",
    "dblink_send_query", "dblink_get_result",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",       # cheap DoS inside the timeout
    "pg_terminate_backend", "pg_cancel_backend",
    "query_to_xml", "query_to_xml_and_xmlschema",       # nests arbitrary SQL as text
    "pg_logical_emit_message", "pg_create_physical_replication_slot",
    "set_config", "pg_reload_conf",                     # can clear the read-only flag
    # Phase 7: run a SQL string or read a relation named in a string, so the
    # Table walk (and the approved-tables check) never sees what they read.
    "ts_stat", "ts_rewrite",
    "query_to_xmlschema", "cursor_to_xml", "cursor_to_xmlschema",
    "table_to_xml", "table_to_xmlschema", "table_to_xml_and_xmlschema",
    "schema_to_xml", "schema_to_xmlschema", "schema_to_xml_and_xmlschema",
    "database_to_xml", "database_to_xmlschema", "database_to_xml_and_xmlschema",
}

# Catalogs holding password hashes / connection strings. SELECT-able by a
# superuser, and a client-supplied "read-only" role is not always one.
_FORBIDDEN_TABLES = {
    "pg_authid", "pg_shadow", "pg_user_mapping", "pg_largeobject",
}

# ── Phase 14 (WH-06): user-mapping catalogs and postgres_fdw connection functions ──
# The table check compares only the unqualified relation name, so every
# schema-qualified form is caught; leading underscores are not stripped.
_FORBIDDEN_TABLES |= {
    "pg_user_mappings", "user_mapping_options", "user_mappings",
    "_pg_user_mappings", "foreign_server_options", "pg_foreign_server",
    "pg_foreign_data_wrapper", "_pg_foreign_servers",
    "_pg_foreign_data_wrappers", "foreign_data_wrapper_options",
}
_FORBIDDEN_FUNCTIONS |= {
    "postgres_fdw_get_connections", "postgres_fdw_disconnect",
    "postgres_fdw_disconnect_all",
}
# ── End Phase 14 (WH-06) ──


class SQLGuardrailError(Exception):
    """Raised when SQL fails read-only validation."""


# ── Phase 22 (DCAP-08): operator timezone for dashboard reads ──
_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")


def _checked_timezone(tz) -> str:
    """A valid IANA zone name or SQLGuardrailError. The result is safe to place in a
    single-quoted SQL literal (the regex admits no quote)."""
    if not isinstance(tz, str) or not _TZ_RE.match(tz):
        raise SQLGuardrailError("invalid timezone")
    try:
        zoneinfo.ZoneInfo(tz)
    except Exception:
        raise SQLGuardrailError("invalid timezone") from None
    return tz
# ── End Phase 22 ──


# ── Phase 14 (WH-03): warehouse reader target. When warehouse.warehouse_mode() is true every tenant read goes to the warehouse as wh_reader; there is no silent fallback to the direct business database (D-02). ──
class WarehouseUnreachable(ConnectionError):
    """The warehouse itself cannot be reached. ConnectionError keeps every
    consumer's unavailable/connection classification identical to v1.0."""

    def __init__(self, msg: str = "The warehouse is unreachable."):
        super().__init__(msg)


class BusinessDbUnreachable(ConnectionError):
    """The warehouse is up but the legacy business database behind the FDW is not."""

    def __init__(self, msg: str = "The business database is unreachable through the warehouse."):
        super().__init__(msg)


# ── Phase 15 (SRC-07): registered sources through the warehouse ──
class SourceUnreachable(ConnectionError):
    """A live source behind a src_ schema is unreachable through the warehouse."""

    def __init__(self, msg: str = "A live data source is unreachable through the warehouse."):
        super().__init__(msg)


_SRC_REF = re.compile(r'\bsrc_([a-z][a-z0-9_]{1,30})\b', re.I)


async def _warehouse_allowed() -> bool:
    if _business_url_set():
        return True
    from . import sourcesvc
    try:
        return await sourcesvc.has_active_sources()
    except Exception:  # registry unavailable: behave as v1.0 (not configured)
        return False
# ── End Phase 15 (SRC-07) ──


_FDW_UNREACHABLE = {"08001", "08006", "HV00N"}
_NOT_CONFIGURED = ("Business database is not configured yet — set it on the dashboard "
                   "Setup page (Business data card).")
_warehouse_pool: asyncpg.Pool | None = None
_warehouse_pool_settings: dict | None = None


def _business_url_set() -> bool:
    s = get_settings()
    return bool(s.tenant_database_url or s.business_database_url)


# ── Quick 033: one rule for "tenant reads can run" ──
def reads_configured() -> bool:
    """True when run_readonly_sql has somewhere to read: a business database URL, or the
    warehouse (a sources-only install reads its registered src_ tables there). Background
    loops that read through run_readonly_sql gate on this, never on the business URL alone.
    Sources-only = reads_configured() and not _business_url_set()."""
    return _business_url_set() or warehouse.warehouse_mode()
# ── End Quick 033 ──


async def _get_warehouse_pool() -> asyncpg.Pool:
    global _warehouse_pool, _warehouse_pool_settings
    if not await _warehouse_allowed():  # Phase 15: registered sources also allow the warehouse path
        raise SQLGuardrailError(_NOT_CONFIGURED)
    cur = warehouse.reader_server_settings()
    if _warehouse_pool is not None and _warehouse_pool_settings != cur:
        old, _warehouse_pool = _warehouse_pool, None
        try:
            await old.close()
        except Exception:
            pass
    if _warehouse_pool is None:
        _warehouse_pool = await asyncpg.create_pool(
            **warehouse.connect_kwargs("reader"),
            min_size=0,
            max_size=5,
            max_inactive_connection_lifetime=300,
            timeout=10,
            server_settings=cur,
        )
        _warehouse_pool_settings = dict(cur)
    return _warehouse_pool


async def _run_on_warehouse(validated: str, limit: int, timezone: str | None = None) -> list[dict]:
    """Run validated read-only SQL on the warehouse as wh_reader.

    The not-configured error is raised before the warehouse is touched, so health
    and selftest code must never use run_readonly_sql to judge whether the
    warehouse itself is reachable; probe it with a short-lived
    warehouse.connect("reader") connection instead (plan 14-07).
    """
    if not await _warehouse_allowed():  # Phase 15: registered sources also allow the warehouse path
        raise SQLGuardrailError(_NOT_CONFIGURED)
    if _business_url_set():
        await warehouse.wait_linked(15)
    wrapped = f"SELECT * FROM ({validated}) AS _guardrail_scope LIMIT {int(limit) + 1}"
    try:
        pool = await _get_warehouse_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                if timezone is not None:
                    # Phase 22 (DCAP-08): a session parameter for this transaction only; not a write (PostgreSQL refuses writes in a read-only transaction regardless)
                    await conn.execute(f"SET LOCAL TimeZone TO '{timezone}'")
                try:
                    # savepoint: a syntax error must not abort the outer transaction
                    async with conn.transaction():
                        rows = await conn.fetch(wrapped)
                except asyncpg.PostgresSyntaxError:
                    rows = await conn.fetch(validated)
    except SQLGuardrailError:
        raise
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):
        warehouse.request_relink("reader auth failed", bootstrap=True)
        raise WarehouseUnreachable() from None
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        src = _SRC_REF.search(validated)  # Phase 15
        if src:
            from . import syncsched
            syncsched.request_reprovision(src.group(1).lower())
        else:
            warehouse.request_relink("undefined object")
        raise
    except asyncpg.PostgresError as e:
        if getattr(e, "sqlstate", None) in _FDW_UNREACHABLE:
            if _SRC_REF.search(validated):  # Phase 15
                raise SourceUnreachable() from None
            raise BusinessDbUnreachable() from None
        if isinstance(e, (asyncpg.CannotConnectNowError, asyncpg.PostgresConnectionError)):
            raise WarehouseUnreachable() from None
        raise
    except (OSError, asyncio.TimeoutError, asyncpg.InterfaceError):
        raise WarehouseUnreachable() from None
    return [dict(r) for r in rows[:limit]]
# ── End Phase 14 (WH-03) ──


def validate_readonly_sql(sql: str) -> str:
    """Validate that `sql` is a single read-only statement. Returns the SQL."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise SQLGuardrailError("Empty SQL statement.")
    if ";" in stripped:
        raise SQLGuardrailError("Multiple statements are not allowed.")

    try:
        statements = sqlglot.parse(stripped, dialect="postgres")
    except sqlglot.errors.ParseError as e:
        raise SQLGuardrailError(f"SQL could not be parsed: {e}") from e

    if len(statements) != 1 or statements[0] is None:
        raise SQLGuardrailError("Exactly one statement is required.")

    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise SQLGuardrailError(
            f"Only SELECT queries are allowed (got {type(root).__name__})."
        )

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardrailError(
                f"Forbidden operation inside query: {type(node).__name__}."
            )
        # Function calls: sqlglot maps known ones to typed nodes and unknown
        # ones to Anonymous, so check both by name.
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = (node.name if isinstance(node, exp.Anonymous)
                    else node.sql_name()) or ""
            if name.lower().lstrip("_") in _FORBIDDEN_FUNCTIONS:
                raise SQLGuardrailError(
                    f"Forbidden function: {name}(). This query is blocked because "
                    f"that function reads outside the database or opens a second "
                    f"connection the read-only session cannot govern."
                )
        if isinstance(node, exp.Table) and node.name.lower() in _FORBIDDEN_TABLES:
            raise SQLGuardrailError(f"Forbidden system catalog: {node.name}.")

    return stripped


async def get_business_pool() -> asyncpg.Pool:
    """Connection pool for the agent's SQL tool, forced read-only at session level.

    Targets the tenant's business-data DB when TENANT_DATABASE_URL is set,
    otherwise falls back to the business knowledge DB.
    """
    global _caisaas_pool
    if warehouse.warehouse_mode():
        return await _get_warehouse_pool()  # Phase 14 (WH-03)
    if _caisaas_pool is None:
        settings = get_settings()
        target = settings.tenant_database_url or settings.business_database_url
        if not target:
            raise SQLGuardrailError(
                "Business database is not configured yet — set it on the dashboard "
                "Setup page (Business data card).")
        url = re.sub(r"^postgresql\+\w+://", "postgresql://", target)
        _caisaas_pool = await asyncpg.create_pool(
            url,
            min_size=0,
            max_size=5,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": "30000",  # 30s
            },
        )
    return _caisaas_pool


async def reset_business_pool() -> None:
    """Close and drop the pool so the next query picks up changed config."""
    global _caisaas_pool
    pool, _caisaas_pool = _caisaas_pool, None
    if pool is not None:
        try:
            await pool.close()
        except Exception:
            pass
    # Phase 14 (WH-03): also close the warehouse reader pool
    global _warehouse_pool, _warehouse_pool_settings
    wpool, _warehouse_pool, _warehouse_pool_settings = _warehouse_pool, None, None
    if wpool is not None:
        try:
            await wpool.close()
        except Exception:
            pass


# Backward-compat alias for external scripts.
get_caisaas_pool = get_business_pool


async def run_readonly_sql(sql: str, limit: int | None = None,
                           timezone: str | None = None) -> list[dict]:
    """THE shared read-only DB gateway. Every consumer that reads the tenant DB —
    the reactive `run_sql` tool (answering questions), the sentinel/watchdog, the
    DB poller (watching for new rows), and investigate_case — goes through here, so
    the read-only + AST + timeout + row-limit guardrails are enforced uniformly in
    one place. Any new DB-reading tool should call this rather than opening its own
    connection. Validate, then execute with the row limit applied. `limit`
    overrides sql_row_limit for one call (Quick 004: run_sql_to_csv's export cap).
    `timezone` (Phase 22) runs the read with SET LOCAL TimeZone for dashboard day buckets."""
    validated = validate_readonly_sql(sql)
    if timezone is not None:
        timezone = _checked_timezone(timezone)
    settings = get_settings()
    limit = int(limit) if limit else settings.sql_row_limit
    if warehouse.warehouse_mode():
        if timezone is None:
            return await _run_on_warehouse(validated, limit)  # Phase 14 (WH-03)
        return await _run_on_warehouse(validated, limit, timezone)  # Phase 22
    pool = await get_business_pool()
    # Apply the cap SERVER-side. Slicing in Python happens only after the whole
    # result set is materialised in the gateway: one seq scan of a few million
    # rows OOMs a process sharing 8 GB with Postgres, FalkorDB and Langfuse,
    # well inside the 30s statement timeout. Fetch limit+1 so we can still tell
    # the model its answer was truncated.
    wrapped = f"SELECT * FROM ({validated}) AS _guardrail_scope LIMIT {int(limit) + 1}"
    async with pool.acquire() as conn:
        # Session is read-only; belt-and-suspenders: read-only transaction too.
        async with conn.transaction(readonly=True):
            if timezone is not None:
                # Phase 22 (DCAP-08): a session parameter for this transaction only; not a write (PostgreSQL refuses writes in a read-only transaction regardless)
                await conn.execute(f"SET LOCAL TimeZone TO '{timezone}'")
            try:
                async with conn.transaction():  # savepoint (see warehouse path)
                    rows = await conn.fetch(wrapped)
            except asyncpg.PostgresSyntaxError:
                # Some valid top-level forms do not survive being nested as a
                # subquery (e.g. a bare VALUES list). Fall back to the original
                # statement rather than refusing a legitimate read.
                rows = await conn.fetch(validated)
    return [dict(r) for r in rows[:limit]]


# ── Layer 0 (recommended, run once on the CAISAAS dev DB) ──────────────
# Phase 11: superseded by deploy/readonly-role.sql + agentctl readonly-role (GOLIVE-03); kept because docs reference it
READONLY_ROLE_SQL = """
CREATE ROLE agent_readonly LOGIN PASSWORD '<choose-a-password>';
GRANT CONNECT ON DATABASE <dbname> TO agent_readonly;
GRANT USAGE ON SCHEMA public TO agent_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_readonly;
"""
