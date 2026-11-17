"""Warehouse linker (Phase 14, WH-04 / WH-05 / WH-06).

The only creator of foreign servers, user mappings, foreign tables, mirrored
views and the ``ic_meta`` link tables, always acting as ``wh_owner``. It never
touches the context store (D-18). Phase 15 adds new sources as its own
functions without changing this provisioning order.

This file holds the first half of the linker:

* pure decisions (URL to server options, loopback rewrite, column type mapping,
  hashes, create/replace/drop planning, schema naming), unit tested without a
  database;
* owner-side plumbing: ``ic_meta`` tables and grants, the ``business_db``
  server with the single ``wh_owner`` user mapping (Option A, D-13), the private
  catalog foreign tables, the live-link ping view and the catalog snapshot that
  reproduces v1.0 discovery rows.

Nothing here logs a URL, a password or a built statement; only role names,
schema names and counts.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, unquote, urlsplit

import asyncpg

from .warehouse import DEFAULT_ROLES, Roles, exec_fmt

log = logging.getLogger("whlink")

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "127.0.1.1", "::1"}
DOCKER_HOST = "host.docker.internal"
_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
_ATTRS = {"any", "read-write", "read-only", "primary", "standby", "prefer-standby"}
_ALLOWED_QUERY = {"sslmode": _SSLMODES, "target_session_attrs": _ATTRS}


@dataclass(frozen=True)
class LinkOptions:
    host: str
    port: str
    dbname: str
    user: str
    password: str = field(repr=False)
    extra: tuple = ()
    host_rewritten: bool = False


# ── Pure functions ──────────────────────────────────────────────────────────
def _split_hostport(part: str) -> tuple[str, str]:
    part = part.strip()
    if part.startswith("["):
        end = part.find("]")
        if end < 0:
            raise ValueError("source URL has an unterminated IPv6 host")
        host, rest = part[1:end], part[end + 1:]
        port = rest[1:] if rest.startswith(":") else ""
    elif ":" in part:
        host, port = part.rsplit(":", 1)
    else:
        host, port = part, ""
    port = port or "5432"
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("source URL has an invalid port")
    return host, port


def parse_source_url(url: str) -> LinkOptions:
    """Server options from a libpq-style URL. Error messages name the problem
    only, never the URL or any part of it."""
    u = (url or "").strip()
    scheme, sep, rest = u.partition("://")
    if not sep or scheme.split("+", 1)[0] not in ("postgresql", "postgres"):
        raise ValueError("source URL must start with postgresql://")
    sp = urlsplit("postgresql://" + rest)
    netloc = sp.netloc
    userinfo, _, hostpart = netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    user, password = unquote(user), unquote(password)
    if not user:
        raise ValueError("source URL has no user")
    hosts, ports = [], []
    for part in hostpart.split(","):
        h, p = _split_hostport(unquote(part) if "%" in part else part)
        if not h:
            raise ValueError("source URL has no host")
        if h.startswith("/"):
            raise ValueError("unix-socket hosts are not supported")
        hosts.append(h)
        ports.append(p)
    dbname = unquote(sp.path.lstrip("/"))
    if not dbname:
        raise ValueError("source URL has no database name")
    extra, ignored = [], []
    for k, v in parse_qsl(sp.query, keep_blank_values=True):
        allowed = _ALLOWED_QUERY.get(k)
        if allowed is None:
            ignored.append(k)
        elif v not in allowed:
            raise ValueError(f"source URL has an unsupported {k} value")
        else:
            extra.append((k, v))
    if ignored:
        log.info("source URL query keys ignored: %s", ", ".join(sorted(set(ignored))))
    return LinkOptions(",".join(hosts), ",".join(ports), dbname, user, password,
                       tuple(sorted(extra)))


def rewrite_loopback(host: str) -> tuple[str, bool]:
    """A client database on the VM host itself is reached as
    host.docker.internal from inside the warehouse container (Pitfall 8)."""
    if host.strip().lower() in LOOPBACK_HOSTS:
        return DOCKER_HOST, True
    return host, False


def rewrite_options(opts: LinkOptions) -> LinkOptions:
    pairs = [rewrite_loopback(h) for h in opts.host.split(",")]
    if not any(r for _, r in pairs):
        return opts
    return replace(opts, host=",".join(h for h, _ in pairs), host_rewritten=True)


# ── Phase 14 gap closure (14-11, CR-01 residual): resolved column types ──
_UNLINKED_REASONS = {
    "c": "composite type is not linked",
    "r": "range type is not linked",
    "m": "multirange type is not linked",
    "p": "pseudo type is not linked",
    "b": "base type outside an extension is not linked",
}
_MAX_DOMAIN_STEPS = 16


def _unlinked(reason: str, is_array: bool = False, **kw) -> dict:
    out = {"rkind": "unlinked", "rbase": None, "enum_key": None, "enum_labels": None,
           "ext_name": None, "ext_typname": None, "ext_visible": None,
           "is_array": is_array, "reason": reason}
    out.update(kw)
    return out


def resolve_type(typid: int, types: dict) -> dict:
    """Resolve a user-defined column type through arrays and domains.

    Rule: a column is either declared with a local type whose operators give the
    source's results, or it is not linked at all. Never a text stand-in, because
    text equality, ORDER BY and GROUP BY can differ silently from the type's own
    operators. Plan 14-12 adds the allowlisted-extension branch at the marked
    place below; the extension decision stays in this one function."""
    t = types.get(typid)
    if t is None:
        return _unlinked("type not found in source catalog")
    is_array = False
    for _ in range(_MAX_DOMAIN_STEPS + 1):
        if t.get("typelem") and (t.get("typname") or "").startswith("_"):
            is_array = True
            t = types.get(t["typelem"])
            if t is None:
                return _unlinked("type not found in source catalog", is_array)
            continue
        if t.get("ext"):
            if t["ext"] in TYPE_EXTENSIONS:
                # 14-12: a real local type from the same extension keeps source semantics
                return {"rkind": "ext", "rbase": None, "enum_key": None,
                        "enum_labels": None, "ext_name": t["ext"],
                        "ext_typname": t.get("typname"),
                        "ext_visible": bool(t.get("visible")), "is_array": is_array,
                        "reason": None}
            return _unlinked("extension type has no local equivalent: " + t["ext"], is_array,
                             ext_name=t["ext"], ext_typname=t.get("typname"),
                             ext_visible=bool(t.get("visible")))
        tt = t.get("typtype")
        if tt == "e":
            return {"rkind": "enum", "rbase": None,
                    "enum_key": f"{t['nspname']}.{t['typname']}",
                    "enum_schema": t["nspname"], "enum_name": t["typname"],
                    "enum_labels": list(t.get("labels") or []), "ext_name": None,
                    "ext_typname": None, "ext_visible": None, "is_array": is_array,
                    "reason": None}
        if tt == "d":
            if t.get("base_fmt") is not None:
                base = t["base_fmt"]
                if base.endswith("[]") and is_array:
                    return _unlinked("nested array domain is not linked", is_array)
                if base.endswith("[]"):
                    is_array = True
                    base = base[:-2]
                return {"rkind": "builtin", "rbase": base, "enum_key": None,
                        "enum_labels": None, "ext_name": None, "ext_typname": None,
                        "ext_visible": None, "is_array": is_array, "reason": None}
            nxt = types.get(t.get("typbasetype"))
            if nxt is None:
                return _unlinked("type not found in source catalog", is_array)
            t = nxt
            continue
        return _unlinked(_UNLINKED_REASONS.get(tt, "base type outside an extension is not linked"),
                         is_array)
    return _unlinked("domain chain too deep", is_array)


def fdw_type(col: dict) -> str | None:
    """Type used in the foreign table for a snapshot column, or None when the
    column has no faithful local type and is not linked. Built-in types are kept;
    resolved built-in bases keep their typmod; enums are a hash/planning token
    ("enumn:<schema>.<name>", never DDL)."""
    t = col.get("type") or "text"
    if col.get("type_builtin"):
        return t
    k = col.get("rkind")
    suffix = "[]" if col.get("is_array") else ""
    if k == "builtin" and col.get("rbase"):
        return col["rbase"] + suffix
    if k == "enum" and col.get("enum_key"):
        # Phase 15 gap B: token change forces a one-time replace onto named mirrors
        return "enumn:" + col["enum_key"] + suffix
    if k == "ext" and col.get("ext_typname"):
        return TYPES_SCHEMA + "." + col["ext_typname"] + suffix
    return None


def linkable_columns(cols: list[dict]) -> tuple[list[dict], list[dict]]:
    linked: list[dict] = []
    unlinked: list[dict] = []
    for c in cols:
        if fdw_type(c) is None:
            unlinked.append({"column": c.get("name"), "type": c.get("type"),
                             "reason": c.get("reason") or "type not found in source catalog"})
        else:
            linked.append(c)
    return linked, unlinked
# ── End Phase 14 gap closure (14-11) ──


# ── Phase 14 gap closure (14-12): extension types with a local equivalent ──
TYPES_SCHEMA = "ic_types"
# Trusted in the pinned warehouse image and text I/O round-trips exactly. vector is not
# trusted; isn and seg are left out on purpose, so their columns stay unlinked.
TYPE_EXTENSIONS = ("citext", "hstore", "ltree", "cube")


def ext_path_decision(tables: list[dict]) -> bool:
    """True when ic_types must join the reader path (some linked extension column is
    visible to the source role). One reader path cannot reproduce both resolutions, so
    invisible extension columns are then turned into unlinked."""
    cols = [c for t in tables for c in t.get("columns") or [] if c.get("rkind") == "ext"]
    if not any(c.get("ext_visible") for c in cols):
        return False
    for c in cols:
        if not c.get("ext_visible"):
            c["rkind"] = "unlinked"
            c["reason"] = ("extension type hidden from the source role while another "
                           "is visible")
    return True


def session_search_path(sp: str, uses_types: bool) -> str:
    if not uses_types:
        return sp
    if TYPES_SCHEMA in _split_search_path(sp):
        return sp
    return f"{sp}, {TYPES_SCHEMA}"


async def _ensure_type_extension(conn, ext: str, typname: str) -> tuple[str, str]:
    """Install an allowlisted trusted extension into ic_types as wh_owner."""
    if ext not in TYPE_EXTENSIONS:
        raise ValueError("extension not allowed for local types")
    row = await conn.fetchrow(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = $1", ext)
    if row is not None and row["nspname"] != TYPES_SCHEMA:
        raise ValueError("extension installed outside ic_types")
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", TYPES_SCHEMA):
        raise ValueError("ic_types schema missing; bootstrap pending")
    if row is None:
        await exec_fmt(conn, "CREATE EXTENSION IF NOT EXISTS %I SCHEMA %I", ext, TYPES_SCHEMA)
    if not await conn.fetchval("SELECT to_regtype(format('%I.%I', $1::text, $2::text))",
                               TYPES_SCHEMA, typname):
        raise ValueError("local extension type missing")
    return TYPES_SCHEMA, typname
# ── End Phase 14 gap closure (14-12) ──


# ── Phase 15 (Phase 14 gap A, WH-05): typmod output names ──
BUILTIN_TYPMODOUT = frozenset({
    "varchartypmodout", "bpchartypmodout", "numerictypmodout", "timestamptypmodout",
    "timestamptztypmodout", "timetypmodout", "timetztypmodout", "intervaltypmodout",
    "bittypmodout", "varbittypmodout"})

# PostGIS 3.5 names the shared function geometry_typmod_out or geography_typmod_out
_POSTGIS_TYPMODOUT_FUNCS = frozenset({"postgis_typmod_out", "geometry_typmod_out",
                                      "geography_typmod_out"})
_POSTGIS_TYPES = (
    "Unknown", "Point", "LineString", "Polygon", "MultiPoint", "MultiLineString",
    "MultiPolygon", "GeometryCollection", "CircularString", "CompoundCurve",
    "CurvePolygon", "MultiCurve", "MultiSurface", "PolyhedralSurface", "Triangle", "Tin",
    "NurbsCurve")


def postgis_typmod_out(typmod: int) -> str:
    """Same output as PostGIS postgis_typmod_out for geometry and geography."""
    if typmod < 0:
        return ""
    t = typmod & 0xFFFFFFFF
    srid = ((t & 0x1FFFFF00) << 3) & 0xFFFFFFFF
    if srid & 0x80000000:
        srid -= 1 << 32
    srid >>= 11
    typ = (t & 0xFC) >> 2
    z = (t & 0x02) >> 1
    m = t & 0x01
    if srid == 0 and typ == 0 and not z and not m:
        return ""
    name = "Invalid type" if typ >= len(_POSTGIS_TYPES) else _POSTGIS_TYPES[typ]
    if typ == 0 and (srid or z or m):
        name = "Geometry"
    out = "(" + name + ("Z" if z else "") + ("M" if m else "")
    if srid:
        out += f",{srid}"
    return out + ")"


def compose_user_type(base: str, typmod: int, typmodout: str | None, is_array: bool,
                      builtin_out: dict) -> tuple[str, str | None]:
    """format_type-equivalent name for a user type column. Returns (name, function)
    where function is set when the typmod output function is not reproduced."""
    bad: str | None = None
    out = base
    if typmod >= 0:
        fn = (typmodout or "").strip()
        if fn in ("", "-"):
            out = f"{base}({typmod})"
        elif fn in BUILTIN_TYPMODOUT and (fn, typmod) in builtin_out:
            out = base + builtin_out[(fn, typmod)]
        elif "." in fn and fn.rsplit(".", 1)[1] in _POSTGIS_TYPMODOUT_FUNCS:
            out = base + postgis_typmod_out(typmod)
        else:
            out = f"{base}({typmod})"
            bad = fn
    return out + ("[]" if is_array else ""), bad


async def builtin_typmod_outputs(conn, pairs: set) -> dict:
    """Ask the warehouse for the exact output of allowlisted typmodout functions."""
    res: dict = {}
    for name, tm in sorted(pairs):
        if name not in BUILTIN_TYPMODOUT:
            continue
        res[(name, tm)] = await conn.fetchval(f"SELECT pg_catalog.{name}($1::int4)::text", tm)
    return res
# ── End Phase 15 gap A ──


# ── Phase 15 (Phase 14 gap B, WH-03/WH-04): source type names ──
MAX_MIRRORED_TYPES = 500


def user_types_from(types: dict, usable: set) -> tuple[list, list]:
    """Enums and domains to mirror under their own schema and name, plus the ones that
    cannot be (with the resolver's reason). Pure: ``types`` is the snapshot type map
    (oid -> facts), ``usable`` the schemas the source role may use."""
    by_name = {(t["nspname"], t["typname"]): (oid, t) for oid, t in types.items()}
    found: dict = {}
    unsupported: list = []

    def enum_entry(t):
        return {"schema": t["nspname"], "name": t["typname"], "kind": "enum",
                "labels": list(t.get("labels") or []), "base": None,
                "visible": bool(t.get("visible"))}

    for oid, t in sorted(types.items()):
        tt = t.get("typtype")
        ns, nm = t["nspname"], t["typname"]
        if tt not in ("e", "d") or t.get("ext"):
            continue
        if ns not in usable or not mirrored_schema_allowed(ns):
            continue
        if tt == "e":
            found[(ns, nm)] = enum_entry(t)
            continue
        r = resolve_type(oid, types)
        if r["rkind"] == "unlinked":
            unsupported.append({"schema": ns, "name": nm, "kind": "domain",
                                "reason": r["reason"]})
            continue
        if r["rkind"] == "builtin":
            base = {"rkind": "builtin", "rbase": r["rbase"], "is_array": r["is_array"]}
        elif r["rkind"] == "enum":
            base = {"rkind": "enum", "enum_key": r["enum_key"],
                    "enum_schema": r["enum_schema"], "enum_name": r["enum_name"],
                    "is_array": r["is_array"]}
        else:
            base = {"rkind": "ext", "ext_name": r["ext_name"],
                    "ext_typname": r["ext_typname"], "is_array": r["is_array"]}
        found[(ns, nm)] = {"schema": ns, "name": nm, "kind": "domain", "labels": None,
                           "base": base, "visible": bool(t.get("visible"))}
    # an enum that is the base of a mirrored domain is mirrored too, usable or not
    for key, e in list(found.items()):
        b = e.get("base")
        if e["kind"] != "domain" or not b or b["rkind"] != "enum":
            continue
        ek = (b["enum_schema"], b["enum_name"])
        if ek in found:
            continue
        hit = by_name.get(ek)
        if hit is not None and mirrored_schema_allowed(ek[0]):
            found[ek] = enum_entry(hit[1])
        else:
            del found[key]
            unsupported.append({"schema": key[0], "name": key[1], "kind": "domain",
                                "reason": "base enum is in a reserved schema"})
    out = [found[k] for k in sorted(found)]
    if len(out) > MAX_MIRRORED_TYPES:
        log.warning("%d user types beyond the first %d are not mirrored",
                    len(out) - MAX_MIRRORED_TYPES, MAX_MIRRORED_TYPES)
        out = out[:MAX_MIRRORED_TYPES]
    return out, unsupported


def type_def_hash(t: dict) -> str:
    core = [t.get("kind"), t.get("labels") if t.get("kind") == "enum" else t.get("base")]
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]


def mirror_plan(user_types: list, stored: dict) -> dict:
    """What the warehouse needs: create, replace (definition changed), unchanged, and the
    stored keys that are no longer in the snapshot (drop)."""
    out: dict = {"create": [], "replace": [], "unchanged": [], "drop": []}
    seen = set()
    for t in user_types:
        key = (t["schema"], t["name"])
        seen.add(key)
        if key not in stored:
            out["create"].append(t)
        elif stored[key] != type_def_hash(t):
            out["replace"].append(t)
        else:
            out["unchanged"].append(t)
    out["drop"] = sorted(k for k in stored if k not in seen)
    return out
# ── End Phase 15 gap B (pure part) ──


def col_hash(columns: list[dict]) -> str:
    """Decides foreign-table DDL only: name, mapped type and source type in
    attnum order. Comments, nullability, pk and fk do not change the DDL."""
    core = [[c.get("name"), fdw_type(c), c.get("type"), c.get("enum_labels")]
            for c in columns]
    return hashlib.sha256(
        json.dumps(core, separators=(",", ":"), default=str).encode()).hexdigest()


def catalog_row_hash(row: dict) -> str:
    """Decides ic_meta.source_catalog refreshes: every v1.0 discovery field."""
    core = {
        "relkind": row.get("relkind"),
        "table_comment": row.get("table_comment"),
        "row_estimate": row.get("row_estimate"),
        "checks": row.get("checks") or [],
        "columns": [{k: c.get(k) for k in ("name", "type", "nullable", "pk", "fk", "comment")}
                    for c in (row.get("columns") or [])],
    }
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _key(r: dict) -> tuple[str, str]:
    return (r.get("schema_name", r.get("remote_schema")),
            r.get("table_name", r.get("remote_table")))


def plan_changes(snapshot: list[dict], linked: list[dict]) -> dict:
    snap = {_key(r): r.get("col_hash") for r in snapshot}
    cur = {_key(r): r.get("col_hash") for r in linked}
    create = sorted(k for k in snap if k not in cur)
    drop = sorted(k for k in cur if k not in snap)
    replace_ = sorted(k for k in snap if k in cur and snap[k] != cur[k])
    unchanged = sum(1 for k in snap if k in cur and snap[k] == cur[k])
    return {"create": create, "replace": replace_, "drop": drop, "unchanged": unchanged}


def private_schema(source: str, remote_schema: str) -> str:
    h = hashlib.md5(remote_schema.encode()).hexdigest()[:12]
    return f"ic_fdw_{source[:35]}_{h}"


def catalog_schema(source: str) -> str:
    return f"ic_fdw_{source[:35]}_catalog"


def mirrored_schema_allowed(name: str) -> bool:
    n = (name or "").lower()
    if not n or n == "information_schema":
        return False
    return not (n.startswith("ic_") or n.startswith("pg_"))


_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_$]*$")


def _quote_ident(name: str) -> str:
    return name if _PLAIN_IDENT.match(name) else '"' + name.replace('"', '""') + '"'


def _split_search_path(raw: str) -> list[str]:
    out, buf, inq, i = [], "", False, 0
    raw = raw or ""
    while i < len(raw):
        ch = raw[i]
        if inq:
            if ch == '"':
                if raw[i + 1:i + 2] == '"':
                    buf += '"'
                    i += 1
                else:
                    inq = False
            else:
                buf += ch
        elif ch == '"':
            inq = True
        elif ch == ",":
            out.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    out.append(buf)
    return [p.strip() for p in out if p.strip()]


def search_path_from(reset_val: str, remote_user: str, mirrored: set[str]) -> str:
    """The source's default search_path, reduced to schemas that are mirrored
    into the warehouse (reserved schemas dropped)."""
    keep: list[str] = []
    for item in _split_search_path(reset_val):
        name = remote_user if item == "$user" else item
        if name in mirrored and mirrored_schema_allowed(name) and name not in keep:
            keep.append(name)
    return ", ".join(_quote_ident(n) for n in keep) or "public"


# ── Phase 14 gap closure (14-10, WR-04): type visibility path for format_type parity ──
def type_search_path(reset_val: str | None, remote_user: str, usable: set[str]) -> list[str]:
    """The active search path PostgreSQL builds for the source role. It decides
    TypeIsVisible, and therefore whether format_type qualifies a type name. It is
    not the reader session path (search_path_from)."""
    out: list[str] = []
    for item in _split_search_path(reset_val or ""):
        if item == "$user":
            if not remote_user:
                continue
            item = remote_user
        if item == "pg_temp":
            continue
        if (item == "pg_catalog" or item in usable) and item not in out:
            out.append(item)
    if "pg_catalog" not in out:
        out.insert(0, "pg_catalog")
    return out
# ── End Phase 14 gap closure (14-10) ──


# ── Owner-side plumbing (all calls act on an open wh_owner connection) ──────
_LINK_STATE_PUBLIC_COLS = ("source", "status", "tables_linked", "linked_at",
                           "source_timezone", "source_search_path")
_LINK_STATE_COLS = _LINK_STATE_PUBLIC_COLS + ("catalog_hash", "detail")

_META_DDL = (
    "CREATE TABLE IF NOT EXISTS ic_meta.source_catalog ("
    "source text NOT NULL, schema_name text NOT NULL, table_name text NOT NULL, "
    "relkind text, table_comment text, row_estimate bigint, columns jsonb, checks jsonb, "
    "col_hash text, row_hash text, PRIMARY KEY (source, schema_name, table_name))",
    "CREATE TABLE IF NOT EXISTS ic_meta.link_objects ("
    "source text NOT NULL, remote_schema text NOT NULL, remote_table text NOT NULL, "
    "private_schema text, mirrored_schema text, col_hash text, "
    "linked_at timestamptz DEFAULT now(), unlinked_columns jsonb, "
    "PRIMARY KEY (source, remote_schema, remote_table))",
    "CREATE TABLE IF NOT EXISTS ic_meta.link_state ("
    "source text PRIMARY KEY, linked_at timestamptz, catalog_hash text, "
    "tables_linked int, status text, detail text, source_timezone text, "
    "source_search_path text)",
    # Phase 15 gap B: named mirrors of source enums and domains (plain table, no server)
    "CREATE TABLE IF NOT EXISTS ic_meta.link_types ("
    "source text NOT NULL, schema_name text NOT NULL, type_name text NOT NULL, "
    "kind text NOT NULL, def_hash text NOT NULL, created_at timestamptz DEFAULT now(), "
    "PRIMARY KEY (source, schema_name, type_name))",
)


async def _meta_grants_ok(conn, reader: str) -> bool:
    q = conn.fetchval
    if not await q("SELECT has_table_privilege($1, 'ic_meta.source_catalog', 'SELECT')", reader):
        return False
    for t in ("link_objects", "link_state", "link_types"):
        if await q("SELECT has_table_privilege($1, $2, 'SELECT')", reader, f"ic_meta.{t}"):
            return False
    if await q("SELECT has_any_column_privilege($1, 'ic_meta.link_objects', 'SELECT')", reader):
        return False
    for c in _LINK_STATE_COLS:
        want = c in _LINK_STATE_PUBLIC_COLS
        got = await q("SELECT has_column_privilege($1, 'ic_meta.link_state', $2, 'SELECT')",
                      reader, c)
        if got != want:
            return False
    return True


async def ensure_meta(conn, roles: Roles = DEFAULT_ROLES) -> None:
    """Plain tables only (never foreign tables or views), so the ic_meta backup
    never references the server. Grants are written only when they differ."""
    for ddl in _META_DDL:
        await conn.execute(ddl)
    # Phase 14 gap closure (14-11): recorded refusals; ALTER only when the column is missing
    if not await conn.fetchval(
            "SELECT 1 FROM pg_attribute WHERE attrelid = 'ic_meta.link_objects'::regclass "
            "AND attname = 'unlinked_columns' AND NOT attisdropped"):
        await conn.execute("ALTER TABLE ic_meta.link_objects ADD COLUMN unlinked_columns jsonb")
    # Phase 15 gap 2 (WR-14): view names. A live table's view carries the warehouse name
    if not await conn.fetchval(
            "SELECT 1 FROM pg_attribute WHERE attrelid = 'ic_meta.link_objects'::regclass "
            "AND attname = 'view_name' AND NOT attisdropped"):
        await conn.execute("ALTER TABLE ic_meta.link_objects ADD COLUMN view_name text")
    # WR-03: a restore of the ic_meta dump recreates the schema without its grants
    for sch in ("ic_meta", "ic_probe"):
        if not await conn.fetchval(
                "SELECT has_schema_privilege($1, $2, 'USAGE')", roles.reader, sch):
            await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", sch, roles.reader)
    if await _meta_grants_ok(conn, roles.reader):
        return
    async with conn.transaction():
        await exec_fmt(conn, "GRANT SELECT ON ic_meta.source_catalog TO %I", roles.reader)
        await exec_fmt(conn, "REVOKE ALL ON ic_meta.link_objects FROM %I", roles.reader)
        await exec_fmt(conn, "REVOKE ALL ON ic_meta.link_state FROM %I", roles.reader)
        await exec_fmt(conn, "REVOKE ALL ON ic_meta.link_types FROM %I", roles.reader)
        await exec_fmt(
            conn,
            "GRANT SELECT (source, status, tables_linked, linked_at, source_timezone, "
            "source_search_path) ON ic_meta.link_state TO %I", roles.reader)
    log.info("ic_meta grants applied for %s", roles.reader)


def _opt_dict(arr) -> dict:
    out = {}
    for item in arr or []:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def _alter_template(prefix: str, cur: dict, want: dict, drop_extra: bool) -> tuple[str, list]:
    """OPTIONS (ADD/SET/DROP ...) clause for the differences, or ("", [])."""
    parts, vals = [], []
    for k, v in want.items():
        if k not in cur:
            parts.append("ADD %I %L")
            vals += [k, v]
        elif cur[k] != v:
            parts.append("SET %I %L")
            vals += [k, v]
    if drop_extra:
        for k in cur:
            if k not in want:
                parts.append("DROP %I")
                vals.append(k)
    if not parts:
        return "", []
    return prefix + " OPTIONS (" + ", ".join(parts) + ")", vals


async def ensure_server(conn, source: str, opts: LinkOptions) -> None:
    """One postgres_fdw server and exactly one user mapping, FOR the owner.
    An unchanged link issues no ALTER (each one invalidates cached FDW
    connections in every warehouse backend)."""
    opts = rewrite_options(opts)
    want = {"host": opts.host, "port": opts.port, "dbname": opts.dbname,
            "updatable": "false", "use_remote_estimate": "true",
            "fetch_size": "1000", "connect_timeout": "10"}
    want.update(dict(opts.extra))
    row = await conn.fetchrow(
        "SELECT srvoptions FROM pg_foreign_server WHERE srvname = $1", source)
    if row is None:
        tpl = ("CREATE SERVER %I FOREIGN DATA WRAPPER postgres_fdw OPTIONS ("
               + ", ".join("%I %L" for _ in want) + ")")
        vals = [source]
        for k, v in want.items():
            vals += [k, v]
        await exec_fmt(conn, tpl, *vals)
    else:
        tpl, vals = _alter_template("ALTER SERVER %I", _opt_dict(row["srvoptions"]), want, True)
        if tpl:
            await exec_fmt(conn, tpl, source, *vals)

    mwant = {"user": opts.user, "password": opts.password}
    mrow = await conn.fetchrow(
        "SELECT umoptions FROM pg_user_mappings WHERE srvname = $1 AND usename = current_user",
        source)
    if mrow is None:
        await exec_fmt(
            conn, "CREATE USER MAPPING FOR CURRENT_USER SERVER %I OPTIONS (user %L, password %L)",
            source, opts.user, opts.password)
    else:
        tpl, vals = _alter_template("ALTER USER MAPPING FOR CURRENT_USER SERVER %I",
                                    _opt_dict(mrow["umoptions"]), mwant, False)
        if tpl:
            await exec_fmt(conn, tpl, source, *vals)
    # Option A (D-13): no mapping for anyone else
    for r in await conn.fetch(
            "SELECT usename FROM pg_user_mappings WHERE srvname = $1 AND usename <> current_user",
            source):
        if r["usename"] == "public":
            await exec_fmt(conn, "DROP USER MAPPING FOR PUBLIC SERVER %I", source)
        else:
            await exec_fmt(conn, "DROP USER MAPPING FOR %I SERVER %I", r["usename"], source)
        log.warning("dropped an unexpected user mapping on %s", source)


_CATALOG_TABLES = {
    "r_class": ("pg_catalog", "pg_class",
                'oid oid, relname name, relnamespace oid, relkind "char", '
                "reltuples real, relispartition boolean"),
    "r_namespace": ("pg_catalog", "pg_namespace", "oid oid, nspname name"),
    "r_attribute": ("pg_catalog", "pg_attribute",
                    "attrelid oid, attname name, atttypid oid, atttypmod integer, "
                    "attnum smallint, attnotnull boolean, attisdropped boolean"),
    "r_constraint": ("pg_catalog", "pg_constraint",
                     'oid oid, conrelid oid, contype "char", conkey smallint[], '
                     "confrelid oid, confkey smallint[]"),
    "r_description": ("pg_catalog", "pg_description",
                      "objoid oid, classoid oid, objsubid integer, description text"),
    "r_type": ("pg_catalog", "pg_type",
               'oid oid, typname name, typnamespace oid, typelem oid, typtype "char", '
               "typbasetype oid, typtypmod integer"),
    "r_table_privileges": ("information_schema", "table_privileges",
                           "table_schema text, table_name text, privilege_type text"),
    "r_settings": ("pg_catalog", "pg_settings", "name text, reset_val text"),
    # CR-01: enum labels, so enum columns get a local mirror type
    "r_enum": ("pg_catalog", "pg_enum",
               "enumtypid oid, enumsortorder real, enumlabel name"),
    # Phase 14 gap closure (14-10, WR-04): schemas the source role may use (owner, CREATE or
    # USAGE); TypeIsVisible skips schemas without USAGE
    "r_schemata": ("information_schema", "schemata", "schema_name text"),
    # Phase 14 gap closure (14-11): extension membership of types
    "r_depend": ("pg_catalog", "pg_depend",
                 'classid oid, objid oid, refclassid oid, refobjid oid, deptype "char"'),
    "r_extension": ("pg_catalog", "pg_extension", "oid oid, extname name, extnamespace oid"),
    # Phase 15 (gap A): typmod output function per type; the regproc arrives as text
    "r_typmod": ("pg_catalog", "pg_type", "oid oid, typmodout text"),
}


async def ensure_catalog_tables(conn, source: str, roles: Roles = DEFAULT_ROLES) -> None:
    """Private catalog foreign tables (no grants to anyone) and the live-link
    ping view. Creates only what is missing, so a provisioned warehouse sees
    no DDL."""
    cs = catalog_schema(source)
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", cs):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", cs)
    for name, (rs, rt, cols) in _CATALOG_TABLES.items():
        if await conn.fetchval("SELECT to_regclass(format('%I.%I', $1::text, $2::text))",
                               cs, name):
            continue
        await exec_fmt(
            conn,
            f"CREATE FOREIGN TABLE %I.%I ({cols}) SERVER %I OPTIONS (schema_name %L, table_name %L)",
            cs, name, source, rs, rt)
    ping = f"{source}_ping"
    if not await conn.fetchval("SELECT to_regclass(format('ic_probe.%I', $1::text))", ping):
        await exec_fmt(conn, "CREATE VIEW ic_probe.%I AS SELECT count(*) AS n FROM %I.r_namespace",
                       ping, cs)
    if not await conn.fetchval(
            "SELECT has_table_privilege($1, format('ic_probe.%I', $2::text), 'SELECT')",
            roles.reader, ping):
        await exec_fmt(conn, "GRANT SELECT ON ic_probe.%I TO %I", ping, roles.reader)


_SNAP_SQL = r"""
SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind::text AS relkind,
       (SELECT d.description FROM _ic_s_d d WHERE d.objoid = c.oid AND d.objsubid = 0) AS table_comment,
       c.reltuples::bigint AS row_estimate,
       (SELECT json_agg(json_build_object(
          'name', a.attname,
          'type', CASE WHEN a.atttypid < 16384 THEN format_type(a.atttypid, a.atttypmod)
                       ELSE (SELECT CASE WHEN ty.typelem <> 0 AND ty.typname LIKE '\_%' THEN
                                      (SELECT tv.qname FROM _ic_s_tvis tv WHERE tv.oid = ty.typelem)
                                    ELSE tvy.qname END
                                 -- Phase 14 gap closure (14-10, WR-04): format_type-equivalent name
                                 -- Phase 15 (gap A): typmod and [] are composed in Python
                             FROM _ic_s_t ty JOIN _ic_s_tvis tvy ON tvy.oid = ty.oid
                             WHERE ty.oid = a.atttypid) END,
          'typmod', a.atttypmod,
          'type_array', (SELECT ty.typelem <> 0 AND ty.typname LIKE '\_%'
                         FROM _ic_s_t ty WHERE ty.oid = a.atttypid),
          'typmodout', (SELECT tm.typmodout FROM _ic_s_tm tm
                        WHERE tm.oid = (SELECT CASE WHEN ty.typelem <> 0 AND ty.typname LIKE '\_%'
                                                    THEN ty.typelem ELSE ty.oid END
                                        FROM _ic_s_t ty WHERE ty.oid = a.atttypid)),
          'type_builtin', a.atttypid < 16384,
          'typid', a.atttypid::bigint,
          'nullable', NOT a.attnotnull,
          'pk', EXISTS (SELECT 1 FROM _ic_s_k p WHERE p.conrelid = c.oid AND p.contype = 'p'
                        AND a.attnum = ANY (p.conkey)),
          'fk', (SELECT json_build_object('table', rt.relname, 'column', ra.attname)
                 FROM _ic_s_k f JOIN _ic_s_c rt ON rt.oid = f.confrelid
                 JOIN _ic_s_a ra ON ra.attrelid = f.confrelid
                      AND ra.attnum = f.confkey[array_position(f.conkey, a.attnum)]
                 WHERE f.conrelid = c.oid AND f.contype = 'f' AND a.attnum = ANY (f.conkey) LIMIT 1),
          'comment', (SELECT d.description FROM _ic_s_d d WHERE d.objoid = c.oid AND d.objsubid = a.attnum))
          ORDER BY a.attnum)
        FROM _ic_s_a a WHERE a.attrelid = c.oid) AS columns
FROM _ic_s_c c JOIN _ic_s_n n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname NOT LIKE 'ic\_%'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
  AND (c.relkind = 'm' OR EXISTS (SELECT 1 FROM _ic_s_p p
                                  WHERE p.table_schema = n.nspname AND p.table_name = c.relname))
ORDER BY n.nspname, c.relname
"""


# Phase 14 gap closure (14-11): user types with the facts resolve_type needs
_TYPES_SQL = """
SELECT t.oid::bigint AS oid, t.typname::text AS typname, n.nspname::text AS nspname,
       t.typtype::text AS typtype, t.typelem::bigint AS typelem,
       t.typbasetype::bigint AS typbasetype,
       CASE WHEN t.typtype = 'd' AND t.typbasetype < 16384
            THEN format_type(t.typbasetype, t.typtypmod) END AS base_fmt,
       (SELECT json_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM _ic_s_e e WHERE e.enumtypid = t.oid) AS labels,
       (SELECT x.extname::text FROM _ic_s_x x WHERE x.objid = t.oid LIMIT 1) AS extname,
       v.visible
FROM _ic_s_t t JOIN _ic_s_n n ON n.oid = t.typnamespace
JOIN _ic_s_tvis v ON v.oid = t.oid
"""


def _jl(v):
    return json.loads(v) if isinstance(v, str) else v


async def fetch_snapshot(conn, source: str, opts: LinkOptions | None = None) -> dict:
    """Rows in the v1.0 CATALOG_SQL shape for every relation the source role can
    SELECT, plus matviews marked for the apply-time probe, plus the source
    TimeZone and raw search_path. Each remote catalog is read once."""
    cs = catalog_schema(source)
    qc = '"' + cs.replace('"', '""') + '"'
    async with conn.transaction():
        # names come from catalog_schema(); fixed filters push down to the source
        mats = {
            "_ic_s_c": f"SELECT oid, relname, relnamespace, relkind, reltuples, relispartition "
                       f"FROM {qc}.r_class WHERE oid >= 16384 AND relkind IN ('r','p','v','m','f')",
            "_ic_s_a": f"SELECT attrelid, attname, atttypid, atttypmod, attnum, attnotnull "
                       f"FROM {qc}.r_attribute WHERE attrelid >= 16384 AND attnum > 0 "
                       f"AND NOT attisdropped",
            "_ic_s_k": f"SELECT oid, conrelid, contype, conkey, confrelid, confkey "
                       f"FROM {qc}.r_constraint WHERE conrelid >= 16384 AND contype IN ('p','f')",
            "_ic_s_d": f"SELECT objoid, objsubid, description FROM {qc}.r_description "
                       f"WHERE classoid = 1259 AND objoid >= 16384",
            "_ic_s_n": f"SELECT oid, nspname FROM {qc}.r_namespace",
            "_ic_s_t": f"SELECT oid, typname, typnamespace, typelem, typtype, typbasetype, typtypmod "
                       f"FROM {qc}.r_type WHERE oid >= 16384",
            "_ic_s_p": f"SELECT DISTINCT table_schema, table_name FROM {qc}.r_table_privileges "
                       f"WHERE privilege_type = 'SELECT'",
            "_ic_s_e": f"SELECT enumtypid, enumsortorder, enumlabel FROM {qc}.r_enum "
                       f"WHERE enumtypid >= 16384",
            "_ic_s_s": f"SELECT name, reset_val FROM {qc}.r_settings "
                       f"WHERE name IN ('TimeZone', 'search_path')",
        }
        # Phase 14 gap closure (14-10, WR-04): inputs for the type visibility computation
        mats["_ic_s_u"] = f"SELECT schema_name FROM {qc}.r_schemata"
        mats["_ic_s_tv"] = f"SELECT typname, typnamespace FROM {qc}.r_type"
        # Phase 14 gap closure (14-11): extension member types (fixed filters push down)
        mats["_ic_s_x0"] = (f"SELECT objid, refobjid FROM {qc}.r_depend WHERE classid = 1247 "
                            f"AND refclassid = 3079 AND deptype = 'e' AND objid >= 16384")
        mats["_ic_s_x1"] = f"SELECT oid, extname FROM {qc}.r_extension"
        # Phase 15 (gap A): no predicate on typmodout (regproc vs text is not shippable)
        mats["_ic_s_tm"] = f"SELECT oid, typmodout FROM {qc}.r_typmod WHERE oid >= 16384"
        for t, sql in mats.items():
            await conn.execute(f"CREATE TEMP TABLE {t} ON COMMIT DROP AS {sql}")
        sett = {r["name"]: r["reset_val"] for r in await conn.fetch("SELECT * FROM _ic_s_s")}
        usable = {r["schema_name"] for r in await conn.fetch("SELECT * FROM _ic_s_u")}
        path = type_search_path(sett.get("search_path"), opts.user if opts else "", usable)
        await conn.execute("CREATE TEMP TABLE _ic_s_path (nspname text, pos int) ON COMMIT DROP")
        await conn.execute(
            "INSERT INTO _ic_s_path SELECT n, p::int FROM unnest($1::text[]) WITH ORDINALITY AS u(n, p)",
            path)
        await conn.execute(
            "CREATE TEMP TABLE _ic_s_tvis ON COMMIT DROP AS "
            "SELECT t.oid, "
            "  (EXISTS (SELECT 1 FROM _ic_s_n n JOIN _ic_s_path p ON p.nspname = n.nspname "
            "           WHERE n.oid = t.typnamespace) "
            "   AND NOT EXISTS (SELECT 1 FROM _ic_s_tv o JOIN _ic_s_n on2 ON on2.oid = o.typnamespace "
            "                   JOIN _ic_s_path op ON op.nspname = on2.nspname "
            "                   WHERE o.typname = t.typname "
            "                     AND op.pos < (SELECT p.pos FROM _ic_s_n n JOIN _ic_s_path p "
            "                                   ON p.nspname = n.nspname WHERE n.oid = t.typnamespace))) AS visible, "
            "  CASE WHEN EXISTS (SELECT 1 FROM _ic_s_n n JOIN _ic_s_path p ON p.nspname = n.nspname "
            "                    WHERE n.oid = t.typnamespace) "
            "            AND NOT EXISTS (SELECT 1 FROM _ic_s_tv o JOIN _ic_s_n on2 ON on2.oid = o.typnamespace "
            "                   JOIN _ic_s_path op ON op.nspname = on2.nspname "
            "                   WHERE o.typname = t.typname "
            "                     AND op.pos < (SELECT p.pos FROM _ic_s_n n JOIN _ic_s_path p "
            "                                   ON p.nspname = n.nspname WHERE n.oid = t.typnamespace)) "
            "       THEN quote_ident(t.typname) "
            "       ELSE quote_ident((SELECT nspname FROM _ic_s_n WHERE oid = t.typnamespace)) "
            "            || '.' || quote_ident(t.typname) END AS qname "
            "FROM _ic_s_t t")
        await conn.execute(
            "CREATE TEMP TABLE _ic_s_x ON COMMIT DROP AS SELECT d.objid, e.extname "
            "FROM _ic_s_x0 d JOIN _ic_s_x1 e ON e.oid = d.refobjid")
        rows = await conn.fetch(_SNAP_SQL)
        trows = await conn.fetch(_TYPES_SQL)
        # Phase 15 (gap A): exact output of allowlisted built-in typmod functions
        want = {(c["typmodout"], int(c["typmod"]))
                for r in rows for c in (_jl(r["columns"]) or [])
                if not c.get("type_builtin") and c.get("typmodout") in BUILTIN_TYPMODOUT
                and c.get("typmod") is not None and int(c["typmod"]) >= 0}
        builtin_out = await builtin_typmod_outputs(conn, want)
    types = {int(r["oid"]): {
        "typname": r["typname"], "nspname": r["nspname"], "typtype": r["typtype"],
        "typelem": int(r["typelem"]), "typbasetype": int(r["typbasetype"]),
        "base_fmt": r["base_fmt"], "labels": _jl(r["labels"]), "ext": r["extname"],
        "visible": bool(r["visible"])} for r in trows}
    # Phase 15 (gap B): enums and domains that can be mirrored under their own names
    user_types, unsupported_types = user_types_from(types, usable)
    tables = []
    unsupported_typmod: list[dict] = []
    for r in rows:
        cols = _jl(r["columns"]) or []
        for c in cols:
            if not c.get("type_builtin"):
                # Phase 15 (gap A): format_type-equivalent name including typmod output
                tm = c.get("typmod")
                c["type"], bad = compose_user_type(
                    c.get("type") or "", int(tm if tm is not None else -1),
                    c.get("typmodout"), bool(c.get("type_array")), builtin_out)
                if bad:
                    unsupported_typmod.append({"schema": r["schema_name"],
                                               "table": r["table_name"],
                                               "column": c.get("name"), "function": bad})
                c.update(resolve_type(int(c["typid"]), types))
            for k in ("typmod", "type_array", "typmodout"):
                c.pop(k, None)
        row = {"schema_name": r["schema_name"], "table_name": r["table_name"],
               "relkind": r["relkind"], "table_comment": r["table_comment"],
               "row_estimate": r["row_estimate"], "columns": cols, "checks": [],
               "probe": r["relkind"] == "m"}
        tables.append(row)
    # Phase 14 gap closure (14-12): one reader path per source, decided before hashing
    uses_types_path = ext_path_decision(tables)
    for row in tables:
        row["col_hash"] = col_hash(row["columns"])
        row["row_hash"] = catalog_row_hash(row)
    tz = sett.get("TimeZone")
    log.info("catalog snapshot for %s: %d relations", source, len(tables))
    for fn in sorted({u["function"] for u in unsupported_typmod}):
        log.warning("typmod output function %s is not reproduced on %s; affected columns "
                    "keep the name(typmod) form", fn, source)
    return {"tables": tables, "timezone": tz, "search_path_raw": sett.get("search_path"),
            "type_path": path, "uses_types_path": uses_types_path,
            "unsupported_typmod": unsupported_typmod,
            "user_types": user_types, "unsupported_types": unsupported_types}


def v1_row(row: dict) -> dict:
    """The row with columns reduced to the v1.0 discovery keys."""
    keys = ("name", "type", "nullable", "pk", "fk", "comment")
    out = dict(row)
    out["columns"] = [{k: c.get(k) for k in keys} for c in row.get("columns") or []]
    return out


# ── Phase 14-05: apply_changes, refresh_catalog, link_source ────────────────
_SAFE_TYPE = re.compile(r'^[A-Za-z_"][A-Za-z0-9_ ,()\[\]"]*$')
_DENIED = ("InsufficientPrivilegeError",)


def _safe_type(col: dict) -> str:
    t = fdw_type(col)
    if t is None or not _SAFE_TYPE.match(t):
        raise ValueError("column type has no safe local declaration")
    return t


def _tables_of(snapshot) -> list[dict]:
    return list(snapshot.get("tables", [])) if isinstance(snapshot, dict) else list(snapshot)


async def _drop_objects(conn, mirrored: str, private: str, table: str, *,
                        view: str | None = None) -> None:
    """View first, then the foreign table. Never a cascading drop: a dependent object owned
    by someone else (for example a Phase 16 model view) blocks the drop."""
    # Phase 15 gap 2 (WR-14): the view may carry a warehouse name different from the remote one
    await exec_fmt(conn, "DROP VIEW IF EXISTS %I.%I", mirrored, view or table)
    await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", private, table)


def types_schema(source: str) -> str:
    return f"ic_fdw_{source[:35]}_types"


async def _ensure_enum(conn, source: str, typname: str, labels: list[str],
                       roles: Roles) -> tuple[str, str]:
    """Local mirror of a source enum, named by a hash of its name and labels so a
    new label yields a new type (and a replaced foreign table)."""
    ts = types_schema(source)
    name = "e_" + hashlib.md5(json.dumps([typname, labels]).encode()).hexdigest()[:20]
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", ts):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", ts)
    await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", ts, roles.reader)
    if not await conn.fetchval("SELECT to_regtype(format('%I.%I', $1::text, $2::text))",
                               ts, name):
        lits = ", ".join("%L" for _ in labels)
        await exec_fmt(conn, f"CREATE TYPE %I.%I AS ENUM ({lits})", ts, name, *labels)
    return ts, name


# ── Phase 15 (Phase 14 gap B, WH-03/WH-04): named type mirrors in the warehouse ──
def _tkey(t: dict) -> tuple:
    return (t["schema"], t["name"])


async def _create_type(conn, t: dict, roles: Roles) -> None:
    """One enum or domain mirror under the source schema and name (owner: wh_owner). All
    identifiers go through %I and labels through %L; the domain base text is validated."""
    schema, name = t["schema"], t["name"]
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", schema)
    if not await conn.fetchval("SELECT has_schema_privilege($1, $2, 'USAGE')",
                               roles.reader, schema):
        await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", schema, roles.reader)
    if t["kind"] == "enum":
        labels = list(t.get("labels") or [])
        lits = ", ".join("%L" for _ in labels)
        await exec_fmt(conn, f"CREATE TYPE %I.%I AS ENUM ({lits})", schema, name, *labels)
        return
    b = t["base"]
    sfx = "[]" if b.get("is_array") else ""
    if b["rkind"] == "builtin":
        if not _SAFE_TYPE.match(b["rbase"] or ""):
            raise ValueError("domain base has no safe local declaration")
        await exec_fmt(conn, f"CREATE DOMAIN %I.%I AS {b['rbase']}{sfx}", schema, name)
    elif b["rkind"] == "enum":
        await exec_fmt(conn, "CREATE DOMAIN %I.%I AS %I.%I" + sfx,
                       schema, name, b["enum_schema"], b["enum_name"])
    elif b["rkind"] == "ext":
        ts, tn = await _ensure_type_extension(conn, b["ext_name"], b["ext_typname"])
        await exec_fmt(conn, "CREATE DOMAIN %I.%I AS %I.%I" + sfx, schema, name, ts, tn)
    else:
        raise ValueError("domain base is not mirrored")


async def _drop_type(conn, schema: str, name: str, kind: str) -> None:
    """Never a cascading drop: a dependent object owned by someone else blocks it."""
    if kind == "domain":
        await exec_fmt(conn, "DROP DOMAIN %I.%I", schema, name)
    else:
        await exec_fmt(conn, "DROP TYPE %I.%I", schema, name)


async def _upsert_link_type(conn, source: str, t: dict) -> None:
    await conn.execute(
        "INSERT INTO ic_meta.link_types (source, schema_name, type_name, kind, def_hash) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (source, schema_name, type_name) DO UPDATE SET "
        "kind = EXCLUDED.kind, def_hash = EXCLUDED.def_hash",
        source, t["schema"], t["name"], t["kind"], type_def_hash(t))


async def _replace_type_group(conn, source: str, new_def: dict, roles: Roles = DEFAULT_ROLES,
                              snapshot_types: dict | None = None) -> bool:
    """Replace one mirrored type and everything of ours that depends on it, in one
    transaction: tracked domains over it, foreign tables using it and their mirrored views
    (the apply step recreates those tables because their hash changed). False, with nothing
    changed, when a dependent object owned by someone else or a lock blocks it."""
    snapshot_types = snapshot_types or {}
    schema, name = new_def["schema"], new_def["name"]
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            old = await conn.fetchrow(
                "SELECT t.oid, t.typarray FROM pg_type t WHERE t.typnamespace = "
                "(SELECT oid FROM pg_namespace WHERE nspname = $1) AND t.typname = $2",
                schema, name)
            oids = [old["oid"], old["typarray"]] if old is not None else []
            tracked = {(r["schema_name"], r["type_name"]): r["kind"] for r in await conn.fetch(
                "SELECT schema_name, type_name, kind FROM ic_meta.link_types WHERE source = $1",
                source)}
            doms = [(r["nspname"], r["typname"]) for r in await conn.fetch(
                "SELECT n.nspname, t.typname FROM pg_type t JOIN pg_namespace n "
                "ON n.oid = t.typnamespace WHERE t.typtype = 'd' "
                "AND t.typbasetype = ANY($1::oid[])", oids)] if oids else []
            again: list = []
            for dk in doms:
                if tracked.get(dk) != "domain":
                    continue  # not ours: the type drop below is then blocked
                await _drop_type(conn, dk[0], dk[1], "domain")
                if dk in snapshot_types:
                    again.append(snapshot_types[dk])
                else:
                    await conn.execute(
                        "DELETE FROM ic_meta.link_types WHERE source = $1 AND schema_name = $2 "
                        "AND type_name = $3", source, dk[0], dk[1])
            fts = await conn.fetch(
                "SELECT DISTINCT n.nspname, c.relname FROM pg_depend d "
                "JOIN pg_class c ON c.oid = d.objid AND d.classid = 'pg_class'::regclass "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE d.refclassid = 'pg_type'::regclass AND d.refobjid = ANY($1::oid[]) "
                "AND c.relkind = 'f'", oids) if oids else []
            for ft in fts:
                lo = await conn.fetchrow(
                    "SELECT mirrored_schema, view_name FROM ic_meta.link_objects WHERE source = $1 "
                    "AND private_schema = $2 AND remote_table = $3",
                    source, ft["nspname"], ft["relname"])
                if lo is not None:
                    await _drop_objects(conn, lo["mirrored_schema"], ft["nspname"],
                                        ft["relname"], view=lo["view_name"])
                else:
                    await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I",
                                   ft["nspname"], ft["relname"])
            if old is not None:
                await _drop_type(conn, schema, name, tracked.get((schema, name), "enum"))
            await _create_type(conn, new_def, roles)
            await _upsert_link_type(conn, source, new_def)
            for d in again:
                await _create_type(conn, d, roles)
                await _upsert_link_type(conn, source, d)
        return True
    except (asyncpg.PostgresError, ValueError) as e:
        log.warning("type mirror %s.%s not replaced: %s", schema, name, type(e).__name__)
        return False


async def ensure_type_mirrors(conn, source: str, user_types: list,
                              roles: Roles = DEFAULT_ROLES,
                              unsupported: list | None = None) -> dict:
    """Create or replace the named mirrors the snapshot asks for. An unchanged source issues
    no DDL. Returns {"created", "replaced", "dropped", "blocked", "blocked_keys",
    "unsupported"}; blocked lists the types whose replace a dependent object refused."""
    res: dict = {"created": 0, "replaced": 0, "dropped": 0, "blocked": [],
                 "blocked_keys": set(), "unsupported": list(unsupported or [])}
    rows = await conn.fetch(
        "SELECT schema_name, type_name, def_hash FROM ic_meta.link_types WHERE source = $1",
        source)
    stored = {(r["schema_name"], r["type_name"]): r["def_hash"] for r in rows}
    plan = mirror_plan(user_types, stored)
    by_key = {_tkey(t): t for t in user_types}
    done: set = set()

    async def create(t):
        k = _tkey(t)
        if await conn.fetchval("SELECT to_regtype(format('%I.%I', $1::text, $2::text))::text",
                               *k):
            res["unsupported"].append({"schema": k[0], "name": k[1], "kind": t["kind"],
                                       "reason": "the name is already used in the warehouse"})
            return
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                await _create_type(conn, t, roles)
                await _upsert_link_type(conn, source, t)
            res["created"] += 1
        except (asyncpg.PostgresError, ValueError) as e:
            log.warning("type mirror %s.%s not created: %s", k[0], k[1], type(e).__name__)
            res["unsupported"].append({"schema": k[0], "name": k[1], "kind": t["kind"],
                                       "reason": "mirror not created: " + type(e).__name__})

    async def replace_(t):
        k = _tkey(t)
        if k in done:
            return
        if await _replace_type_group(conn, source, t, roles, by_key):
            res["replaced"] += 1
            done.add(k)
        else:
            res["blocked"].append(f"{k[0]}.{k[1]}")
            res["blocked_keys"].add(k)

    # enums first (a domain can be over one), then domains
    for t in plan["replace"]:
        if t["kind"] == "enum":
            await replace_(t)
    for t in plan["create"]:
        if t["kind"] == "enum":
            await create(t)
    for t in plan["create"]:
        if t["kind"] == "domain":
            await create(t)
    for t in plan["replace"]:
        if t["kind"] == "domain":
            await replace_(t)
    return res


async def drop_vanished_type_mirrors(conn, source: str, user_types: list) -> int:
    """Drop tracked mirrors that left the source, once nothing depends on them (run after the
    tables are applied). A blocked drop keeps the mirror and the tracking row."""
    rows = await conn.fetch(
        "SELECT schema_name, type_name, kind FROM ic_meta.link_types WHERE source = $1", source)
    keep = {_tkey(t) for t in user_types}
    n = 0
    for r in sorted(rows, key=lambda r: r["kind"] != "domain"):
        if (r["schema_name"], r["type_name"]) in keep:
            continue
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                await _drop_type(conn, r["schema_name"], r["type_name"], r["kind"])
                await conn.execute(
                    "DELETE FROM ic_meta.link_types WHERE source = $1 AND schema_name = $2 "
                    "AND type_name = $3", source, r["schema_name"], r["type_name"])
            n += 1
        except asyncpg.PostgresError as e:
            log.info("type mirror %s.%s kept: %s", r["schema_name"], r["type_name"],
                     type(e).__name__)
    return n


async def drop_orphan_hash_enums(conn, source: str) -> int:
    """Best effort: remove e_<md5> enum mirrors nothing uses any more (the Phase 14 form)."""
    ts = types_schema(source)
    rows = await conn.fetch(
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = $1 AND t.typtype = 'e' AND t.typname ~ '^e_[0-9a-f]{20}$'", ts)
    n = 0
    for r in rows:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '2s'")
                await exec_fmt(conn, "DROP TYPE %I.%I", ts, r["typname"])
            n += 1
        except asyncpg.PostgresError:
            pass  # still in use
    return n
# ── End Phase 15 gap B ──


async def _create_foreign_table(conn, source: str, t: dict, roles: Roles = DEFAULT_ROLES,
                                *, mirror_names: bool = True,
                                blocked_types: frozenset = frozenset()
                                ) -> tuple[str, list, list]:
    schema, table = t["schema_name"], t["table_name"]
    private = private_schema(source, schema)
    cols, unlinked = linkable_columns(t.get("columns") or [])
    if not cols:
        raise ValueError("relation has no linkable columns")
    if unlinked:
        log.warning("columns not linked on %s.%s: %s", schema, table,
                    ", ".join(u["column"] for u in unlinked))
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", private):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", private)
    parts: list[str] = []
    vals: list[str] = [private, table]
    for c in cols:
        if c.get("rkind") == "ext":
            # Phase 14 gap closure (14-12): real local type from the same extension
            ts, tn = await _ensure_type_extension(conn, c["ext_name"], c["ext_typname"])
            parts.append("%I %I.%I" + ("[]" if c.get("is_array") else "")
                         + " OPTIONS (column_name %L)")
            vals += [c["name"], ts, tn, c["name"]]
        elif c.get("rkind") == "enum":
            # CR-01: a local mirror enum keeps postgres_fdw from shipping text operators
            ek = (c.get("enum_schema"), c.get("enum_name"))
            named = mirror_names and ek[0] is not None and await conn.fetchval(
                "SELECT 1 FROM ic_meta.link_types WHERE source = $1 AND schema_name = $2 "
                "AND type_name = $3 AND kind = 'enum'", source, ek[0], ek[1])
            if named:
                # Phase 15 gap B: the source schema and name, so SQL naming the type resolves
                if ek in blocked_types:
                    raise ValueError("type mirror replace is blocked")
                ts, tn = ek
            else:
                ts, tn = await _ensure_enum(conn, source, c["enum_key"], c["enum_labels"],
                                            roles)
            parts.append("%I %I.%I" + ("[]" if c.get("is_array") else "")
                         + " OPTIONS (column_name %L)")
            vals += [c["name"], ts, tn, c["name"]]
        else:
            parts.append(f"%I {_safe_type(c)} OPTIONS (column_name %L)")
            vals += [c["name"], c["name"]]
    coldefs = ", ".join(parts)
    vals += [source, schema, table]
    await exec_fmt(
        conn, f"CREATE FOREIGN TABLE %I.%I ({coldefs}) SERVER %I "
              "OPTIONS (schema_name %L, table_name %L)", *vals)
    return private, cols, unlinked


async def _create_view(conn, private: str, schema: str, table: str, cols: list[dict],
                       roles: Roles, *, view: str | None = None) -> None:
    if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", schema)
    await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", schema, roles.reader)
    sel = ", ".join("%I" for _ in cols)
    vname = view or table  # Phase 15 gap 2 (WR-14)
    await exec_fmt(conn, f"CREATE VIEW %I.%I AS SELECT {sel} FROM %I.%I",
                   schema, vname, *[c["name"] for c in cols], private, table)
    await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", schema, vname, roles.reader)


async def _probe(conn, private: str, table: str) -> None:
    await exec_fmt(conn, "SELECT 1 FROM %I.%I LIMIT 1", private, table)


async def _upsert_link_object(conn, source, schema, table, private, hash_, unlinked=None,
                              *, view_name: str | None = None):
    await conn.execute(
        "INSERT INTO ic_meta.link_objects (source, remote_schema, remote_table, "
        "private_schema, mirrored_schema, col_hash, linked_at, unlinked_columns, view_name) "
        "VALUES ($1,$2,$3,$4,$2,$5,now(),$6::jsonb,$7) "
        "ON CONFLICT (source, remote_schema, remote_table) DO UPDATE SET "
        "private_schema = EXCLUDED.private_schema, mirrored_schema = EXCLUDED.mirrored_schema, "
        "col_hash = EXCLUDED.col_hash, linked_at = now(), "
        "unlinked_columns = EXCLUDED.unlinked_columns, view_name = EXCLUDED.view_name",
        source, schema, table, private, hash_, json.dumps(unlinked or []), view_name)


async def _delete_link_object(conn, source, schema, table):
    await conn.execute(
        "DELETE FROM ic_meta.link_objects WHERE source = $1 AND remote_schema = $2 "
        "AND remote_table = $3", source, schema, table)


async def apply_changes(conn, source: str, snapshot, roles: Roles = DEFAULT_ROLES,
                        remote_user: str = "", *, mirror_names: bool = True,
                        blocked_types: frozenset = frozenset(),
                        keep_keys: frozenset = frozenset()) -> dict:
    """Make the warehouse objects match the snapshot (Option A, D-13): a private
    foreign table plus an owner-owned mirrored view per readable relation. Each
    table change runs in its own transaction with a 5 s lock timeout. Nothing
    is dropped implicitly; a blocked drop or replace keeps the old objects and is reported
    as stale. An unchanged plan issues no DDL."""
    tables = []
    for t in _tables_of(snapshot):
        if mirrored_schema_allowed(t["schema_name"]):
            tables.append(t)
        else:
            log.warning("skipped relation in reserved schema %s", t["schema_name"])
    by = {(t["schema_name"], t["table_name"]): t for t in tables}
    rows = await conn.fetch(
        "SELECT remote_schema, remote_table, col_hash, private_schema FROM ic_meta.link_objects "
        "WHERE source = $1", source)
    cur = {(r["remote_schema"], r["remote_table"]): dict(r) for r in rows}
    plan = plan_changes(tables, [dict(r) for r in rows])
    res: dict = {"created": 0, "replaced": 0, "dropped": 0, "stale": [], "linked": set(),
                 "reverted": []}
    stale: list[str] = res["stale"]
    linked: set = res["linked"]
    # Phase 17 (CONS-05): tables the owner imported are left alone by the sweep; one with no
    # link_objects row (lost warehouse volume) is created live below and reported as reverted
    kept = {k for k in keep_keys if k in cur}
    for k in kept:
        for part in ("create", "replace", "drop"):
            plan[part] = [x for x in plan[part] if x != k]
        linked.add(k)

    async def one(fn):
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            return await fn()

    def priv_of(key):
        return (cur.get(key) or {}).get("private_schema") or private_schema(source, key[0])

    async def remove(key):
        await _drop_objects(conn, key[0], priv_of(key), key[1])
        await _delete_link_object(conn, source, key[0], key[1])

    for key in plan["drop"]:
        try:
            await one(lambda key=key: remove(key))
            res["dropped"] += 1
        except asyncpg.PostgresError as e:
            log.warning("drop of %s.%s blocked: %s", key[0], key[1], type(e).__name__)
            stale.append(f"{key[0]}.{key[1]}")
            linked.add(key)

    for key in plan["create"] + plan["replace"]:
        t = by[key]
        is_replace = key in plan["replace"]

        async def make(t=t, key=key, is_replace=is_replace):
            # orphans of a lost link_objects row are dropped too
            await _drop_objects(conn, key[0], priv_of(key), key[1])
            p, linked_cols, unl = await _create_foreign_table(
                conn, source, t, roles, mirror_names=mirror_names, blocked_types=blocked_types)
            if t.get("probe"):
                await _probe(conn, p, key[1])
            await _create_view(conn, p, key[0], key[1], linked_cols, roles)
            await _upsert_link_object(conn, source, key[0], key[1], p, t["col_hash"], unl)
        try:
            await one(make)
            res["replaced" if is_replace else "created"] += 1
            linked.add(key)
            if key in keep_keys:
                res["reverted"].append(f"{key[0]}.{key[1]}")
        except (asyncpg.PostgresError, ValueError) as e:
            if t.get("probe") and type(e).__name__ in _DENIED:
                # a revoked matview is skipped (S12); a replace removes the old objects
                if is_replace:
                    try:
                        await one(lambda key=key: remove(key))
                        res["dropped"] += 1
                    except asyncpg.PostgresError:
                        stale.append(f"{key[0]}.{key[1]}")
                        linked.add(key)
                continue
            log.warning("link of %s.%s not applied: %s", key[0], key[1], type(e).__name__)
            stale.append(f"{key[0]}.{key[1]}")
            if is_replace:
                linked.add(key)

    skip = set(plan["create"]) | set(plan["replace"]) | set(plan["drop"])
    for key, t in by.items():
        if key in skip or key in kept:
            continue
        linked.add(key)
        if not t.get("probe"):
            continue
        try:  # linked matviews are re-probed on every run
            async with conn.transaction():
                await _probe(conn, priv_of(key), key[1])
        except asyncpg.PostgresError as e:
            if type(e).__name__ not in _DENIED:
                continue
            try:
                await one(lambda key=key: remove(key))
                res["dropped"] += 1
                linked.discard(key)
            except asyncpg.PostgresError:
                stale.append(f"{key[0]}.{key[1]}")
    return res


async def refresh_catalog(conn, source: str, tables: list[dict], linked: set) -> int:
    """Keep ic_meta.source_catalog equal to the source for every linked table,
    comparing the full v1.0-shaped row hash (D-17, D-18, WH-05). Writes nothing
    when every row matches. Returns rows inserted, updated or deleted."""
    stored = {(r["schema_name"], r["table_name"]): r["row_hash"] for r in await conn.fetch(
        "SELECT schema_name, table_name, row_hash FROM ic_meta.source_catalog "
        "WHERE source = $1", source)}
    keep = {(t["schema_name"], t["table_name"]): t for t in tables
            if (t["schema_name"], t["table_name"]) in linked}
    todo = [t for k, t in keep.items() if stored.get(k) != t["row_hash"]]
    gone = [k for k in stored if k not in keep]
    if not todo and not gone:
        return 0
    async with conn.transaction():
        for t in todo:
            v1 = v1_row(t)
            await conn.execute(
                "INSERT INTO ic_meta.source_catalog (source, schema_name, table_name, relkind, "
                "table_comment, row_estimate, columns, checks, col_hash, row_hash) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10) "
                "ON CONFLICT (source, schema_name, table_name) DO UPDATE SET "
                "relkind = EXCLUDED.relkind, table_comment = EXCLUDED.table_comment, "
                "row_estimate = EXCLUDED.row_estimate, columns = EXCLUDED.columns, "
                "checks = EXCLUDED.checks, col_hash = EXCLUDED.col_hash, "
                "row_hash = EXCLUDED.row_hash",
                source, t["schema_name"], t["table_name"], t["relkind"], t["table_comment"],
                t["row_estimate"], json.dumps(v1["columns"], default=str),
                json.dumps(t.get("checks") or []), t["col_hash"], t["row_hash"])
        for s, n in gone:
            await conn.execute(
                "DELETE FROM ic_meta.source_catalog WHERE source = $1 AND schema_name = $2 "
                "AND table_name = $3", source, s, n)
    return len(todo) + len(gone)


async def _record_state(conn, source: str, status: str, detail, *, tables_linked=None,
                        catalog_hash=None, tz=None, sp=None, ok=False):
    await conn.execute(
        "INSERT INTO ic_meta.link_state (source, linked_at, catalog_hash, tables_linked, status, "
        "detail, source_timezone, source_search_path) "
        "VALUES ($1, CASE WHEN $8::boolean THEN now() END, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (source) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail, "
        "linked_at = CASE WHEN $8::boolean THEN now() ELSE ic_meta.link_state.linked_at END, "
        "catalog_hash = coalesce(EXCLUDED.catalog_hash, ic_meta.link_state.catalog_hash), "
        "tables_linked = coalesce(EXCLUDED.tables_linked, ic_meta.link_state.tables_linked), "
        "source_timezone = coalesce(EXCLUDED.source_timezone, ic_meta.link_state.source_timezone), "
        "source_search_path = coalesce(EXCLUDED.source_search_path, "
        "ic_meta.link_state.source_search_path)",
        source, catalog_hash, tables_linked, status, detail, tz, sp, ok)


def _error_detail(e: Exception, secrets_: tuple = ()) -> str:
    from . import secretbox
    text = str(e).strip()
    msg = text.splitlines()[0] if text else ""
    for s in secrets_:
        if s:
            msg = msg.replace(s, "[redacted]")
    return f"{type(e).__name__}: {secretbox.redact(msg)}"[:300]


async def link_source(conn, source: str, url: str, roles: Roles = DEFAULT_ROLES,
                      *, mirror_names: bool = True, keep_keys: frozenset = frozenset()) -> dict:
    """Link (or relink) one source on an open wh_owner connection. Never raises;
    returns {"status": "ok"|"stale"|"error"|"busy", ...}."""
    try:
        got = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", f"ic_link:{source}")
    except Exception as e:
        return {"status": "error", "detail": _error_detail(e), "tables_linked": 0,
                "created": 0, "replaced": 0, "dropped": 0, "stale": [],
                "catalog_updated": 0, "session": {}, "unsupported_typmod": []}
    if not got:
        return {"status": "busy"}
    pw = ""
    try:
        opts = parse_source_url(url)
        pw = opts.password
        await ensure_meta(conn, roles)
        await ensure_server(conn, source, opts)
        await ensure_catalog_tables(conn, source, roles)
        snap = await fetch_snapshot(conn, source, opts)
        tables = [t for t in snap["tables"] if mirrored_schema_allowed(t["schema_name"])]
        # Phase 15 gap B: source enum and domain names exist in the warehouse before the tables
        types_res = {"created": 0, "replaced": 0, "dropped": 0, "blocked": [],
                     "blocked_keys": set(), "unsupported": []}
        if mirror_names:
            types_res = await ensure_type_mirrors(
                conn, source, snap.get("user_types") or [], roles,
                snap.get("unsupported_types"))
        applied = await apply_changes(conn, source, {"tables": tables}, roles, opts.user,
                                      mirror_names=mirror_names,
                                      blocked_types=frozenset(types_res["blocked_keys"]),
                                      keep_keys=frozenset(keep_keys))
        if mirror_names:
            types_res["dropped"] = await drop_vanished_type_mirrors(
                conn, source, snap.get("user_types") or [])
        await drop_orphan_hash_enums(conn, source)
        type_schemas: set = set()
        if mirror_names:
            on_path = set(snap.get("type_path") or [])
            type_schemas = {r["schema_name"] for r in await conn.fetch(
                "SELECT DISTINCT schema_name FROM ic_meta.link_types WHERE source = $1", source)
                if r["schema_name"] in on_path}
        linked = applied["linked"]
        updated = await refresh_catalog(conn, source, tables, linked)
        keep = [t for t in tables if (t["schema_name"], t["table_name"]) in linked]
        sp = search_path_from(snap.get("search_path_raw") or "", opts.user,
                              {t["schema_name"] for t in keep} | type_schemas)
        # Phase 14 gap closure (14-12): ic_types joins the reader path when the source role sees the types
        uses_types = bool(snap.get("uses_types_path")) and any(
            c.get("rkind") == "ext" and c.get("ext_visible")
            for t in keep for c in t.get("columns") or [])
        sp = session_search_path(sp, uses_types)
        tz = snap.get("timezone")
        chash = hashlib.sha256(
            "\n".join(sorted(t["row_hash"] for t in keep)).encode()).hexdigest()
        status = "stale" if applied["stale"] else "ok"
        detail = ("stale: " + ", ".join(applied["stale"]))[:300] if applied["stale"] else None
        await _record_state(conn, source, status, detail, tables_linked=len(linked),
                            catalog_hash=chash, tz=tz, sp=sp, ok=True)
        return {"status": status, "tables_linked": len(linked),
                "created": applied["created"], "replaced": applied["replaced"],
                "dropped": applied["dropped"], "stale": applied["stale"],
                "reverted": applied.get("reverted", []),
                "catalog_updated": updated, "catalog_hash": chash,
                "session": {"TimeZone": tz, "search_path": sp}, "detail": detail,
                "unsupported_typmod": snap.get("unsupported_typmod") or [],
                "types": {k: v for k, v in types_res.items() if k != "blocked_keys"}}
    except Exception as e:
        detail = _error_detail(e, (pw,))
        log.warning("link of %s failed: %s", source, type(e).__name__)
        try:
            await _record_state(conn, source, "error", detail)
        except Exception:
            pass
        return {"status": "error", "detail": detail, "tables_linked": 0, "created": 0,
                "replaced": 0, "dropped": 0, "stale": [], "catalog_updated": 0,
                "session": {}, "unsupported_typmod": []}
    finally:
        try:
            await conn.fetchval("SELECT pg_advisory_unlock(hashtext($1))", f"ic_link:{source}")
        except Exception:
            pass


# ── Phase 16: model schema DDL (as wh_owner; called only by semanticmodel.apply) ──
# Every function acts on an open wh_owner connection. The derived SELECT text enters DDL
# only through "%s" after semanticmodel has validated it (modelcompiler.derived_select_refs
# plus check_reader_refs below), so no unchecked text reaches the server. Nothing here
# uses the dependent-object override: a drop that other objects depend on is refused.
MODEL_SCHEMA = "model"
MODEL_DDL_TIMEOUT = "120s"
_MODEL_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ModelDDLRefused(Exception):
    """A model DDL step was refused. The message is fixed human text, never a driver
    message, host, user or SQL."""


def _model_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _MODEL_NAME_RE.match(name):
        raise ModelDDLRefused(f"invalid model {what}")
    return name


async def ensure_model_schema(conn, roles: Roles = DEFAULT_ROLES) -> None:
    if await conn.fetchval("SELECT to_regclass('ic_meta.link_objects') IS NOT NULL"):
        if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM ic_meta.link_objects WHERE mirrored_schema = $1)",
                MODEL_SCHEMA):
            raise ModelDDLRefused(
                "a source schema named model is linked; the semantic model schema cannot share it")
    await exec_fmt(conn, "CREATE SCHEMA IF NOT EXISTS %I", MODEL_SCHEMA)
    await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", MODEL_SCHEMA, roles.reader)


async def check_reader_refs(conn, relations, roles: Roles = DEFAULT_ROLES) -> None:
    """Every relation a derived SELECT reads must be schema-qualified, outside ic_*, pg_*
    and information_schema, and SELECT-able by the reader role."""
    for rel in relations or ():
        parts = rel.split(".") if isinstance(rel, str) else []
        if len(parts) != 2 or not all(parts):
            raise ModelDDLRefused("a view may only read schema-qualified relations")
        schema, table = parts
        if schema.startswith(("pg_", "ic_")) or schema == "information_schema":
            raise ModelDDLRefused(f"schema {schema} cannot be used in a model view")
        try:
            ok = await conn.fetchval(
                "SELECT has_table_privilege($1, format('%I.%I', $2::text, $3::text), 'SELECT')",
                roles.reader, schema, table)
        except asyncpg.UndefinedTableError:
            raise ModelDDLRefused(f"relation {rel} was not found") from None
        except asyncpg.PostgresError:
            raise ModelDDLRefused(f"relation {rel} could not be checked") from None
        if not ok:
            raise ModelDDLRefused(f"the read-only role cannot read {rel}")


async def model_relation_columns(conn, name: str) -> dict:
    rows = await conn.fetch(
        "SELECT a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum", MODEL_SCHEMA, name)
    return {r["col"]: r["typ"] for r in rows}


async def create_model_view(conn, name: str, select_sql: str, roles: Roles = DEFAULT_ROLES) -> dict:
    _model_name(name)
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{MODEL_DDL_TIMEOUT}'")
            await exec_fmt(conn, "CREATE OR REPLACE VIEW %I.%I WITH (security_invoker = true) AS %s",
                           MODEL_SCHEMA, name, select_sql)
            await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", MODEL_SCHEMA, name, roles.reader)
    except asyncpg.PostgresError as e:
        if isinstance(e, asyncpg.InvalidTableDefinitionError):
            raise ModelDDLRefused(
                "the view's columns changed; retire it and create it under a new name") from None
        raise ModelDDLRefused("the view could not be created") from None
    return await model_relation_columns(conn, name)


async def create_model_matview(conn, name: str, select_sql: str, roles: Roles = DEFAULT_ROLES) -> dict:
    _model_name(name)
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{MODEL_DDL_TIMEOUT}'")
            await exec_fmt(conn, "CREATE MATERIALIZED VIEW IF NOT EXISTS %I.%I AS %s WITH DATA",
                           MODEL_SCHEMA, name, select_sql)
            await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", MODEL_SCHEMA, name, roles.reader)
    except asyncpg.PostgresError:
        raise ModelDDLRefused("the materialized view could not be created") from None
    return await model_relation_columns(conn, name)


async def refresh_model_matview(conn, name: str) -> None:
    _model_name(name)
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{MODEL_DDL_TIMEOUT}'")
            await exec_fmt(conn, "REFRESH MATERIALIZED VIEW %I.%I", MODEL_SCHEMA, name)
    except asyncpg.PostgresError:
        raise ModelDDLRefused("the materialized view could not be refreshed") from None


async def create_model_index(conn, name: str, matview: str, columns) -> None:
    _model_name(name, "index name")
    _model_name(matview, "relation")
    kind = await conn.fetchval(
        "SELECT c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2", MODEL_SCHEMA, matview)
    if kind != "m":
        raise ModelDDLRefused("indexes are only allowed on materialized views in schema model")
    have = await model_relation_columns(conn, matview)
    cols = list(columns or ())
    if not cols or any(c not in have for c in cols):
        raise ModelDDLRefused("the index names a column the materialized view does not have")
    template = "CREATE INDEX IF NOT EXISTS %I ON %I.%I (" + ", ".join("%I" for _ in cols) + ")"
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{MODEL_DDL_TIMEOUT}'")
            await exec_fmt(conn, template, name, MODEL_SCHEMA, matview, *cols)
    except asyncpg.PostgresError:
        raise ModelDDLRefused("the index could not be created") from None


_DROP_TEMPLATES = {
    "view": "DROP VIEW IF EXISTS %I.%I",
    "materialized_view": "DROP MATERIALIZED VIEW IF EXISTS %I.%I",
    "index": "DROP INDEX IF EXISTS %I.%I",
}


async def drop_model_object(conn, kind: str, name: str) -> None:
    template = _DROP_TEMPLATES.get(kind)
    if template is None:
        raise ModelDDLRefused("unknown model object kind")
    _model_name(name)
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{MODEL_DDL_TIMEOUT}'")
            await exec_fmt(conn, template, MODEL_SCHEMA, name)
    except asyncpg.DependentObjectsStillExistError:
        raise ModelDDLRefused("other model objects depend on it") from None
    except asyncpg.PostgresError:
        raise ModelDDLRefused("the object could not be dropped") from None
# ── End Phase 16 ──
