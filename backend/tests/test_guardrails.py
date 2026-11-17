"""SQL guardrails — the AST layer (pure; no DB).

The agent's SQL is model-authored, and the model's input includes untrusted
text (uploaded documents, polled DB rows, web results). Everything here is
reachable by prompt injection, so "it parses as a SELECT" is not sufficient.
"""

import pytest

from app.guardrails import SQLGuardrailError, validate_readonly_sql


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT * FROM invoices WHERE amount > 100",
    "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
    "SELECT count(*), sum(amount) FROM orders GROUP BY customer_id",
    "SELECT a.id, b.name FROM orders a JOIN customers b ON a.cust = b.id LIMIT 10",
    "SELECT * FROM shipments UNION SELECT * FROM archived_shipments",
])
def test_ordinary_reads_are_allowed(sql):
    assert validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM invoices",
    "UPDATE invoices SET amount = 0",
    "DROP TABLE invoices",
    "SELECT 1; DROP TABLE invoices",
    "WITH x AS (DELETE FROM invoices RETURNING id) SELECT * FROM x",
])
def test_writes_are_blocked(sql):
    with pytest.raises(SQLGuardrailError):
        validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    # Host filesystem reads — these parse as perfectly ordinary SELECTs.
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/proc/self/environ')",
    "SELECT pg_ls_dir('/')",
    "SELECT lo_import('/etc/shadow')",
    # dblink opens a SECOND connection, whose writes the read-only session
    # flag on OUR connection does not govern.
    "SELECT dblink_exec('dbname=x', 'DELETE FROM invoices')",
    # Denial of service that fits inside the 30s statement timeout.
    "SELECT pg_sleep(100)",
    # Credential catalogs.
    "SELECT * FROM pg_authid",
    "SELECT rolpassword FROM pg_shadow",
    # Clearing the read-only flag from inside the query.
    "SELECT set_config('default_transaction_read_only', 'off', false)",
])
def test_side_effecting_functions_and_catalogs_are_blocked(sql):
    with pytest.raises(SQLGuardrailError):
        validate_readonly_sql(sql)


def test_forbidden_function_message_explains_why():
    with pytest.raises(SQLGuardrailError, match="Forbidden function"):
        validate_readonly_sql("SELECT pg_read_file('/etc/passwd')")


# ── Phase 14 (WH-06) ──
@pytest.mark.parametrize("sql", [
    "SELECT * FROM pg_user_mappings",
    "SELECT * FROM pg_catalog.pg_user_mappings",
    'SELECT * FROM "pg_user_mappings"',
    "SELECT * FROM PG_USER_MAPPINGS",
    "SELECT * FROM information_schema.user_mapping_options",
    "SELECT * FROM information_schema.user_mappings",
    "SELECT * FROM information_schema._pg_user_mappings",
    "SELECT * FROM information_schema.foreign_server_options",
    "SELECT srvoptions FROM pg_foreign_server",
    "SELECT * FROM pg_catalog.pg_foreign_data_wrapper",
    "SELECT * FROM information_schema._pg_foreign_servers",
    "SELECT * FROM information_schema._pg_foreign_data_wrappers",
    "SELECT * FROM information_schema.foreign_data_wrapper_options",
    "WITH m AS (SELECT * FROM pg_user_mappings) SELECT * FROM m",
    "SELECT 1 FROM regions r JOIN information_schema.user_mapping_options o ON true",
    "SELECT (SELECT count(*) FROM pg_user_mappings)",
    "SELECT postgres_fdw_get_connections()",
    "SELECT * FROM ic_ext.postgres_fdw_get_connections()",
    "SELECT postgres_fdw_disconnect('business_db')",
    "SELECT postgres_fdw_disconnect_all()",
])
def test_phase14_mapping_catalogs_and_fdw_functions_refused(sql):
    with pytest.raises(SQLGuardrailError):
        validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM invoices",
    "SELECT * FROM public.invoices",
    "SELECT r.name, count(*) FROM regions r JOIN customers c ON c.region_id = r.id GROUP BY r.name",
    "SELECT * FROM ic_meta.source_catalog",
])
def test_phase14_legit_reads_still_allowed(sql):
    assert validate_readonly_sql(sql)


# ── Phase 14 (WH-03): warehouse-mode guardrails ──
import asyncio  # noqa: E402

