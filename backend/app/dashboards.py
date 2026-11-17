"""Dashboards service seam, pure layer.

This is the ONE dashboards seam (like cases_util for case notes): pure
validators here plus, from plan 03-03, connection-taking operations. It adds
no agent tool or skill (build rule; Phase 6 wraps it). Stored query SQL is only
validated here with guardrails.validate_readonly_sql and is never executed:
this module never touches the tenant DB. Query entries are SQL (validated,
never executed here) or a kpi_history reference to a watch. View tokens must never be logged or
copied into actions payloads.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
from html.parser import HTMLParser

from .guardrails import SQLGuardrailError, validate_readonly_sql

log = logging.getLogger("dashboards")

# Phase 1 contract values; one constant each so the merge step can adjust them.
MAX_CODE_BYTES = 262_144
MAX_QUERIES = 20
QUERY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
QUERY_KEYS = frozenset({"id", "sql", "refresh_minutes"})
REFRESH_MIN, REFRESH_MAX, REFRESH_DEFAULT = 5, 1440, 15
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TITLE_CAP, NOTE_CAP = 200, 2000
INTEGRITY_RE = re.compile(r"^sha(256|384|512)-[A-Za-z0-9+/]+={0,2}$")  # one SRI token, also a CSP hash-source
LIB_PATH_PREFIX = "/d/_lib/"  # only the vendored library's integrity is honoured
STATUSES = ("draft", "published", "retired")
AUTHORS = ("agent", "operator")


class DashboardError(ValueError):
    """Input or state error; .status is the HTTP-class code (422, 404 or 409)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def validate_slug(slug) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise DashboardError(
            422, "slug must be 1-64 chars of lowercase letters, digits and hyphens, "
                 "not starting or ending with a hyphen")
    return slug


def validate_title(title) -> str:
    if not isinstance(title, str):
        raise DashboardError(422, "title must be text")
    title = title.strip()
    if not title:
        raise DashboardError(422, "title required")
    if len(title) > TITLE_CAP:
        raise DashboardError(422, f"title exceeds {TITLE_CAP} characters")
    if "\x00" in title:
        raise DashboardError(422, "title contains a NUL byte")
    return _utf8(title, "title")


def validate_note(note, field: str = "change_note") -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise DashboardError(422, f"{field} must be text")
    if "\x00" in note:
        raise DashboardError(422, f"{field} contains a NUL byte")
    note = note.strip()
    if len(note) > NOTE_CAP:
        raise DashboardError(422, f"{field} exceeds {NOTE_CAP} characters")
    return _utf8(note, field) or None


INT4_MAX, INT8_MAX = 2**31 - 1, 2**63 - 1


def require_int(value, field: str, *, hi: int = INT4_MAX) -> int:
    # bool is an int subclass in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or not -hi - 1 <= value <= hi:
        raise DashboardError(422, f"{field} must be an integer in range")
    return value


def _utf8(s: str, field: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise DashboardError(422, f"{field} is not valid UTF-8") from None
    return s


def normalize_code(code) -> str:
    if not isinstance(code, str) or not code.strip():
        raise DashboardError(422, "code required")
    code = code.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in code:
        raise DashboardError(422, "code contains a NUL byte")
    _utf8(code, "code")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise DashboardError(422, f"code exceeds {MAX_CODE_BYTES} bytes")
    return code


def validate_queries(queries, max_queries: int = MAX_QUERIES) -> list[dict]:
    if not isinstance(queries, list) or len(queries) > max_queries:
        raise DashboardError(422, f"queries must be a list of at most {max_queries}")
    out, seen = [], set()
    for q in queries:
        # Phase 6: kpi_history source (D1)
        if isinstance(q, dict) and q.get("source") == KPI_SOURCE:
            out.append(_validate_kpi_query(q, seen))
            continue
        # Phase 13.1: forecast source
        if isinstance(q, dict) and q.get("source") == FORECAST_SOURCE:
            out.append(_validate_forecast_query(q, seen))
            continue
        if not isinstance(q, dict) or set(q) - QUERY_KEYS or not {"id", "sql"} <= set(q):
            raise DashboardError(422, f"each query is {{id, sql, refresh_minutes?}}: {q!r:.80}")
        qid, sql = q["id"], q["sql"]
        if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid) or qid in seen:
            raise DashboardError(422, f"bad or duplicate query id {qid!r}")
        rm = q.get("refresh_minutes", REFRESH_DEFAULT)
        if isinstance(rm, bool) or not isinstance(rm, int) or not REFRESH_MIN <= rm <= REFRESH_MAX:
            raise DashboardError(
                422, f"refresh_minutes for {qid} must be an integer {REFRESH_MIN}..{REFRESH_MAX}")
        if not isinstance(sql, str) or "\x00" in sql:
            raise DashboardError(422, f"sql for {qid} must be text")
        try:
            sql = _utf8(validate_readonly_sql(sql), f"sql for {qid}")
        except SQLGuardrailError as e:
            raise DashboardError(422, f"query {qid}: {e}") from e
        seen.add(qid)
        out.append({"id": qid, "sql": sql, "refresh_minutes": rm})
    return out


