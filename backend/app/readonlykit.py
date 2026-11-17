"""Read-only role kit for the client's Postgres (Phase 11, GOLIVE-03).

Operator tool plumbing; the app itself never writes the tenant DB.

Two halves:
  * render/url/pgenv/CLI: validate inputs and build the SET lines that drive
    deploy/readonly-role.sql (used by `agentctl readonly-role`). `pgenv` splits the
    admin URL (from env) into libpq PG* variables so psql never gets it on argv.
  * check_role(conn): the proof behind selftest and the Setup page Test button.
    It checks EFFECTIVE privileges of the connected role: no write, no CREATE,
    revoked tables unreadable.
Secrets come from the environment only, never argv.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

REF_RE = re.compile(r"^business_db\.([^.\s]+)\.([^.\s]+)$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TIMEOUT_RE = re.compile(r"^\d{1,7}(ms|s|min)?$")
KIT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "readonly-role.sql"


def validate_ident(s: str) -> str:
    if not isinstance(s, str) or not IDENT_RE.match(s):
        raise ValueError(f"invalid identifier: {s!r}")
    return s


def validate_role(s: str) -> str:
    if not isinstance(s, str) or not ROLE_RE.match(s):
        raise ValueError(f"invalid role name: {s!r} (lowercase letters, digits, underscore)")
    return s


def validate_timeout(s: str) -> str:
    if not isinstance(s, str) or not TIMEOUT_RE.match(s):
        raise ValueError(f"invalid timeout: {s!r} (examples: 30s, 45000, 500ms, 2min)")
    if int(re.match(r"\d+", s).group()) == 0:
        raise ValueError("timeout must be greater than zero (0 disables the statement timeout)")
    return s


def _split_qualified(entry: str) -> tuple[str, str]:
    parts = entry.split(".")
    if len(parts) != 2:
        raise ValueError(f"invalid schema.table: {entry!r}")
    return validate_ident(parts[0]), validate_ident(parts[1])


_HIDE_LOOKALIKE = re.compile(r"hide|hidden|revoke|sensitive|restrict", re.I)


def parse_pack(data: dict) -> tuple[list[str], list[str]]:
    """(schemas_in_scope, revoke_select 'schema.table' list) from a context pack.

    Hide sections go through the same normalizer as the importer (packnorm), so the
    aliases tables_to_hide / table are read too. A pack whose hide section cannot be
    parsed is an error: a silently empty revoke list leaves sensitive tables readable."""
    from .packnorm import normalize_pack
    norm = normalize_pack(data)
    data = norm.data
    schemas: list[str] = []
    for s in ((data.get("meta") or {}).get("schemas_in_scope") or []):
        validate_ident(s)
        if s not in schemas:
            schemas.append(s)
    if norm.problems:
        raise ValueError("cannot parse the pack's hide section: " + "; ".join(norm.problems[:5]))
    for k in data:
        if k != "hide_tables" and _HIDE_LOOKALIKE.search(str(k)) and k in (data or {}):
            raise ValueError(f"unrecognised section {k!r} looks like a hide/revoke list; rename it "
                             "to hide_tables (entries: ref, recommended_action) or remove it")
    revoke: list[str] = []
    for i, h in enumerate(data.get("hide_tables") or []):
        ref = h.get("ref", "")
        m = REF_RE.match(ref) if isinstance(ref, str) else None
        if not m:
            raise ValueError(f"hide_tables[{i}]: bad ref {ref!r} (want business_db.<schema>.<table>)")
        if h.get("recommended_action") != "revoke_select":
            continue
        try:
            sch, tbl = validate_ident(m.group(1)), validate_ident(m.group(2))
        except ValueError as e:
            raise ValueError(f"hide_tables[{i}]: {e}") from None
        q = f"{sch}.{tbl}"
        if q not in revoke:
            revoke.append(q)
    return schemas, revoke


def load_pack(path: str | os.PathLike) -> tuple[list[str], list[str]]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"{p.name}: the context pack must be JSON (ic-context-pack.json)") from None
    if not isinstance(data, dict):
        raise ValueError(f"{p.name}: expected a JSON object")
    return parse_pack(data)


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def render(role: str, schemas: list[str], revoke: list[str], timeout: str, password: str,
           reset_revoke: bool = False) -> str:
    validate_role(role)
    if not schemas:
        raise ValueError("no schemas in scope")
    for s in schemas:
        validate_ident(s)
    for e in revoke:
        _split_qualified(e)
    validate_timeout(timeout)
    if password and ("\x00" in password or "\n" in password or "\r" in password):
        raise ValueError("password may not contain NUL or newline")
    lines = [
        f"SET ic_ro.role = {_lit(role)};",
        f"SET ic_ro.schemas = {_lit(','.join(schemas))};",
        f"SET ic_ro.revoke = {_lit(','.join(revoke))};",
        f"SET ic_ro.statement_timeout = {_lit(timeout)};",
    ]
    if reset_revoke:
        lines.append("SET ic_ro.reset_revoke = 'on';")
    if password:
        lines.append(f"SET ic_ro.password = {_lit(password)};")
    return "\n".join(lines) + "\n" + KIT_PATH.read_bytes().decode("utf-8")


_URL_DROP = {"user", "password", "connect_timeout"}   # credentials; connect_timeout is libpq-only (asyncpg would send it to the server)


def build_url(admin_url: str, role: str, password: str, host: str | None = None) -> str:
    """The URL saved for the read-only role. Keeps the admin URL's query (sslmode and
    friends, Quick 029) but never its credentials or connect_timeout; host/port only
    when no --host replica override is given."""
    p = urlsplit(admin_url)
    hostport = host if host else p.netloc.rsplit("@", 1)[-1]
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{hostport}"
    drop = _URL_DROP | ({"host", "port"} if host else set())
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in drop]
    query = urlencode(kept, quote_via=quote)
    return urlunsplit((p.scheme, netloc, p.path, query, ""))


# Phase 11 gap closure (WR-05): admin URL -> libpq environment, never argv.
PG_QUERY_ENV = {
    "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
    "options": "PGOPTIONS", "application_name": "PGAPPNAME", "connect_timeout": "PGCONNECT_TIMEOUT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD", "dbname": "PGDATABASE",
}
PG_ENV_KEYS = sorted({"PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", *PG_QUERY_ENV.values()})


def pg_env(admin_url: str) -> dict[str, str]:
    """Split a postgres URL into libpq env vars. Errors never carry a value."""
    try:
        p = urlsplit(admin_url)
        scheme = p.scheme.split("+", 1)[0]
        if scheme not in ("postgres", "postgresql"):
            raise ValueError("admin URL must start with postgresql://")
        out: dict[str, str] = {}
        if p.username:
            out["PGUSER"] = unquote(p.username)
        if p.password:
            out["PGPASSWORD"] = unquote(p.password)
        if p.hostname:
            out["PGHOST"] = p.hostname
        try:
            port = p.port
        except ValueError:
            raise ValueError("invalid port in the admin URL") from None
        if port is not None:
            out["PGPORT"] = str(port)
        db = unquote(p.path.lstrip("/"))
        if db:
            out["PGDATABASE"] = db
    except ValueError as e:
        if str(e).startswith(("admin URL", "invalid port")):
            raise
        raise ValueError("the admin URL is not a valid postgres URL") from None
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k not in PG_QUERY_ENV:
            raise ValueError(f"unsupported admin URL parameter: {k}")
        out[PG_QUERY_ENV[k]] = v
    out = {k: v for k, v in out.items() if v != ""}
    for k, v in out.items():
        if any(c in v for c in "\x00\r\n"):
            raise ValueError(f"{k} contains a control character")
    return out


# -- the proof ------------------------------------------------------------

class _Rollback(Exception):
    pass


async def check_role(conn) -> tuple[bool, str]:
    import asyncpg

    failures: list[str] = []
    warnings: list[str] = []
    can_write = False

    flags = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
        "WHERE rolname = current_user")
    if flags:
        if flags["rolsuper"]:
            failures.append("role is a superuser")
            can_write = True
        if flags["rolbypassrls"]:
            failures.append("role has BYPASSRLS")
        if flags["rolcreaterole"] or flags["rolcreatedb"]:
            warnings.append("role has CREATEROLE or CREATEDB")
    mem = await conn.fetchrow(
        # CASE, not AND: the predefined roles do not exist before PostgreSQL 14 and
        # pg_has_role raises for an unknown role (CR-03).
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='pg_read_all_data') "
        "            THEN pg_has_role(current_user,'pg_read_all_data','MEMBER') ELSE false END AS r, "
        "       CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='pg_write_all_data') "
        "            THEN pg_has_role(current_user,'pg_write_all_data','MEMBER') ELSE false END AS w")
    if mem:
        if mem["w"]:
            failures.append("role is a member of pg_write_all_data")
            can_write = True
        if mem["r"]:
            failures.append("role is a member of pg_read_all_data (revokes cannot hold)")

    n_write = await conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\\_%' "
        "AND c.relkind IN ('r','p','v','m','f') "
        "AND (has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE') "
        "OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE'))")
    if n_write:
        failures.append(f"{n_write} table(s) have a write privilege (INSERT/UPDATE/DELETE/TRUNCATE)")
        can_write = True

    create_schemas = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE has_schema_privilege(oid,'CREATE') "
        "AND nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'")
    if create_schemas:
        names = ", ".join(r["nspname"] for r in create_schemas)
        msg = f"CREATE allowed on schema(s): {names}"
        if any(r["nspname"] == "public" for r in create_schemas):
            msg += " (remedy for the DBA: REVOKE CREATE ON SCHEMA public FROM PUBLIC)"
        failures.append(msg)
        can_write = True

    # Physical probe, forced READ WRITE so the role's read-only default cannot
    # mask a real grant (it would otherwise "pass" for the wrong reason).
    created = False
    try:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ WRITE")
            await conn.execute("CREATE TABLE public._ic_write_probe (id int)")
            created = True
            raise _Rollback()
    except _Rollback:
        pass
    except Exception:
        pass  # good: the role cannot create
    if created:
        failures.append("probe CREATE TABLE succeeded (rolled back)")
        can_write = True

    ro = await conn.fetchval("SHOW default_transaction_read_only")
    st = await conn.fetchval("SHOW statement_timeout")
    if str(ro).lower() != "on":
        warnings.append("default_transaction_read_only is off for this role")
    if str(st) in ("0", "0ms"):
        warnings.append("statement_timeout is 0 (no limit)")

    comment = await conn.fetchval(
        "SELECT shobj_description((SELECT oid FROM pg_roles WHERE rolname = current_user), 'pg_authid')")
    revoked: list[str] = []
    if comment and "revoke_select:" in comment:
        raw = comment.split("revoke_select:", 1)[1]
        revoked = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        warnings.append("no revoke list recorded on the role (run agentctl readonly-role)")

    verified: list[str] = []
    dep_targets: list[str] = []
    for entry in revoked:
        try:
            sch, tbl = _split_qualified(entry)
        except ValueError:
            failures.append(f"revoke list entry {entry!r} is not a valid schema.table")
            continue
        q = f'"{sch}"."{tbl}"'
        if await conn.fetchval("SELECT to_regclass($1)", q) is None:
            warnings.append(f"revoked table {sch}.{tbl} does not exist")
            continue
        dep_targets.append(q)
        priv = await conn.fetchrow(
            "SELECT has_table_privilege($1,'SELECT') AS t, has_any_column_privilege($1,'SELECT') AS c", q)
        if priv["t"] or priv["c"]:
            failures.append(f"{sch}.{tbl} is still readable (check grants to PUBLIC or role memberships)")
            continue
        try:
            await conn.execute(f"SELECT 1 FROM {q} LIMIT 0")
            failures.append(
                f"{sch}.{tbl} is still readable (SELECT succeeded; check grants to PUBLIC or role memberships)")
        except asyncpg.InsufficientPrivilegeError:
            verified.append(f"{sch}.{tbl}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{sch}.{tbl}: unexpected {type(e).__name__} instead of permission denied")

    # Phase 11 gap closure (WR-02): a view runs with its owner's rights.
    dep_ok: list[str] = []
    if dep_targets:
        try:
            dep_rows = await conn.fetch(
                "WITH RECURSIVE dep(oid, root) AS ("
                " SELECT rw.ev_class, d.refobjid FROM pg_depend d JOIN pg_rewrite rw ON rw.oid = d.objid"
                " WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass"
                " AND d.refobjid IN (SELECT to_regclass(x)::oid FROM unnest($1::text[]) x)"
                " AND rw.ev_class <> d.refobjid"
                " UNION"
                " SELECT rw.ev_class, dep.root FROM dep JOIN pg_depend d ON d.refobjid = dep.oid"
                " JOIN pg_rewrite rw ON rw.oid = d.objid"
                " WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass"
                " AND rw.ev_class <> d.refobjid)"
                " SELECT DISTINCT n.nspname || '.' || c.relname AS rel,"
                " rn.nspname || '.' || rc.relname AS root,"
                " ((has_table_privilege(c.oid,'SELECT') OR has_any_column_privilege(c.oid,'SELECT'))"
                "  AND has_schema_privilege(c.relnamespace,'USAGE')) AS readable"
                " FROM dep JOIN pg_class c ON c.oid = dep.oid JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_class rc ON rc.oid = dep.root JOIN pg_namespace rn ON rn.oid = rc.relnamespace"
                " WHERE c.relkind IN ('v','m') ORDER BY 1, 2", dep_targets)
            for row in dep_rows:
                if row["readable"]:
                    failures.append(
                        f"{row['rel']} reads revoked {row['root']} and is still readable "
                        "(re-run agentctl readonly-role, which revokes views over revoked tables)")
                elif row["rel"] not in dep_ok:
                    dep_ok.append(row["rel"])
        except Exception as e:  # noqa: BLE001
            warnings.append(f"could not check views over revoked tables ({type(e).__name__})")

    if failures:
        head = "ROLE CAN WRITE" if can_write else "ROLE IS NOT SAFE"
        return False, (f"{head}: " + "; ".join(failures)
                       + ". Fix: run `agentctl readonly-role` (deploy/readonly-role.sql) on the primary and retest")

    sample_rows = await conn.fetch(
        "SELECT n.nspname || '.' || c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\\_%' "
        "AND c.relkind IN ('r','p','v','m','f') AND has_table_privilege(c.oid,'SELECT') "
        "ORDER BY 1 LIMIT 5")
    sample = [r["name"] for r in sample_rows]
    detail = (f"role is read-only (no write privilege on any table, no CREATE on any schema; "
              f"default_transaction_read_only={ro}, statement_timeout={st}); "
              f"revoked tables unreadable: {', '.join(verified) if verified else 'none recorded'}; "
              + (f"views over revoked tables unreadable: {', '.join(dep_ok)}; " if dep_ok else "")
              + f"sample tables: {sample}")
    for w in warnings:
        detail += f"; warning: {w}"
    return True, detail


# -- CLI --------------------------------------------------------------------

def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="readonlykit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--role", default="intelligence_corner")
    r.add_argument("--schemas", default="")
    r.add_argument("--pack")
    r.add_argument("--revoke", default="")
    r.add_argument("--timeout", default="30s")
    r.add_argument("--reset-revoke", action="store_true")
    u = sub.add_parser("url")
    u.add_argument("--role", default="intelligence_corner")
    u.add_argument("--host")
    sub.add_parser("pgenv")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        if a.cmd == "render":
            schemas: list[str] = []
            revoke: list[str] = []
            if a.pack:
                schemas, revoke = load_pack(a.pack)
            for s in _csv(a.schemas):
                if s not in schemas:
                    schemas.append(s)
            for e in _csv(a.revoke):
                if e not in revoke:
                    revoke.append(e)
            if not schemas:
                schemas = ["public"]
            if a.pack:
                print(f"role kit: pack {Path(a.pack).name}: will revoke SELECT on {len(revoke)} table(s)"
                      + (": " + ", ".join(revoke) if revoke else ""), file=sys.stderr)
            sys.stdout.write(render(a.role, schemas, revoke, a.timeout,
                                    os.environ.get("IC_RO_PASSWORD", ""), a.reset_revoke))
        elif a.cmd == "pgenv":
            admin = os.environ.get("ADMIN_DATABASE_URL")
            if not admin:
                raise ValueError("ADMIN_DATABASE_URL must be set in the environment")
            env = pg_env(admin)
            for k in sorted(env):
                sys.stdout.write(f"{k}={env[k]}\n")
        else:
            admin = os.environ.get("ADMIN_DATABASE_URL")
            pw = os.environ.get("IC_RO_PASSWORD", "")
            if not admin or not pw:
                raise ValueError("ADMIN_DATABASE_URL and IC_RO_PASSWORD must be set in the environment")
            validate_role(a.role)
            print(build_url(admin, a.role, pw, host=a.host))
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
