"""Phase 15 (SRC-02): the only reader and writer of source_credentials. Decrypted values go only to
syncrunner env and wh_owner user mappings; they are never returned by a route or given to the agent."""

import json
import logging
import re
import secrets as _secrets

from . import secretbox, sourcesvc, syncrunner
from .db import get_pool
from .syncrunner import source_env

log = logging.getLogger("sourcevault")

CRED_KINDS = ("postgres", "mysql", "mssql")
AAD_PREFIX = "source_credentials:"
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_BAD_HOST = re.compile(r"[/@?\s]|://")
_OPTION_RULES = {
    "postgres": {"sslmode": lambda v: v in SSL_MODES},
    "mysql": {"ssl": lambda v: isinstance(v, bool)},
    "mssql": {"encrypt": lambda v: v in ("yes", "no", "strict"),
              "trust_server_certificate": lambda v: isinstance(v, bool)},
}
IN_USE = "Credential is used by an active source; remove or pause that source first."


def _aad(name: str) -> str:
    return AAD_PREFIX + name


def validate(name, kind, fields) -> dict:
    err = sourcesvc.validate_name(name)
    if err:
        raise ValueError(err)
    if kind not in CRED_KINDS:
        raise ValueError("kind must be postgres, mysql or mssql")
    f = fields or {}
    host = f.get("host")
    if not isinstance(host, str) or not host or len(host) > 253 or _BAD_HOST.search(host):
        raise ValueError("host is invalid: use a bare host name or address")
    try:
        port = int(f.get("port"))
    except (TypeError, ValueError):
        raise ValueError("port must be a number from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("port must be a number from 1 to 65535")
    database, user = f.get("database"), f.get("user")
    if not isinstance(database, str) or not database.strip():
        raise ValueError("database is required")
    if not isinstance(user, str) or not user.strip():
        raise ValueError("user is required")
    password = f.get("password")
    if password is None:
        password = ""
    if not isinstance(password, str) or len(password) > 1024:
        raise ValueError("password is invalid or too long")
    opts = f.get("options") or {}
    if not isinstance(opts, dict):
        raise ValueError("options must be an object")
    rules = _OPTION_RULES[kind]
    for k, v in opts.items():
        if k not in rules:
            raise ValueError(f"option {k} is not allowed for {kind}")
        if not rules[k](v):
            raise ValueError(f"option {k} has an invalid value")
    return {"host": host.strip(), "port": port, "database": database.strip(),
            "user": user.strip(), "password": password, "options": opts}


def _tls_flag(d: dict):
    """Quick 028 (WR-04): does the credential ask for TLS? None when it cannot be told."""
    if d.get("secret") is None:
        return None
    try:
        from .whsources import tls_requested
        opts = json.loads(secretbox.decrypt_aad(_aad(d["name"]), d["secret"])).get("options")
        return tls_requested(d["kind"], opts)
    except Exception:  # noqa: BLE001
        return None


def public_view(row) -> dict:
    d = dict(row)
    disp = d.get("display")
    if isinstance(disp, str):
        try:
            disp = json.loads(disp)
        except ValueError:
            disp = {}
    disp = {k: (disp or {}).get(k) for k in ("host", "port", "database", "user")}
    return {"handle": "cred:" + d["name"], "name": d["name"], "kind": d["kind"], "display": disp,
            "tls": _tls_flag(d),  # Quick 028 (WR-04): a boolean only, no option values
            "read_only": d.get("read_only"), "verify_status": d.get("verify_status"),
            "verify_detail": d.get("verify_detail"), "verified_at": d.get("verified_at")}


# Phase 15 (SRC-02): owner audit rows
async def _decider(conn) -> int | None:
    row = await conn.fetchrow("SELECT id FROM people WHERE is_primary AND active LIMIT 1")
    return row["id"] if row else None


_VERBS = {"save": "saved", "verify": "re-verified", "delete": "deleted"}


async def _audit(conn, op: str, row: dict) -> None:
    handle = "cred:" + row["name"]
    payload = {"op": op, "handle": handle, "kind": row["kind"],
               "verify_status": row.get("verify_status"), "read_only": row.get("read_only"),
               "origin": "owner", "_summary": f"Owner {_VERBS[op]} credential {handle}"}
    await conn.execute(
        """INSERT INTO actions (kind, payload, tier, status, decided_by, executed_at)
           VALUES ('source_credential', $1::jsonb, 'operator', 'executed', $2, now())""",
        json.dumps(payload), await _decider(conn))
# End Phase 15 (SRC-02): owner audit rows


async def _fetch(name: str):
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM source_credentials WHERE name = $1", name)


def _load_fields(row) -> dict:
    return json.loads(secretbox.decrypt_aad(_aad(row["name"]), row["secret"]))


async def save(name: str, kind: str, fields: dict) -> dict:
    clean = validate(name, kind, fields)
    stored = secretbox.encrypt_aad(_aad(name), json.dumps(clean))
    if clean["password"]:
        secretbox.add_known([clean["password"]])
    display = {k: clean[k] for k in ("host", "port", "database", "user")}
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO source_credentials (name, kind, secret, display)
                   VALUES ($1, $2, $3, $4::jsonb)
                   ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, secret = EXCLUDED.secret,
                     display = EXCLUDED.display, read_only = NULL, verify_status = NULL,
                     verify_detail = NULL, verified_at = NULL, updated_at = now()""",
                name, kind, stored, json.dumps(display))
    log.info("credential cred:%s saved", name)
    return await verify(name, audit=True, op="save")


async def verify(name: str, *, audit: bool = True, op: str = "verify") -> dict:
    row = await _fetch(name)
    if row is None:
        raise LookupError("unknown credential")
    cred = _load_fields(row)
    secretbox.add_known([cred.get("password", "")])
    status, detail, read_only = "failed", None, row["read_only"]
    try:
        res = await syncrunner.run_interactive(
            "check", {"v": 1, "kind": row["kind"], "source": "check"}, source_env(cred),
            name=f"chk-{name}-{_secrets.token_hex(3)}")
        if res.get("status") == "ok":
            status, read_only = "ok", res.get("read_only")
        else:
            detail = res.get("error") or res.get("stderr") or "check failed"
    except syncrunner.SyncImageMissing:
        detail = syncrunner.SYNC_RUNNER_MISSING
    except Exception as e:
        detail = f"check could not run ({type(e).__name__})"
    if detail is not None:
        detail = (secretbox.redact(str(detail)) or "")[:300]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE source_credentials SET verify_status = $2, verify_detail = $3,
                     read_only = $4, verified_at = now(), updated_at = now() WHERE name = $1""",
                name, status, detail, read_only)
            if audit:
                await _audit(conn, op, dict(row, verify_status=status, read_only=read_only))
    log.info("credential cred:%s verify %s", name, status)
    return public_view(await _fetch(name))


async def load(name: str) -> dict:
    """Plumbing callers only: decrypted fields."""
    row = await _fetch(name)
    if row is None:
        raise LookupError("unknown credential")
    cred = _load_fields(row)
    secretbox.add_known([cred.get("password", "")])
    return cred


async def list_handles() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM source_credentials ORDER BY name")
    return [public_view(r) for r in rows]


async def delete(name: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM source_credentials WHERE name = $1 FOR UPDATE", name)
            if row is None:
                raise LookupError("unknown credential")
            used = await conn.fetchval(
                "SELECT count(*) FROM sources WHERE credential IN ($1, $2) AND status <> 'removed'",
                name, "cred:" + name)
            if used:
                raise ValueError(IN_USE)
            await conn.execute("DELETE FROM source_credentials WHERE name = $1", name)
            await _audit(conn, "delete", dict(row))
    log.info("credential cred:%s deleted", name)
    return {"handle": "cred:" + name, "deleted": True}