class _Scripts(HTMLParser):
    """Ordered ("body", text) / ("hash", token) entries in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.items: list[tuple[str, str]] = []
        self.foreign: set[str] = set()  # integrity tokens seen on non-library scripts
        self.has_base = False
        self._cur: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "base":
            self.has_base = True  # would rebase /d/_lib/ onto another host
            return
        if tag != "script":
            return
        # Browsers use the FIRST occurrence of a duplicated attribute.
        a: dict = {}
        for k, v in attrs:
            a.setdefault(k, v)
        if "src" in a:
            self._cur = None
            toks = [t for t in (a.get("integrity") or "").split() if INTEGRITY_RE.fullmatch(t)]
            if (a["src"] or "").strip().startswith(LIB_PATH_PREFIX):
                for tok in toks:
                    self.items.append(("hash", tok))
            else:
                self.foreign.update(toks)
        else:
            self._cur = []

    def handle_data(self, data):
        if self._cur is not None:
            self._cur.append(data)

    def handle_entityref(self, name):
        if self._cur is not None:
            self._cur.append(f"&{name};")

    def handle_charref(self, name):
        if self._cur is not None:
            self._cur.append(f"&#{name};")

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            self.items.append(("body", "".join(self._cur)))
            self._cur = None


def script_hashes(code: str) -> list[str]:
    """CSP hash-sources for `code`, which must already have passed normalize_code.

    Returns the sha256 of each inline script body plus the integrity hash-source
    of /d/_lib/ library scripts, in document order, de-duplicated, without
    surrounding quotes. Integrity on any other src (or on inline scripts) is
    ignored, so agent HTML cannot whitelist a foreign host's script.
    """
    p = _Scripts()
    p.feed(code)
    p.close()
    if p.has_base:
        raise DashboardError(422, "<base> is not allowed in dashboard code")
    out: list[str] = []
    for kind, val in p.items:
        # A hash-source admits ANY script whose SRI matches, so a token also used
        # on a foreign-host script must never reach script-src.
        if kind == "hash" and val in p.foreign:
            raise DashboardError(
                422, "integrity hash on a /d/_lib/ script is reused on a non-library script")
        if kind == "body":
            val = "sha256-" + base64.b64encode(hashlib.sha256(val.encode("utf-8")).digest()).decode()
        if val not in out:
            out.append(val)
    return out


def new_view_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Connection-taking operations (plan 03-03). These are the only code that may
# change dashboard state. Nothing here logs or stores a view token.
# ---------------------------------------------------------------------------


async def save_version(conn, *, slug, title, code, queries, change_note=None,
                       author="agent", task_run_id=None) -> dict:
    """Insert draft version n+1. Never touches current_version_id or the title."""
    slug = validate_slug(slug)
    title = validate_title(title)
    change_note = validate_note(change_note)
    code = normalize_code(code)
    queries = validate_queries(queries)
    if author not in AUTHORS:
        raise DashboardError(422, f"author must be one of {', '.join(AUTHORS)}")
    if task_run_id is not None:
        task_run_id = require_int(task_run_id, "task_run_id", hi=INT8_MAX)
    hashes = script_hashes(code)
    async with conn.transaction():
        # The upsert takes the dashboard row lock, serializing concurrent saves.
        row = await conn.fetchrow(
            "INSERT INTO dashboards (slug, title, view_token) VALUES ($1, $2, $3) "
            "ON CONFLICT (slug) DO UPDATE SET updated_at = now() "
            "RETURNING id, (xmax = 0) AS created",
            slug, title, new_view_token())
        dashboard_id = row["id"]
        version = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM dashboard_versions WHERE dashboard_id = $1",
            dashboard_id)
        version_id = await conn.fetchval(
            "INSERT INTO dashboard_versions (dashboard_id, version, title, code, script_hashes, "
            "queries, status, change_note, author, task_run_id) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'draft', $7, $8, $9) RETURNING id",
            dashboard_id, version, title, code, hashes, json.dumps(queries),
            change_note, author, task_run_id)
        if queries:
            await conn.executemany(
                "INSERT INTO dashboard_snapshots (version_id, query_id, refresh_minutes) "
                "VALUES ($1, $2, $3)",
                [(version_id, q["id"], q["refresh_minutes"]) for q in queries])
    log.info("dashboard %s saved draft v%d (%s)", slug, version, author)
    return {"dashboard_id": dashboard_id, "slug": slug, "version_id": version_id,
            "version": version, "status": "draft", "created": row["created"],
            "script_hashes": hashes, "path": f"/d/{slug}/v/{version}/"}


def lint_refusal(code, queries) -> str | None:
    """Quick 029 F4: why a stored version may not be published, or None when it lints clean.
    dashboardlint imports this module, so it is imported lazily here."""
    from . import dashboardlint
    if isinstance(queries, (str, bytes)):
        try:
            queries = json.loads(queries)
        except ValueError:
            queries = []
    report = dashboardlint.lint(code, queries or [])
    errors = report.get("errors") or []
    if not errors:
        return None
    rules = sorted({e["rule"] for e in errors})
    return f"fails the dashboard lint (rules: {', '.join(rules)}): {errors[0]['message']}"[:300]


async def publish(conn, *, dashboard_id: int, version_id: int,
                  expect_status: tuple[str, ...] | None = ("draft",)) -> dict:
    """Atomic pointer flip. Writes NO actions row: callers own the audit (Phase 6's
    approval path has its own row), so this is reusable by that executor. Nested in a
    caller transaction it runs as a SAVEPOINT. Order: lock, retire, publish, pointer,
    as separate UPDATEs (a single CASE swap raises an order-dependent unique violation).
    `expect_status` is checked under the row lock: a stale approval of a version that
    is no longer in that status raises 409 instead of silently rolling back."""
    dashboard_id = require_int(dashboard_id, "dashboard_id", hi=INT8_MAX)
    version_id = require_int(version_id, "version_id", hi=INT8_MAX)
    async with conn.transaction():
        dash = await conn.fetchrow(
            "SELECT id, current_version_id FROM dashboards WHERE id = $1 FOR UPDATE",
            dashboard_id)
        if dash is None:
            raise DashboardError(404, "dashboard not found")
        target = await conn.fetchrow(
            "SELECT id, version, title, status, code, queries, (spec IS NOT NULL) AS is_spec "
            "FROM dashboard_versions WHERE id = $2 AND dashboard_id = $1",
            dashboard_id, version_id)
        if target is None:
            raise DashboardError(404, "version not found")
        if (expect_status is not None and target["status"] not in expect_status
                and dash["current_version_id"] != version_id):
            raise DashboardError(
                409, f"version {target['version']} is {target['status']}, "
                     f"expected {' or '.join(expect_status)}")
        if dash["current_version_id"] == version_id:
            return {"changed": False, "version_id": target["id"], "version": target["version"],
                    "from_version_id": target["id"], "from_version": target["version"]}
        # Quick 029 F4: every publish path (operator publish and rollback, approved
        # proposals) is linted here, so an unsafe version never becomes the live one.
        # Quick 040: the lint is the HTML lint, so it runs on HTML versions only. A chart
        # spec (Phase 18) was validated by prepare_spec when it was saved, versions are
        # immutable, and its measure-bound queries carry keys the HTML lint refuses.
        refusal = (None if target.get("is_spec")  # a row without the flag lints, as before
                   else lint_refusal(target["code"], target["queries"]))
        if refusal:
            raise DashboardError(
                422, f"cannot publish version {target['version']}: {refusal}; "
                     "save a corrected version")
        old = await conn.fetchrow(
            "UPDATE dashboard_versions SET status = 'retired', retired_at = now() "
            "WHERE dashboard_id = $1 AND status = 'published' RETURNING id, version",
            dashboard_id)
        await conn.execute(
            "UPDATE dashboard_versions SET status = 'published', published_at = now(), "
            "retired_at = NULL WHERE id = $2 AND dashboard_id = $1", dashboard_id, version_id)
        await conn.execute(
            "UPDATE dashboards SET current_version_id = $2, title = $3, updated_at = now() "
            "WHERE id = $1", dashboard_id, version_id, target["title"])
    return {"changed": True, "version_id": target["id"], "version": target["version"],
            "from_version_id": old["id"] if old else None,
            "from_version": old["version"] if old else None}


async def resolve_decider(conn, decided_by=None) -> int:
    """Explicit person id, else the active primary person, else 422 (never anonymous)."""
    if decided_by is not None:
        pid = require_int(decided_by, "decided_by", hi=INT8_MAX)
        if await conn.fetchval("SELECT id FROM people WHERE id = $1", pid) is None:
            raise DashboardError(422, f"decided_by {pid} is not a known person")
        return pid
    pid = await conn.fetchval("SELECT id FROM people WHERE is_primary AND active LIMIT 1")
    if pid is None:
        raise DashboardError(
            422, "Add yourself on the People page and mark yourself primary first, "
                 "so the change records who decided.")
    return pid


async def close_stale_publish_proposals(conn, dashboard_id: int, version: int) -> int:
    """Quick 052: once version N is live, a still-proposed publish proposal for the same
    dashboard with a lower version can only replace it with something older. Reject it
    (decided_by stays NULL) and say why in the payload. Callers hold a transaction."""
    rows = await conn.fetch(
        "UPDATE actions SET status = 'rejected', "
        "payload = payload || jsonb_build_object('superseded_by_version', $2::int) "
        "WHERE kind = 'publish_dashboard' AND status = 'proposed' "
        "AND payload->>'dashboard_id' = $1::text "
        "AND (payload->>'version') ~ '^[0-9]+$' "
        "AND (payload->>'version')::int < $2::int RETURNING id", str(dashboard_id), version)
    return len(rows)


_OP_NEEDS = {"publish": "draft", "rollback": "retired"}


async def operator_set_version(conn, *, slug, version, op, decided_by=None, note=None) -> dict:
    """Operator publish (draft only) or rollback (retired only) plus one executed
    publish_dashboard action row, all in one transaction."""
    if op not in _OP_NEEDS:
        raise DashboardError(422, "op must be 'publish' or 'rollback'")
    slug = validate_slug(slug)
    version = require_int(version, "version")
    note = validate_note(note, "note")
    async with conn.transaction():
        dash = await conn.fetchrow(
            "SELECT id, slug, title, current_version_id FROM dashboards "
            "WHERE slug = $1 FOR UPDATE", slug)
        if dash is None:
            raise DashboardError(404, "dashboard not found")
        target = await conn.fetchrow(
            "SELECT id, version, title, status, change_note FROM dashboard_versions "
            "WHERE dashboard_id = $1 AND version = $2", dash["id"], version)
        if target is None:
            raise DashboardError(404, "version not found")
        need = _OP_NEEDS[op]
        if target["status"] != need:
            raise DashboardError(
                409, f"cannot {op} version {version}: status is {target['status']}, needs {need}")
        if op == "publish":  # Phase 18
            await _p18_refuse_html_draft(conn, target["id"])
        decider = await resolve_decider(conn, decided_by)
        res = await publish(conn, dashboard_id=dash["id"], version_id=target["id"],
                            expect_status=(need,))
        await close_stale_publish_proposals(conn, dash["id"], target["version"])
        payload = {"op": op, "dashboard_id": dash["id"], "slug": slug,
                   "title": target["title"], "version_id": target["id"],
                   "version": target["version"], "from_version_id": res["from_version_id"],
                   "from_version": res["from_version"], "change_note": target["change_note"],
                   "note": note, "decided_via": "ops_api"}
        action_id = await conn.fetchval(
            "INSERT INTO actions (kind, payload, tier, status, decided_by, executed_at) "
            "VALUES ('publish_dashboard', $1::jsonb, 'operator', 'executed', $2, now()) "
            "RETURNING id", json.dumps(payload), decider)
    log.info("dashboard %s %s v%d (action %s)", slug, op, version, action_id)
    return {"slug": slug, "version": version, "from_version": res["from_version"],
            "action_id": action_id, "decided_by": decider}


async def rotate_token(conn, *, slug) -> dict:
    slug = validate_slug(slug)
    row = await conn.fetchrow(
        "UPDATE dashboards SET view_token = $2, token_rotated_at = now(), updated_at = now() "
        "WHERE slug = $1 RETURNING slug, view_token, token_rotated_at", slug, new_view_token())
    if row is None:
        raise DashboardError(404, "dashboard not found")
    log.info("dashboard %s view token rotated", slug)
    return {"slug": row["slug"], "view_token": row["view_token"],
            "token_rotated_at": row["token_rotated_at"], "path": f"/d/{slug}/"}


# ------------------------------------------------------------------- readers
# SELECT-only helpers for the ops API. The list never returns code or the token;
# no reader returns snapshot data rows (Phase 4 serves those on its own origin).

def _decode_json(value):
    """asyncpg returns jsonb as a str unless a codec is set (app/CLAUDE.md trap)."""
    return json.loads(value) if isinstance(value, str) else value


async def list_dashboards(conn, *, limit: int = 100) -> list[dict]:
    limit = max(1, min(500, require_int(limit, "limit")))
    rows = await conn.fetch(
        "SELECT d.id, d.slug, d.title, cv.version AS current_version, "
        "cv.status AS current_status, lv.version AS latest_version, lv.status AS latest_status, "
        "(SELECT count(*) FROM dashboard_versions x WHERE x.dashboard_id = d.id) AS version_count, "
        "cv.last_error AS current_last_error, "
        "(SELECT max(s.refreshed_at) FROM dashboard_snapshots s "
        " WHERE s.version_id = d.current_version_id) AS last_refreshed_at, "
        "d.updated_at "
        "FROM dashboards d "
        "LEFT JOIN dashboard_versions cv ON cv.id = d.current_version_id "
        "LEFT JOIN LATERAL (SELECT version, status FROM dashboard_versions "
        "  WHERE dashboard_id = d.id ORDER BY version DESC LIMIT 1) lv ON true "
        "ORDER BY d.updated_at DESC LIMIT $1", limit)
    out = [dict(r) for r in rows]
    await _p18_list_extras(conn, out)  # Phase 18
    return out


async def get_dashboard(conn, *, slug) -> dict:
    slug = validate_slug(slug)
    dash = await conn.fetchrow(
        "SELECT d.id, d.slug, d.title, d.view_token, d.token_rotated_at, "
        "cv.version AS current_version, d.created_at, d.updated_at "
        "FROM dashboards d LEFT JOIN dashboard_versions cv ON cv.id = d.current_version_id "
        "WHERE d.slug = $1", slug)
    if dash is None:
        raise DashboardError(404, "dashboard not found")
    vers = await conn.fetch(
        "SELECT v.version, v.status, v.title, v.change_note, v.author, v.created_at, "
        "v.published_at, v.retired_at, v.last_error, "
        "pa.decided_by AS published_by, p.name AS published_by_name "
        "FROM dashboard_versions v "
        "LEFT JOIN LATERAL (SELECT a.decided_by FROM actions a "
        "  WHERE a.kind = 'publish_dashboard' AND a.status = 'executed' "
        "  AND a.payload->>'version_id' = v.id::text "
        "  ORDER BY a.id DESC LIMIT 1) pa ON true "
        "LEFT JOIN people p ON p.id = pa.decided_by "
        "WHERE v.dashboard_id = $1 ORDER BY v.version DESC", dash["id"])
    out = dict(dash)
    out["path"] = f"/d/{slug}/"
    versions = []
    for r in vers:
        v = dict(r)
        v["path"] = f"/d/{slug}/v/{v['version']}/"
        versions.append(v)
    await _p18_dashboard_extras(conn, dash["id"], versions)  # Phase 18
    return {"dashboard": out, "versions": versions}


async def get_version(conn, *, slug, version) -> dict:
    slug = validate_slug(slug)
    version = require_int(version, "version")
    dash = await conn.fetchrow(
        "SELECT id, current_version_id FROM dashboards WHERE slug = $1", slug)
    if dash is None:
        raise DashboardError(404, "dashboard not found")
    row = await conn.fetchrow(
        "SELECT id, version, status, title, change_note, author, created_at, published_at, "
        "retired_at, last_error, code, script_hashes, queries FROM dashboard_versions "
        "WHERE dashboard_id = $1 AND version = $2", dash["id"], version)
    if row is None:
        raise DashboardError(404, "version not found")
    snaps = await conn.fetch(
        "SELECT query_id, refresh_minutes, row_count, refreshed_at, error, next_refresh_at "
        "FROM dashboard_snapshots WHERE version_id = $1 ORDER BY query_id", row["id"])
    out = {k: row[k] for k in ("version", "status", "title", "change_note", "author",
                               "created_at", "published_at", "retired_at", "last_error", "code")}
    out["script_hashes"] = list(row["script_hashes"] or [])
    out["queries"] = _decode_json(row["queries"]) or []
    out["is_current"] = dash["current_version_id"] == row["id"]
    out["snapshots"] = [dict(s) for s in snaps]
    await _p18_version_extras(conn, slug, version, out)  # Phase 18
    return out


# ── Phase 4: serving helpers (SERVE-01, SERVE-03) ──
PREVIEW_TTL_DEFAULT = 60
PREVIEW_TTL_MIN = 5
PREVIEW_TTL_MAX = 1440
PREVIEW_TOKEN_MAX = 128


def inline_script_hashes(code: str) -> frozenset[str]:
    """sha256 hash-sources of the inline script bodies in `code`, nothing else.

    Serve-time half of the W1 filter: the /d/ router intersects the stored
    script_hashes with this set, so an integrity token an agent attached to a
    /d/_lib/ src script is never re-admitted into script-src. Uses the same
    _Scripts parser as save time so hashes agree byte for byte; never raises.
    """
    p = _Scripts()
    p.feed(code)
    p.close()
    return frozenset(
        "sha256-" + base64.b64encode(hashlib.sha256(val.encode("utf-8")).digest()).decode()
        for kind, val in p.items if kind == "body")


def preview_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def mint_preview_token(conn, *, slug, version, ttl_minutes=PREVIEW_TTL_DEFAULT) -> dict:
    """Mint a short-lived preview token for a DRAFT version; returns the raw token once.

    No HTTP route calls this in Phase 4; Phase 6 adds the ops-key route behind
    its validators (contract sections 5 and 9). Only the sha256 digest is stored.
    """
    slug = validate_slug(slug)
    version = require_int(version, "version")
    ttl_minutes = require_int(ttl_minutes, "ttl_minutes")
    if not PREVIEW_TTL_MIN <= ttl_minutes <= PREVIEW_TTL_MAX:
        raise DashboardError(422, "ttl_minutes must be 5..1440")
    row = await conn.fetchrow(
        "SELECT v.id, v.status FROM dashboard_versions v JOIN dashboards d "
        "ON d.id = v.dashboard_id WHERE d.slug = $1 AND v.version = $2", slug, version)
    if row is None:
        raise DashboardError(404, "version not found")
    if row["status"] != "draft":
        raise DashboardError(409, "preview tokens are minted only for drafts")
    token = secrets.token_urlsafe(32)
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM dashboard_preview_tokens WHERE version_id = $1 AND expires_at <= now()",
            row["id"])
        expires_at = await conn.fetchval(
            "INSERT INTO dashboard_preview_tokens (token_sha256, version_id, expires_at) "
            "VALUES ($1, $2, now() + make_interval(mins => $3)) RETURNING expires_at",
            preview_token_digest(token), row["id"], ttl_minutes)
    log.info("dashboard %s v%d preview token minted", slug, version)
    return {"slug": slug, "version": version, "preview_token": token,
            "expires_at": expires_at, "path": f"/d/{slug}/v/{version}/"}


async def preview_token_ok(conn, *, version_id: int, token) -> bool:
    """True only for an unexpired token minted for exactly this version_id."""
    if not isinstance(token, str) or not token or len(token) > PREVIEW_TOKEN_MAX:
        return False
    # Lookup is by digest primary key, so no constant-time compare is needed.
    hit = await conn.fetchval(
        "SELECT 1 FROM dashboard_preview_tokens WHERE token_sha256 = $1 "
        "AND version_id = $2 AND expires_at > now()",
        preview_token_digest(token), version_id)
    return hit is not None
# ── end Phase 4 ──


# ── Phase 6: dashboard specialist ──
KPI_SOURCE = "kpi_history"
KPI_QUERY_KEYS = frozenset({"id", "source", "watch_id", "days", "refresh_minutes"})
KPI_DAYS_MIN, KPI_DAYS_MAX, KPI_DAYS_DEFAULT = 1, 400, 30


def _validate_kpi_query(q: dict, seen: set) -> dict:
    """A kpi_history query: {id, source, watch_id, days?, refresh_minutes?}, no sql."""
    if set(q) - KPI_QUERY_KEYS:
        raise DashboardError(
            422, "kpi_history queries take id, source, watch_id, days, refresh_minutes "
                 f"and no sql: {q!r:.80}")
    qid = q.get("id")
    if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid) or qid in seen:
        raise DashboardError(422, f"bad or duplicate query id {qid!r}")
    wid = q.get("watch_id")
    if isinstance(wid, bool) or not isinstance(wid, int) or not 1 <= wid <= INT8_MAX:
        raise DashboardError(422, f"query {qid}: watch_id must be a positive integer")
    days = q.get("days", KPI_DAYS_DEFAULT)
    if isinstance(days, bool) or not isinstance(days, int)             or not KPI_DAYS_MIN <= days <= KPI_DAYS_MAX:
        raise DashboardError(
            422, f"query {qid}: days must be an integer {KPI_DAYS_MIN}..{KPI_DAYS_MAX}")
    rm = q.get("refresh_minutes", REFRESH_DEFAULT)
    if isinstance(rm, bool) or not isinstance(rm, int) or not REFRESH_MIN <= rm <= REFRESH_MAX:
        raise DashboardError(
            422, f"refresh_minutes for {qid} must be an integer {REFRESH_MIN}..{REFRESH_MAX}")
    seen.add(qid)
    return {"id": qid, "source": KPI_SOURCE, "watch_id": wid, "days": days,
            "refresh_minutes": rm}
async def apply_publish_proposal(conn, payload) -> bool:
    """Executor for an APPROVED agent publish_dashboard proposal, called only from
    actionsvc._execute. Re-validates the untrusted payload; publish() row-locks and
    enforces expect_status; writes no actions row (approve() owns the audit)."""
    if not isinstance(payload, dict):
        raise DashboardError(422, "publish proposal payload must be an object")
    did = require_int(payload.get("dashboard_id"), "dashboard_id", hi=INT8_MAX)
    vid = require_int(payload.get("version_id"), "version_id", hi=INT8_MAX)
    slug = validate_slug(payload.get("slug"))
    version = require_int(payload.get("version"), "version")
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT d.slug, v.version FROM dashboards d "
            "JOIN dashboard_versions v ON v.dashboard_id = d.id "
            "WHERE d.id = $1 AND v.id = $2", did, vid)
        if row is None or row["slug"] != slug or row["version"] != version:
            raise DashboardError(409, "publish proposal does not match the stored version")
        await _p18_refuse_html_draft(conn, vid)  # Phase 18
        live = await conn.fetchrow(
            "SELECT v.version FROM dashboards d JOIN dashboard_versions v "
            "ON v.id = d.current_version_id WHERE d.id = $1", did)
        if live is not None and live["version"] > version:
            raise DashboardError(
                409, f"v{version} is older than the live v{live['version']}, so it was not "
                     "published. To go back to it, use Roll back on the Dashboards page.")
        await publish(conn, dashboard_id=did, version_id=vid, expect_status=("draft",))
        await close_stale_publish_proposals(conn, did, version)
    log.info("published dashboard %s v%s via approved proposal", slug, version)
    return True


# ── end Phase 6 ──


# ── Phase 13.1: forecast source (FCST-04) ──
FORECAST_SOURCE = "forecast"
FORECAST_QUERY_KEYS = frozenset({"id", "source", "forecast_id", "refresh_minutes"})


def _validate_forecast_query(q: dict, seen: set) -> dict:
    """A forecast query: {id, source, forecast_id, refresh_minutes?}, no sql."""
    if set(q) - FORECAST_QUERY_KEYS:
        raise DashboardError(
            422, "forecast queries take id, source, forecast_id, refresh_minutes "
                 f"and no sql: {q!r:.80}")
    qid = q.get("id")
    if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid) or qid in seen:
        raise DashboardError(422, f"bad or duplicate query id {qid!r}")
    fid = q.get("forecast_id")
    if isinstance(fid, bool) or not isinstance(fid, int) or not 1 <= fid <= INT8_MAX:
        raise DashboardError(422, f"query {qid}: forecast_id must be a positive integer")
    rm = q.get("refresh_minutes", REFRESH_DEFAULT)
    if isinstance(rm, bool) or not isinstance(rm, int) or not REFRESH_MIN <= rm <= REFRESH_MAX:
        raise DashboardError(
            422, f"refresh_minutes for {qid} must be an integer {REFRESH_MIN}..{REFRESH_MAX}")
    seen.add(qid)
    return {"id": qid, "source": FORECAST_SOURCE, "forecast_id": fid, "refresh_minutes": rm}
# ── end Phase 13.1 ──


# ── Phase 18: chart-spec dashboards (SPEC-03, SPEC-06) ──
HTML_DRAFT_REFUSED = ("this draft is an old HTML dashboard; ask the agent to convert it to a "
                      "chart spec (or turn on dashboard_legacy_html)")


def legacy_html_on() -> bool:
    """The dashboard_legacy_html setting; False when settings cannot be read."""
    try:
        from . import config
        return bool(config.get_settings().dashboard_legacy_html)
    except Exception:  # noqa: BLE001 - a settings failure must never open the HTML door
        return False


async def _p18_refuse_html_draft(conn, version_id) -> None:
    """409 when the version is an HTML draft (spec IS NULL) while legacy HTML is off."""
    row = await conn.fetchrow(
        "SELECT (spec IS NULL) AS is_html FROM dashboard_versions WHERE id = $1", version_id)
    if row is not None and row.get("is_html") and not legacy_html_on():
        raise DashboardError(409, HTML_DRAFT_REFUSED)

MEASURE_SOURCE = "measure"
MEASURE_QUERY_KEYS = frozenset({"id", "source", "measure", "request", "columns", "sql",
                                "refresh_minutes"})
MEASURE_QID_PREFIX = "m_"


class SpecRejected(DashboardError):
    """A chart spec the engine refused; .findings carries every {rule, path, message}."""

    def __init__(self, findings: list[dict]):
        self.findings = list(findings)
        detail = "; ".join(f"{f.get('path')}: {f.get('message')}" for f in self.findings[:3])
        super().__init__(422, detail or "invalid chart spec")


def _refresh_ok(qid, rm) -> int:
    if isinstance(rm, bool) or not isinstance(rm, int) or not REFRESH_MIN <= rm <= REFRESH_MAX:
        raise DashboardError(
            422, f"refresh_minutes for {qid} must be an integer {REFRESH_MIN}..{REFRESH_MAX}")
    return rm


def _validate_measure_query(q: dict, seen: set) -> dict:
    """A frozen compiled measure query: exactly the seven keys, an m_ id, guarded SQL."""
    from .modelcompiler import NAME_RE

    if set(q) != MEASURE_QUERY_KEYS:
        raise DashboardError(422, "measure queries take exactly id, source, measure, request, "
                                  f"columns, sql, refresh_minutes: {sorted(q)!r:.100}")
    qid = q["id"]
    if (not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid)
            or not qid.startswith(MEASURE_QID_PREFIX) or qid in seen):
        raise DashboardError(422, f"bad or duplicate measure query id {qid!r}")
    if not isinstance(q["measure"], str) or not NAME_RE.match(q["measure"]):
        raise DashboardError(422, f"query {qid}: measure name is not valid")
    if not isinstance(q["request"], dict):
        raise DashboardError(422, f"query {qid}: request must be an object")
    cols = q["columns"]
    if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
        raise DashboardError(422, f"query {qid}: columns must be a list of names")
    rm = _refresh_ok(qid, q["refresh_minutes"])
    sql = q["sql"]
    if not isinstance(sql, str) or "\x00" in sql:
        raise DashboardError(422, f"sql for {qid} must be text")
    try:
        sql = _utf8(validate_readonly_sql(sql), f"sql for {qid}")
    except SQLGuardrailError as e:
        raise DashboardError(422, f"query {qid}: {e}") from e
    seen.add(qid)
    return {"id": qid, "source": MEASURE_SOURCE, "measure": q["measure"],
            "request": q["request"], "columns": list(cols), "sql": sql, "refresh_minutes": rm}


def validate_stored_queries(queries, max_queries: int = MAX_QUERIES) -> list[dict]:
    """Validate the stored query list of a spec version: declared SQL, kpi_history and
    forecast entries (through validate_queries) plus frozen measure entries. Spec v2 versions
    pass MAX_QUERIES_V2."""
    if not isinstance(queries, list) or len(queries) > max_queries:
        raise DashboardError(422, f"queries must be a list of at most {max_queries}")
    out, seen = [], set()
    for q in queries:
        if isinstance(q, dict) and q.get("source") == MEASURE_SOURCE:
            out.append(_validate_measure_query(q, seen))
            continue
        one = validate_queries([q])[0]
        if one["id"] in seen:
            raise DashboardError(422, f"bad or duplicate query id {one['id']!r}")
        if one["id"].startswith(MEASURE_QID_PREFIX):
            raise DashboardError(422, f"query id {one['id']!r}: the m_ prefix is reserved "
                                      "for compiled measures")
        seen.add(one["id"])
        out.append(one)
    return out


async def prepare_spec(raw) -> dict:
    """Validate a chart spec and freeze its SQL. Loads the semantic model only when a tile
    binds a measure. Raises SpecRejected (422) with path-named findings."""
    from . import dashboardrender, dashboardspec, dashsettings

    try:
        spec = dashboardspec.parse(raw)
    except dashboardspec.SpecError as e:
        raise SpecRejected(e.findings) from e
    v2 = isinstance(spec, dashboardspec.SpecV2)
    cap = dashboardspec.MAX_QUERIES_V2 if v2 else MAX_QUERIES
    declared, found = [], []
    for i, q in enumerate(spec.queries):
        try:
            declared.append(validate_queries([q.model_dump(mode="json", exclude_none=True)])[0])
        except DashboardError as e:
            found.append({"rule": "query", "path": f"queries[{i}]", "message": str(e)})
    if found:
        raise SpecRejected(found)
    compiled: list[dict] = []
    try:
        if dashboardspec.has_measures(spec):
            from . import semanticmodel
            model = await semanticmodel.load_model()
            if v2:
                compiled = dashboardspec.compile_measures(
                    spec, model, naive_utc=dashsettings.naive_utc())
            else:
                compiled = dashboardspec.compile_measures(spec, model)
        stored = dashboardspec.bind(spec, declared, compiled)
        if v2:
            stored["number_locale"] = spec.number_locale or dashsettings.number_locale()
        queries = dashboardspec.stored_queries(declared, compiled, cap)
    except dashboardspec.SpecError as e:
        raise SpecRejected(e.findings) from e
    try:
        queries = validate_stored_queries(queries, cap)
    except DashboardError as e:
        raise SpecRejected([{"rule": "query", "path": "queries", "message": str(e)}]) from e
    return {"spec": spec, "title": spec.title, "stored": stored, "queries": queries,
            "compiled": compiled,
            "renderer": dashboardrender.renderer_for(2 if v2 else 1)}


async def save_spec_version(conn, *, slug, stored, queries, title, renderer, change_note=None,
                            author="agent", task_run_id=None) -> dict:
    """Insert spec draft n+1 (code '', spec, renderer) plus one snapshot row per query.
    Never touches current_version_id; only an approved publish moves the pointer."""
    from . import dashboardrender
    from .dashboardspec import MAX_QUERIES_V2, MAX_SPEC_BYTES, MAX_SPEC_BYTES_V2

    slug = validate_slug(slug)
    title = validate_title(title)
    change_note = validate_note(change_note)
    pin = dashboardrender.pin(renderer) if isinstance(renderer, str) else None
    if pin is None:
        raise DashboardError(422, f"unknown renderer {renderer!r}")
    if not isinstance(stored, dict):
        raise DashboardError(422, "stored spec must be an object")
    spec_json = json.dumps(stored, ensure_ascii=False)
    v2 = stored.get("spec_version") == 2
    if renderer != dashboardrender.renderer_for(2 if v2 else 1):
        raise DashboardError(422, "renderer does not match spec_version")
    max_bytes = MAX_SPEC_BYTES_V2 if v2 else MAX_SPEC_BYTES
    if len(spec_json.encode("utf-8")) > max_bytes:
        raise DashboardError(422, f"spec exceeds {max_bytes} bytes")
    queries = validate_stored_queries(queries, MAX_QUERIES_V2 if v2 else MAX_QUERIES)
    if author not in AUTHORS:
        raise DashboardError(422, f"author must be one of {', '.join(AUTHORS)}")
    if task_run_id is not None:
        task_run_id = require_int(task_run_id, "task_run_id", hi=INT8_MAX)
    hashes = [pin["csp_hash"], pin["lib_csp_hash"]]
    async with conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO dashboards (slug, title, view_token) VALUES ($1, $2, $3) "
            "ON CONFLICT (slug) DO UPDATE SET updated_at = now() "
            "RETURNING id, (xmax = 0) AS created",
            slug, title, new_view_token())
        dashboard_id = row["id"]
        version = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM dashboard_versions WHERE dashboard_id = $1",
            dashboard_id)
        version_id = await conn.fetchval(
            "INSERT INTO dashboard_versions (dashboard_id, version, title, code, script_hashes, "
            "queries, spec, renderer, status, change_note, author, task_run_id) "
            "VALUES ($1, $2, $3, '', $4, $5::jsonb, $6::jsonb, $7, 'draft', $8, $9, $10) "
            "RETURNING id",
            dashboard_id, version, title, hashes, json.dumps(queries), spec_json, renderer,
            change_note, author, task_run_id)
        if queries:
            await conn.executemany(
                "INSERT INTO dashboard_snapshots (version_id, query_id, refresh_minutes) "
                "VALUES ($1, $2, $3)",
                [(version_id, q["id"], q["refresh_minutes"]) for q in queries])
    log.info("dashboard %s saved spec draft v%d (%s)", slug, version, author)
    return {"dashboard_id": dashboard_id, "slug": slug, "version_id": version_id,
            "version": version, "status": "draft", "created": row["created"],
            "script_hashes": hashes, "path": f"/d/{slug}/v/{version}/", "format": "spec"}


def _spec_of(value):
    v = _decode_json(value)
    return v if isinstance(v, dict) else None


async def _p18_list_extras(conn, rows: list[dict]) -> None:
    """current_format ('spec', 'html' or None) and needs_conversion on each list row."""
    fmt = {}
    ids = [r["id"] for r in rows if r.get("id") is not None]
    if ids:
        got = await conn.fetch(
            "SELECT d.id, (v.spec IS NULL) AS is_html FROM dashboards d "
            "JOIN dashboard_versions v ON v.id = d.current_version_id "
            "WHERE d.id = ANY($1::bigint[])", ids)
        fmt = {g["id"]: ("html" if g.get("is_html") else "spec") for g in got or []}
    off = not legacy_html_on()
    for r in rows:
        cf = fmt.get(r.get("id"))
        r["current_format"] = cf
        r["needs_conversion"] = bool(cf == "html" and off)


async def _p18_dashboard_extras(conn, dashboard_id, versions: list[dict]) -> None:
    """format, tile summary and tile diff against the next lower version, per version."""
    from . import dashboardspec

    got = await conn.fetch(
        "SELECT version, spec, queries FROM dashboard_versions WHERE dashboard_id = $1",
        dashboard_id)
    by = {g["version"]: (_spec_of(g["spec"]), _decode_json(g["queries"]) or []) for g in got or []}
    lower = sorted(by)
    for v in versions:
        spec, queries = by.get(v["version"], (None, []))
        v.update(format="spec" if spec else "html", tiles=[], tiles_added=[], tiles_removed=[],
                 tiles_changed=[], measures=[], queries_used=[],
                 spec_version=(spec.get("spec_version", 1) if spec else None))  # Phase 22
        if not spec:
            continue
        prev = max((n for n in lower if n < v["version"]), default=None)
        old = by[prev][0] if prev is not None else None
        diff = dashboardspec.tile_diff(old, spec)
        summ = dashboardspec.summary(spec, queries)
        v.update(tiles=summ["tiles"], tiles_added=diff["added"], tiles_removed=diff["removed"],
                 tiles_changed=diff["changed"], measures=summ["measures"],
                 queries_used=summ["queries"])


async def _p18_version_extras(conn, slug, version, out: dict) -> None:
    """format, renderer, the authoring-form spec and a summary on a version read."""
    from . import dashboardspec

    row = await conn.fetchrow(
        "SELECT spec, renderer, queries FROM dashboard_versions "
        "WHERE dashboard_id = (SELECT id FROM dashboards WHERE slug = $1) AND version = $2",
        slug, version)
    spec = _spec_of(row.get("spec")) if row else None
    out["format"] = "spec" if spec else "html"
    out["renderer"] = row.get("renderer") if row else None
    out["spec"] = dashboardspec.authoring_form(spec) if spec else None
    if spec:
        out["summary"] = dashboardspec.summary(spec, _decode_json(row["queries"]) or [])
# ── end Phase 18 ──


# ---- Phase 22: owner layout tweak (DCAP-15) -----------------------------------------------------

def _layout_note(s: dict) -> str:
    bits = []
    if s["moved"]:
        bits.append(f"moved {len(s['moved'])}")
    if s["widths"]:
        bits.append(f"resized {len(s['widths'])}")
    if s["renamed"]:
        bits.append(f"renamed {len(s['renamed'])}")
    if s["hidden"]:
        bits.append(f"hid {len(s['hidden'])}")
    if s["shown"]:
        bits.append(f"showed {len(s['shown'])}")
    if s["default_range"]:
        bits.append(f"default range {s['default_range']}")
    return "Owner layout change: " + (", ".join(bits) or "no visible change")


async def save_layout_tweak(conn, *, slug, base_version, changes, decided_by=None) -> dict:
    """Owner layout change on the current published v2 version, saved as an operator draft
    (queries copied verbatim) plus one executed dashboard_layout action, in one transaction.
    Never runs a tenant query, loads the model or compiles."""
    from . import dashboarddesign, dashboardspec

    slug = validate_slug(slug)
    base_version = require_int(base_version, "base_version")
    if not isinstance(changes, dict):
        raise DashboardError(422, "changes must be an object")
    note = validate_note(changes.get("note"), "note")
    async with conn.transaction():
        dash = await conn.fetchrow(
            "SELECT id, slug, current_version_id FROM dashboards WHERE slug = $1 FOR UPDATE",
            slug)
        if dash is None:
            raise DashboardError(404, "dashboard not found")
        row = await conn.fetchrow(
            "SELECT id, version, title, spec, queries, renderer FROM dashboard_versions "
            "WHERE id = $1", dash["current_version_id"]) if dash["current_version_id"] else None
        if row is None or row["version"] != base_version:
            raise DashboardError(
                409, "the dashboard changed since you opened the editor; reload")
        base = _spec_of(row["spec"])
        if base is None or base.get("spec_version") != 2:
            raise DashboardError(
                409, "layout editing needs a spec v2 dashboard; ask the agent to upgrade it")
        new = dashboardspec.layout_tweak(base, changes)
        summ = dashboardspec.layout_summary(base, new)
        queries = _decode_json(row["queries"]) or []
        decider = await resolve_decider(conn, decided_by)
        change_note = _layout_note(summ)
        if note:
            change_note = f"{change_note}. {note}"
        saved = await save_spec_version(
            conn, slug=slug, stored=new, queries=queries, title=row["title"],
            renderer=row["renderer"], change_note=change_note[:500], author="operator",
            task_run_id=None)
        payload = {"slug": slug, "dashboard_id": dash["id"], "base_version": base_version,
                   "version": saved["version"], "version_id": saved["version_id"],
                   "moved": summ["moved"], "widths": summ["widths"],
                   "renamed": summ["renamed"], "hidden": summ["hidden"],
                   "shown": summ["shown"], "default_range": summ["default_range"],
                   "decided_via": "ops_api"}
        action_id = await conn.fetchval(
            "INSERT INTO actions (kind, payload, tier, status, decided_by, executed_at) "
            "VALUES ('dashboard_layout', $1::jsonb, 'operator', 'executed', $2, now()) "
            "RETURNING id", json.dumps(payload), decider)
    try:
        spec = dashboardspec.parse(dashboardspec.authoring_form(new))
        warnings = dashboarddesign.design_lint(spec, new, queries)
    except Exception:  # lint is advice; the draft is already saved
        log.exception("layout lint failed for %s", slug)
        warnings = []
    log.info("dashboard %s layout draft v%d (action %s)", slug, saved["version"], action_id)
    return {"slug": slug, "version": saved["version"], "version_id": saved["version_id"],
            "action_id": action_id, "warnings": warnings}
# ---- end Phase 22: owner layout tweak ----
