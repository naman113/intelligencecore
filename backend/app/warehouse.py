"""Warehouse plumbing seam (Phase 14, WH-02 / WH-03).

Who may do what in the warehouse Postgres:

* ``bootstrap()`` (this module, run as the warehouse superuser) is the only
  creator of the warehouse roles, extensions and the ``ic_*`` control schemas.
* ``whlink.py`` (plans 14-04 and 14-05, run as ``wh_owner``) is the only
  creator of foreign servers, user mappings, foreign tables and mirrored views.
* The app reads business data only through ``guardrails`` as ``wh_reader``.

Credentials are never formed into a DSN string: ``connect_kwargs`` returns a
credential-free ``dsn`` plus separate ``user`` and ``password`` values. Logs
carry role names and hosts only, never DSNs, passwords or statements.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from .config import get_settings

log = logging.getLogger("warehouse")


@dataclass(frozen=True)
class Roles:
    owner: str = "wh_owner"
    loader: str = "wh_loader"
    reader: str = "wh_reader"


DEFAULT_ROLES = Roles()


class WarehouseNotConfigured(RuntimeError):
    """WAREHOUSE_URL is not set."""


def warehouse_mode() -> bool:
    """The single mode switch: the app reads through the warehouse only when
    both the warehouse address and the reader password are configured."""
    s = get_settings()
    return bool(s.warehouse_url and s.wh_reader_password)


def _clean_dsn(url: str) -> str:
    """Normalise the driver prefix and strip any userinfo from the URL."""
    u = (url or "").strip()
    if u.startswith("postgresql+"):
        u = "postgresql://" + u.split("://", 1)[1]
    elif u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    p = urlsplit(u)
    netloc = p.netloc.rsplit("@", 1)[-1]
    return urlunsplit((p.scheme, netloc, p.path, p.query, ""))


def connect_kwargs(role: str, roles: Roles = DEFAULT_ROLES) -> dict:
    """asyncpg keyword arguments for ``role`` (admin, owner, loader, reader)."""
    s = get_settings()
    if not s.warehouse_url:
        raise WarehouseNotConfigured("WAREHOUSE_URL is not set.")
    if role == "admin":
        user, password = s.warehouse_admin_user, s.warehouse_admin_password
    elif role == "owner":
        user, password = roles.owner, s.wh_owner_password
    elif role == "loader":
        user, password = roles.loader, s.wh_loader_password
    elif role == "reader":
        user, password = roles.reader, s.wh_reader_password
    else:
        raise ValueError(f"unknown warehouse role: {role}")
    return {"dsn": _clean_dsn(s.warehouse_url), "user": user, "password": password}


async def connect(role: str, *, roles: Roles = DEFAULT_ROLES, timeout: float = 10.0,
                  server_settings: dict | None = None) -> asyncpg.Connection:
    return await asyncpg.connect(
        **connect_kwargs(role, roles), timeout=timeout, server_settings=server_settings)


async def exec_fmt(conn, template: str, *values: str) -> None:
    """Build a statement with the server's format() (%I identifiers, %L literals)
    and execute exactly what the server returned. Never logs the statement."""
    # VARIADIC array: a plain call with one argument per value fails at 100
    # arguments (wide tables); an array counts as one argument.
    if values:
        stmt = await conn.fetchval(
            "SELECT format($1, VARIADIC $2::text[])", template, list(values))
    else:
        stmt = await conn.fetchval("SELECT format($1)", template)
    await conn.execute(stmt)


# ── Link state (loop-agnostic; written by the linker, read by guardrails) ──
RELINK_DEBOUNCE_SECONDS = 30
_relink_event: asyncio.Event | None = None
_bootstrap_wanted = False
_last_relink = 0.0
_relink_requests: list[str] = []
_linked = False
_wait_done = False
_session: dict = {}


def get_relink_event() -> asyncio.Event:
    global _relink_event
    if _relink_event is None:
        _relink_event = asyncio.Event()
    return _relink_event


def request_relink(reason: str, *, bootstrap: bool = False) -> None:
    """Ask the linker loop to relink (debounced). Never raises."""
    global _bootstrap_wanted, _last_relink
    try:
        now = time.monotonic()
        if not bootstrap and _last_relink and now - _last_relink < RELINK_DEBOUNCE_SECONDS:
            return
        _last_relink = now
        if bootstrap:
            _bootstrap_wanted = True
        _relink_requests.append(str(reason))
        del _relink_requests[:-50]
        try:
            get_relink_event().set()
        except Exception:
            pass
        log.info("warehouse relink requested: %s", reason)
    except Exception:
        pass


def mark_linked(session: dict | None = None) -> None:
    global _linked
    if session:
        for k in ("TimeZone", "search_path"):
            if session.get(k):
                _session[k] = str(session[k])
    _linked = True


async def wait_linked(timeout: float = 15.0) -> bool:
    """One bounded wait for the first link after boot, then a latch: a query
    never waits twice."""
    global _wait_done
    if _linked:
        return True
    if _wait_done:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _linked:
            return True
        await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    _wait_done = True
    return _linked


def reader_server_settings() -> dict:
    out = {"default_transaction_read_only": "on", "statement_timeout": "30000"}
    out.update(_session)
    return out


def _reset_state_for_tests() -> None:
    global _relink_event, _bootstrap_wanted, _last_relink, _linked, _wait_done
    _relink_event = None
    _bootstrap_wanted = False
    _last_relink = 0.0
    _relink_requests.clear()
    _linked = False
    _wait_done = False
    _session.clear()
    _reset_loop_state_for_tests()


# ── Bootstrap (superuser; every start and after a lost volume; never initdb.d, D-10) ──
_NOPRIV = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"


async def _extension_available(conn, name: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM pg_available_extensions WHERE name = $1", name))


async def bootstrap(conn=None, *, roles: Roles = DEFAULT_ROLES,
                    passwords: dict | None = None) -> dict:
    """Provision roles, schemas, extensions and grants idempotently."""
    s = get_settings()
    pw = {"owner": s.wh_owner_password, "loader": s.wh_loader_password,
          "reader": s.wh_reader_password}
    pw.update(passwords or {})
    own = conn is None
    if own:
        if not s.warehouse_admin_password or not s.warehouse_url:
            log.warning("warehouse bootstrap skipped: no warehouse admin password")
            return {"skipped": "no warehouse admin password"}
        conn = await connect("admin", roles=roles)
    try:
        return await _bootstrap(conn, roles, pw)
    finally:
        if own:
            try:
                await conn.close()
            except Exception:
                pass


async def _bootstrap(conn, roles: Roles, pw: dict) -> dict:
    await conn.execute("SET log_min_error_statement = panic")
    db = await conn.fetchval("SELECT current_database()")
    ow, lo, rd = roles.owner, roles.loader, roles.reader
    x = lambda tpl, *v: exec_fmt(conn, tpl, *v)  # noqa: E731

    for r in (ow, lo, rd):
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", r):
            await x("CREATE ROLE %I LOGIN", r)
        await x(f"ALTER ROLE %I {_NOPRIV}", r)
    await x("ALTER ROLE %I CONNECTION LIMIT 20", rd)

    await x("ALTER ROLE %I SET default_transaction_read_only = on", rd)
    await x("ALTER ROLE %I SET statement_timeout = '30s'", rd)
    await x("ALTER ROLE %I SET idle_in_transaction_session_timeout = '60s'", rd)
    await x("ALTER ROLE %I SET search_path = public", rd)
    await x("ALTER ROLE %I SET log_min_error_statement = 'panic'", ow)
    await x("ALTER ROLE %I SET log_statement = 'none'", ow)
    await x("ALTER ROLE %I SET lock_timeout = '5s'", ow)

    await x("REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC", db)
    for r in (ow, lo, rd):
        await x("GRANT CONNECT ON DATABASE %I TO %I", db, r)
    await x("GRANT CREATE, TEMPORARY ON DATABASE %I TO %I", db, ow)

    await conn.execute("CREATE SCHEMA IF NOT EXISTS ic_ext")
    await conn.execute("REVOKE ALL ON SCHEMA ic_ext FROM PUBLIC")
    for sch in ("ic_meta", "ic_probe"):
        await x(f"CREATE SCHEMA IF NOT EXISTS {sch} AUTHORIZATION %I", ow)
        await x(f"GRANT USAGE ON SCHEMA {sch} TO %I", rd)
    # Phase 14 gap closure (14-12): schema for source extension types (D-06: never public)
    await x("CREATE SCHEMA IF NOT EXISTS ic_types AUTHORIZATION %I", ow)
    await conn.execute("REVOKE ALL ON SCHEMA ic_types FROM PUBLIC")
    await x("GRANT USAGE ON SCHEMA ic_types TO %I", rd)
    # End Phase 14 gap closure (14-12)
    await conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    await x("GRANT USAGE, CREATE ON SCHEMA public TO %I", ow)
    await x("GRANT USAGE ON SCHEMA public TO %I", rd)

    await conn.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw SCHEMA ic_ext")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements SCHEMA ic_ext")
    ext = {"postgres_fdw": True, "pg_stat_statements": True}
    for name in ("mysql_fdw", "tds_fdw"):
        ok = False
        try:
            if await _extension_available(conn, name):
                async with conn.transaction():
                    await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {name} SCHEMA ic_ext")
                ok = True
        except asyncpg.PostgresError:
            log.warning("warehouse extension %s could not be created", name)
        if not ok:
            log.warning("warehouse extension %s is not available", name)
        ext[name] = ok
    await x("GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO %I", ow)
    await conn.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ic_ext FROM PUBLIC")

    for key, r in (("owner", ow), ("loader", lo), ("reader", rd)):
        if pw.get(key):
            await x("ALTER ROLE %I PASSWORD %L", r, pw[key])
    p15 = await _bootstrap_p15(conn, roles, ext)  # Phase 15
    log.info("warehouse bootstrap done: roles %s, %s, %s", ow, lo, rd)
    return {"roles": [ow, lo, rd], "extensions": ext, "p15": p15}


# ── Phase 15 (SRC-05, SRC-07): loader and FDW grants ──
async def _bootstrap_p15(conn, roles: Roles, ext: dict) -> dict:
    """Rights Phase 15 needs on top of Phase 14. The loader never gets CREATE on the database:
    wh_owner creates its schemas and grants it USAGE, CREATE on those schemas only."""
    ow, lo = roles.owner, roles.loader
    x = lambda tpl, *v: exec_fmt(conn, tpl, *v)  # noqa: E731
    granted = []
    for fdw in ("mysql_fdw", "tds_fdw"):
        if ext.get(fdw):
            await x(f"GRANT USAGE ON FOREIGN DATA WRAPPER {fdw} TO %I", ow)
            granted.append(fdw)
    await x("ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT SELECT ON TABLES TO %I", lo, ow)
    await x("ALTER ROLE %I CONNECTION LIMIT 5", lo)
    await x("ALTER ROLE %I SET statement_timeout = '30min'", lo)
    await x("ALTER ROLE %I SET lock_timeout = '30s'", lo)
    await x("ALTER ROLE %I SET idle_in_transaction_session_timeout = '10min'", lo)
    await x("ALTER ROLE %I SET log_min_error_statement = 'panic'", lo)
    return {"fdw_grants": granted, "loader_connection_limit": 5}
# ── End Phase 15 ──


class _Rollback(Exception):
    pass


async def check_reader(conn, *, roles: Roles = DEFAULT_ROLES,
                       server: str = "business_db") -> tuple[bool, str]:
    """Prove the current connection is a read-only, mapping-blind reader."""
    proved: list[str] = []

    def fail(reason: str) -> tuple[bool, str]:
        return False, reason

    if await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
        return fail("role is a superuser")
    proved.append("not superuser")

    n_write = await conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname NOT LIKE 'pg\\_%' "
        "AND c.relkind IN ('r','p','v','m','f') "
        "AND (has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE') "
        "OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE'))")
    if n_write:
        return fail(f"{n_write} relation(s) have a write privilege")
    proved.append("no write privilege")

    n_create = await conn.fetchval(
        "SELECT count(*) FROM pg_namespace WHERE has_schema_privilege(oid,'CREATE') "
        "AND nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'")
    if n_create:
        return fail(f"CREATE allowed on {n_create} schema(s)")
    proved.append("no CREATE on any schema")

    if await conn.fetchval("SELECT 1 FROM pg_foreign_server WHERE srvname = $1", server):
        if await conn.fetchval("SELECT has_server_privilege($1, 'USAGE')", server):
            return fail(f"USAGE on foreign server {server}")
        proved.append("no server USAGE")
    else:
        proved.append("no server yet")

    member = await conn.fetchval(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1) "
        "THEN pg_has_role(current_user, $1, 'MEMBER') ELSE false END", roles.owner)
    if member:
        return fail(f"member of {roles.owner}")
    proved.append("not an owner member")

    if await conn.fetchval("SELECT count(*) FROM pg_user_mappings WHERE umoptions IS NOT NULL"):
        return fail("user-mapping options are visible")
    if await conn.fetchval("SELECT count(*) FROM information_schema.user_mapping_options"):
        return fail("user-mapping options are visible (information_schema)")
    proved.append("mapping options hidden")

    if (await conn.fetchval("SHOW default_transaction_read_only")) != "on":
        return fail("default_transaction_read_only is not on")
    proved.append("read-only default")

    created = False
    try:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ WRITE")
            await conn.execute("CREATE TEMP TABLE _ic_reader_probe (id int)")
            created = True
            raise _Rollback()
    except _Rollback:
        pass
    except asyncpg.PostgresError:
        pass
    if created:
        return fail("a forced read-write transaction could create a table")
    proved.append("write probe refused")
    return True, "; ".join(proved)


# ── Loop (D-10, D-18, WH-09): bootstrap + link at start, on lost ic_meta or auth failure, on a sweep, on request ──
WAREHOUSE_TICK_SECONDS = 30
SOURCE = "business_db"

_link_lock: asyncio.Lock | None = None
_need_bootstrap = True
_last_owner_bootstrap = -1e9
_last_sweep_at = 0.0
_last_link: dict = {"status": None, "tables_linked": 0, "detail": None,
                    "linked_at": None, "finished": 0.0, "seq": 0}


def _lock() -> asyncio.Lock:
    global _link_lock
    if _link_lock is None:
        _link_lock = asyncio.Lock()
    return _link_lock


def _business_url() -> str:
    s = get_settings()
    return (s.tenant_database_url or s.business_database_url or "").strip()


def last_link() -> dict:
    """The outcome of the most recent link attempt (no secrets)."""
    return dict(_last_link)


def _note_link(res: dict) -> None:
    global _last_sweep_at
    st = res.get("status")
    _last_link.update(status=st, tables_linked=res.get("tables_linked", 0),
                      detail=res.get("detail"), finished=time.monotonic(),
                      seq=_last_link["seq"] + 1)
    if st in ("ok", "stale"):
        _last_link["linked_at"] = time.time()
        _last_sweep_at = time.monotonic()
        mark_linked(res.get("session"))


async def _link_locked(url: str) -> dict:
    """Open a short-lived owner connection and link. Caller holds _link_lock."""
    from . import whlink
    conn = None
    try:
        conn = await connect("owner")
    except Exception as e:
        global _last_owner_bootstrap
        # WR-01: ask for a bootstrap only on a real login failure, at most every 5 minutes
        if _login_failed(e) and time.monotonic() - _last_owner_bootstrap > 300:
            _last_owner_bootstrap = time.monotonic()
            request_relink("owner login failed", bootstrap=True)
        res = {"status": "error", "detail": type(e).__name__}
        _note_link(res)
        return res
    # Phase 17 (CONS-05): tables the owner imported are kept out of the live sweep
    keep = await _load_keep_keys()
    if keep is None:
        # WR-01: fail closed, never sweep without knowing which tables are imported
        try:
            await conn.close()
        except Exception:
            pass
        return {"status": "busy", "detail": "keep-keys unavailable"}
    try:
        kw = dict(keep_keys=keep) if keep else {}
        res = await whlink.link_source(conn, SOURCE, url, **kw)
    finally:
        try:
            await conn.close()
        except Exception:
            pass
    if res.get("reverted"):
        await _mark_reimport(res["reverted"])
    if res.get("status") != "busy":
        _note_link(res)
    return res


async def _load_keep_keys() -> frozenset | None:
    # Phase 17 (CONS-05): agent_memory failure returns None and the sweep is skipped (fail closed)
    try:
        from .db import get_pool
        rows = await (await get_pool()).fetch(
            "SELECT t.remote_schema, t.remote_table FROM source_tables t "
            "JOIN sources s ON s.id = t.source_id WHERE s.name = 'business_db' AND t.mode = 'import'")
        return frozenset((r["remote_schema"], r["remote_table"]) for r in rows)
    except Exception as e:
        log.warning("keep-keys not loaded: %s", type(e).__name__)
        return None


async def _mark_reimport(reverted: list) -> None:
    # a keep-key recreated live re-imports itself on the next sync (WH-09 stays true)
    try:
        from .db import get_pool
        pool = await get_pool()
        for name in reverted:
            sch, _, tbl = name.partition(".")
            await pool.execute(
                "UPDATE source_tables t SET mode = 'live', pending_mode = 'import', "
                "backfill_done = false, backfill_progress = 0, import_reset = true "
                "FROM sources s WHERE s.id = t.source_id AND s.name = 'business_db' "
                "AND t.remote_schema = $1 AND t.remote_table = $2", sch, tbl)
    except Exception as e:
        log.warning("re-import mark failed: %s", type(e).__name__)


def _login_failed(e: Exception) -> bool:
    name = type(e).__name__
    return (name in ("InvalidPasswordError", "InvalidAuthorizationSpecificationError",
                     "InvalidCatalogNameError")
            or "does not exist" in str(e))


async def _bootstrap_now() -> bool:
    """Run bootstrap as the superuser; True when roles were provisioned."""
    global _need_bootstrap, _bootstrap_wanted
    res = await bootstrap()
    if isinstance(res, dict) and res.get("skipped"):
        return False
    _need_bootstrap = False
    _bootstrap_wanted = False
    try:
        from . import guardrails
        await guardrails.reset_business_pool()
    except Exception:
        log.warning("warehouse pool reset after bootstrap failed")
    return True


async def _tick() -> None:
    """One pass: bootstrap when needed, link when due."""
    ev = get_relink_event()
    requested = ev.is_set()
    ev.clear()
    if not warehouse_mode():
        return
    want_bootstrap = _need_bootstrap or _bootstrap_wanted
    meta_missing = False
    if not want_bootstrap:
        conn = None
        try:
            conn = await connect("owner")
            meta_missing = await conn.fetchval(
                "SELECT to_regclass('ic_meta.link_state') IS NULL")
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError):
            raise
        except Exception as e:
            if not _login_failed(e):
                raise
            want_bootstrap = True
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        if not want_bootstrap and not meta_missing:
            rconn = None
            try:
                rconn = await connect("reader")
            except (asyncpg.PostgresConnectionError, ConnectionError, OSError):
                raise
            except Exception as e:
                if not _login_failed(e):
                    raise
                want_bootstrap = True
            finally:
                if rconn is not None:
                    try:
                        await rconn.close()
                    except Exception:
                        pass
        if meta_missing:
            want_bootstrap = True
    bootstrapped = False
    if want_bootstrap:
        bootstrapped = await _bootstrap_now()
        if not bootstrapped:
            return
    url = _business_url()
    if not url:
        return
    sweep = max(1, int(get_settings().warehouse_link_sweep_seconds or 600))
    due = (bootstrapped or requested or _last_link["status"] is None
           or _last_link["status"] not in ("ok", "stale")
           or time.monotonic() - _last_sweep_at >= sweep)
    if not due:
        return
    async with _lock():
        await _link_locked(url)


async def load_link_state() -> bool:
    """Best effort at start: read the stored link as the reader so an intact
    volume serves queries before the first relink finishes."""
    if not warehouse_mode():
        return False
    conn = None
    try:
        conn = await connect("reader")
        row = await conn.fetchrow(
            "SELECT status, source_timezone, source_search_path FROM ic_meta.link_state "
            "WHERE source = $1", SOURCE)
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
    if not row or row["status"] != "ok":
        return False
    mark_linked({"TimeZone": row["source_timezone"], "search_path": row["source_search_path"]})
    return True


async def run_forever() -> None:
    """Never-die warehouse loop (worker convention)."""
    log.info("warehouse loop started")
    try:
        await load_link_state()
    except Exception:
        pass
    while True:
        try:
            await _tick()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("warehouse connection blip, retrying: %s", type(e).__name__)
        except Exception as e:
            log.exception("warehouse tick error")
            from . import jobruns  # M2 sync: Quick 008 loop-error record (never raises)
            await jobruns.loop_error("warehouse", e)
        try:
            await asyncio.wait_for(get_relink_event().wait(), WAREHOUSE_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _bounded(coro, timeout: float) -> dict:
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        return {"status": "timeout"}
    except Exception as e:
        return {"status": "error", "detail": type(e).__name__}


async def relink_now(reason: str, timeout: float = 20.0) -> dict:
    """Link now (used by the Setup save). Never raises."""
    async def go() -> dict:
        if not warehouse_mode() or not _business_url():
            return {"status": "not_configured"}
        async with _lock():
            return await _link_locked(_business_url())
    log.info("warehouse relink_now: %s", reason)
    # WR-05: never cancel a running link; on timeout hand it to the loop as due
    task = asyncio.ensure_future(go())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        _last_link["status"] = None
        get_relink_event().set()
        return {"status": "pending"}
    except Exception as e:
        return {"status": "error", "detail": type(e).__name__}


async def ensure_fresh(max_age: float = 0.0, timeout: float = 20.0) -> dict:
    """Make sure the link is current before discovery (D-18). Never raises."""
    started = _last_link["seq"]

    async def go() -> dict:
        if not warehouse_mode() or not _business_url():
            return {"status": "not_configured"}
        async with _lock():
            ok = _last_link["status"] in ("ok", "stale")
            fin = _last_link["finished"]
            if ok and (_last_link["seq"] > started or (max_age > 0 and time.monotonic() - fin < max_age)):
                return {"status": "fresh"}
            return await _link_locked(_business_url())
    return await _bounded(go(), timeout)


def _reset_loop_state_for_tests() -> None:
    global _link_lock, _need_bootstrap, _last_sweep_at
    _link_lock = None
    _need_bootstrap = True
    _last_sweep_at = 0.0
    _last_link.update(status=None, tables_linked=0, detail=None, linked_at=None, finished=0.0, seq=0)