import asyncpg  # noqa: E402

from app import dashboard_refresh, dashboardtools, guardrails, warehouse  # noqa: E402
from app.config import get_settings  # noqa: E402


class _PgErr(asyncpg.PostgresError):
    pass


def _err(state, msg="could not connect to server db.internal.example port 5432 user bob"):
    e = _PgErr(msg)
    e.sqlstate = state
    return e


class _Tx:
    def __init__(self, ro):
        self.ro = ro

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, fetch):
        self._fetch, self.sqls, self.ro = fetch, [], None

    def transaction(self, readonly=False):
        self.ro = bool(self.ro) or readonly  # nested savepoints keep the outer flag
        return _Tx(readonly)

    async def fetch(self, sql):
        self.sqls.append(sql)
        r = self._fetch(sql)
        if isinstance(r, BaseException):
            raise r
        return r


class _Acq:
    def __init__(self, conn, exc):
        self.conn, self.exc = conn, exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn=None, acquire_exc=None):
        self.conn, self.exc, self.closed = conn, acquire_exc, False

    def acquire(self):
        return _Acq(self.conn, self.exc)

    async def close(self):
        self.closed = True


def _reset_pools():
    guardrails._warehouse_pool = None
    guardrails._warehouse_pool_settings = None
    guardrails._caisaas_pool = None


@pytest.fixture
def whmode(monkeypatch):
    envs = {"WAREHOUSE_URL": "postgresql://wh:5432/warehouse", "WH_READER_PASSWORD": "rpw-1234567",
            "TENANT_DATABASE_URL": "postgresql://u:p@legacy:5432/biz"}
    for k, v in envs.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    warehouse._reset_state_for_tests()
    _reset_pools()
    calls = {"relink": []}

    async def fast_wait(timeout=15.0):
        calls["waited"] = calls.get("waited", 0) + 1
        return True
    monkeypatch.setattr(warehouse, "wait_linked", fast_wait)
    monkeypatch.setattr(warehouse, "request_relink",
                        lambda reason, bootstrap=False: calls["relink"].append((reason, bootstrap)))
    yield calls
    get_settings.cache_clear()
    warehouse._reset_state_for_tests()
    _reset_pools()


@pytest.fixture
def v1mode(monkeypatch):
    for k in ("WAREHOUSE_URL", "WH_READER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql+asyncpg://u:p@legacy:5432/biz")
    get_settings.cache_clear()
    _reset_pools()
    yield
    _reset_pools()
    get_settings.cache_clear()


def _recorder(monkeypatch, pool=None):
    rec = {}

    async def create_pool(*a, **kw):
        rec["args"], rec["kw"] = a, kw
        return pool or _Pool()
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return rec


async def test_v1_pool_call_unchanged(monkeypatch, v1mode):
    rec = _recorder(monkeypatch)
    await guardrails.get_business_pool()
    assert rec["args"] == ("postgresql://u:p@legacy:5432/biz",)
    assert rec["kw"] == {"min_size": 0, "max_size": 5, "server_settings": {
        "default_transaction_read_only": "on", "statement_timeout": "30000"}}


async def test_warehouse_pool_args(monkeypatch, whmode):
    rec = _recorder(monkeypatch)
    await guardrails.get_business_pool()
    kw = rec["kw"]
    assert rec["args"] == ()
    assert kw["dsn"] == "postgresql://wh:5432/warehouse"
    assert kw["user"] == "wh_reader" and kw["password"] == "rpw-1234567"
    assert (kw["min_size"], kw["max_size"], kw["max_inactive_connection_lifetime"],
            kw["timeout"]) == (0, 5, 300, 10)
    assert kw["server_settings"] == warehouse.reader_server_settings()
    assert "legacy" not in repr(rec)


async def test_warehouse_not_configured_when_no_tenant_url(monkeypatch, whmode):
    monkeypatch.setenv("TENANT_DATABASE_URL", "")
    monkeypatch.setenv("BUSINESS_DATABASE_URL", "")
    monkeypatch.setenv("CAISAAS_DATABASE_URL", "")
    get_settings.cache_clear()
    with pytest.raises(guardrails.SQLGuardrailError, match="not configured yet"):
        await guardrails.run_readonly_sql("SELECT 1")


@pytest.mark.parametrize("exc", [OSError("x"), ConnectionRefusedError("x"),
                                 asyncio.TimeoutError(), asyncpg.CannotConnectNowError("x")])
async def test_warehouse_unreachable(monkeypatch, whmode, exc):
    _recorder(monkeypatch, _Pool(acquire_exc=exc))
    with pytest.raises(guardrails.WarehouseUnreachable) as ei:
        await guardrails.run_readonly_sql("SELECT 1")
    assert str(ei.value) == "The warehouse is unreachable."


async def test_invalid_password_requests_bootstrap(monkeypatch, whmode):
    _recorder(monkeypatch, _Pool(acquire_exc=asyncpg.InvalidPasswordError("password for wh_reader")))
    with pytest.raises(guardrails.WarehouseUnreachable):
        await guardrails.run_readonly_sql("SELECT 1")
    assert whmode["relink"] == [("reader auth failed", True)]


@pytest.mark.parametrize("state", ["08001", "08006", "HV00N"])
async def test_business_db_unreachable(monkeypatch, whmode, state):
    conn = _Conn(lambda sql: _err(state))
    _recorder(monkeypatch, _Pool(conn))
    with pytest.raises(guardrails.BusinessDbUnreachable) as ei:
        await guardrails.run_readonly_sql("SELECT 1")
    assert str(ei.value) == "The business database is unreachable through the warehouse."
    assert "db.internal" not in str(ei.value) and ei.value.__cause__ is None
    assert ei.value.__suppress_context__


@pytest.mark.parametrize("cls", [asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError])
async def test_undefined_object_relinks_and_reraises(monkeypatch, whmode, cls):
    original = cls("relation x does not exist")
    _recorder(monkeypatch, _Pool(_Conn(lambda sql: original)))
    with pytest.raises(cls) as ei:
        await guardrails.run_readonly_sql("SELECT * FROM x")
    assert ei.value is original
    assert whmode["relink"] == [("undefined object", False)]


def test_error_classes_are_connection_errors():
    assert issubclass(guardrails.WarehouseUnreachable, ConnectionError)
    assert issubclass(guardrails.BusinessDbUnreachable, ConnectionError)
    assert dashboard_refresh.categorize(guardrails.BusinessDbUnreachable()) == "unavailable"
    assert dashboardtools._is_conn_error(guardrails.WarehouseUnreachable()) is True


async def test_warehouse_keeps_wrapper_limit_and_readonly(monkeypatch, whmode):
    limit = get_settings().sql_row_limit
    conn = _Conn(lambda sql: [{"n": i} for i in range(limit + 1)])
    _recorder(monkeypatch, _Pool(conn))
    rows = await guardrails.run_readonly_sql("SELECT n FROM t;")
    assert conn.sqls == [f"SELECT * FROM (SELECT n FROM t) AS _guardrail_scope LIMIT {limit + 1}"]
    assert conn.ro is True and len(rows) == limit
    assert whmode["waited"] == 1


async def test_warehouse_syntax_fallback(monkeypatch, whmode):
    def fetch(sql):
        return asyncpg.PostgresSyntaxError("x") if "_guardrail_scope" in sql else [{"a": 1}]
    conn = _Conn(fetch)
    _recorder(monkeypatch, _Pool(conn))
    assert await guardrails.run_readonly_sql("SELECT 1 AS a") == [{"a": 1}]
    assert conn.sqls[-1] == "SELECT 1 AS a"


async def test_warehouse_still_validates(whmode):
    with pytest.raises(guardrails.SQLGuardrailError):
        await guardrails.run_readonly_sql("DELETE FROM t")


async def test_pool_rebuilt_when_session_settings_change(monkeypatch, whmode):
    pools = []

    async def create_pool(*a, **kw):
        p = _Pool()
        pools.append(p)
        return p
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    first = await guardrails.get_business_pool()
    assert await guardrails.get_business_pool() is first
    warehouse.mark_linked({"TimeZone": "Asia/Kolkata"})
    second = await guardrails.get_business_pool()
    assert second is not first and first.closed is True and len(pools) == 2


async def test_reset_closes_both_pools(whmode):
    direct, wh = _Pool(), _Pool()
    guardrails._caisaas_pool, guardrails._warehouse_pool = direct, wh
    await guardrails.reset_business_pool()
    assert direct.closed and wh.closed
    assert guardrails._caisaas_pool is None and guardrails._warehouse_pool is None
