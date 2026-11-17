"""Phase 4 (SERVE-01, SERVE-03): the schema.sql Phase 4 block and the dashboards.py
Phase 4 helpers. Pure text and fakes; no DB, LLM or network."""
import base64
import hashlib
import logging
import re
from pathlib import Path

import pytest

from app import dashboards as d
from app.dashboards import DashboardError

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")
START = "-- Phase 4: Dashboard serving"
END = "-- end Phase 4"
DOC = Path(__file__).resolve().parents[2] / "docs" / "DASHBOARD_CONTRACT.md"
LIB_INTEGRITY = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
FENCE = "`" * 3


def _block() -> str:
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    assert SCHEMA.index(END) > SCHEMA.index(START)
    return SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]


# ------------------------------------------------------------------ schema

def test_block_delimited_once_and_positioned():
    _block()
    assert SCHEMA.index(START) > SCHEMA.index("-- end Phase 3")
    assert SCHEMA.index(END) < SCHEMA.index("Phase 2: Context store")


def test_schema_still_ends_with_phase2_marker():
    # Phase 12 appends its own blocks after the Phase 2 block.
    m = "-- End Phase 2: Context store"
    assert m in SCHEMA
    assert "-- Phase 2" not in SCHEMA[SCHEMA.index(m) + len(m):]


def test_table_columns_idempotent():
    b = _block()
    assert "CREATE TABLE IF NOT EXISTS dashboard_preview_tokens" in b
    assert not re.search(r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", b)
    for pat in (r"token_sha256\s+TEXT\s+PRIMARY\s+KEY",
                r"version_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+dashboard_versions\(id\)\s+ON\s+DELETE\s+CASCADE",
                r"expires_at\s+TIMESTAMPTZ\s+NOT\s+NULL",
                r"created_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\(\)"):
        assert re.search(pat, b), pat
    assert re.search(r"CREATE INDEX IF NOT EXISTS idx_dashboard_preview_tokens_version\s+ON\s+"
                     r"dashboard_preview_tokens\s*\(version_id\)", b)


def test_block_stores_no_raw_token_and_touches_nothing_else():
    b = _block()
    assert not re.search(r"\btoken\s+TEXT", b)
    for bad in ("-- end Phase 3", "-- Phase 3: Dashboard storage", "Phase 2: Context store",
                "DROP", "DELETE FROM"):
        assert bad not in b


# ------------------------------------------------------------------ fakes

class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.calls.append(("BEGIN", "", ()))

    async def __aexit__(self, *exc):
        self.conn.calls.append(("END", "", ()))
        return False


class RecConn:
    def __init__(self, rules=None):
        self.calls = []
        self.rules = list(rules or [])

    def transaction(self):
        return _Tx(self)

    def _resp(self, method, sql, args, default):
        self.calls.append((method, sql, args))
        for sub, resp in self.rules:
            if sub in sql:
                return resp(*args) if callable(resp) else resp
        return default

    async def fetchrow(self, sql, *args):
        return self._resp("fetchrow", sql, args, None)

    async def fetchval(self, sql, *args):
        return self._resp("fetchval", sql, args, None)

    async def fetch(self, sql, *args):
        return self._resp("fetch", sql, args, [])

    async def execute(self, sql, *args):
        return self._resp("execute", sql, args, "DELETE 0")

    def db_calls(self):
        return [c for c in self.calls if c[0] not in ("BEGIN", "END")]


def _example_html() -> str:
    m = re.search(FENCE + r"html\n(.*?)" + FENCE, DOC.read_text(encoding="utf-8"), re.S)
    assert m
    return m.group(1)


def _tok(body: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()


# ------------------------------------------------------------------ inline_script_hashes

def test_inline_hashes_contract_example():
    html = _example_html()
    got = d.inline_script_hashes(html)
    assert isinstance(got, frozenset) and len(got) == 1
    assert LIB_INTEGRITY not in got
    assert got == frozenset(h for h in d.script_hashes(html) if h != LIB_INTEGRITY)


def test_inline_hashes_exclude_src_integrity():
    fake = "sha256-" + "A" * 43 + "="
    code = ('<script src="/d/_lib/x.js" integrity="%s"></script>'
            "<script>var a=1;</script><script>var b=2;</script>" % fake)
    got = d.inline_script_hashes(code)
    assert got == frozenset({_tok("var a=1;"), _tok("var b=2;")})
    assert fake not in got


def test_inline_hashes_never_raise_on_base():
    got = d.inline_script_hashes('<base href="x"><script>var a=1;</script>')
    assert got == frozenset({_tok("var a=1;")})


def test_inline_hashes_empty():
    assert d.inline_script_hashes("<p>hi</p>") == frozenset()


# ------------------------------------------------------------------ digest / mint

def test_digest():
    assert d.preview_token_digest("abc") == hashlib.sha256(b"abc").hexdigest()


def _mint_conn(status="draft", row=True):
    return RecConn([
        ("FROM dashboard_versions v", {"id": 11, "status": status} if row else None),
        ("INSERT INTO dashboard_preview_tokens", "2030-01-01"),
    ])


async def test_mint_ok_stores_digest_only(caplog):
    caplog.set_level(logging.DEBUG)
    c = _mint_conn()
    out = await d.mint_preview_token(c, slug="sales", version=2)
    assert set(out) == {"slug", "version", "preview_token", "expires_at", "path"}
    assert out["path"] == "/d/sales/v/2/" and len(out["preview_token"]) >= 43
    ins = next(x for x in c.db_calls() if "INSERT INTO dashboard_preview_tokens" in x[1])
    assert ins[2][0] == d.preview_token_digest(out["preview_token"])
    assert ins[2][1] == 11 and ins[2][2] == d.PREVIEW_TTL_DEFAULT
    assert "make_interval(mins => $3)" in ins[1]
    assert all(out["preview_token"] not in str(x[2]) for x in c.db_calls())
    assert out["preview_token"] not in caplog.text
    dele = next(x for x in c.db_calls() if x[1].startswith("DELETE FROM dashboard_preview_tokens"))
    assert "expires_at <= now()" in dele[1] and dele[2] == (11,)
    assert c.calls.index(("BEGIN", "", ())) < c.calls.index(dele) < c.calls.index(ins)


async def test_mint_missing_version_404():
    with pytest.raises(DashboardError) as e:
        await d.mint_preview_token(_mint_conn(row=False), slug="sales", version=2)
    assert e.value.status == 404


@pytest.mark.parametrize("status", ["published", "retired"])
async def test_mint_non_draft_409(status):
    with pytest.raises(DashboardError) as e:
        await d.mint_preview_token(_mint_conn(status), slug="sales", version=2)
    assert e.value.status == 409


@pytest.mark.parametrize("kw", [
    {"slug": "Bad Slug", "version": 1}, {"slug": "sales", "version": True},
    {"slug": "sales", "version": 1, "ttl_minutes": 4},
    {"slug": "sales", "version": 1, "ttl_minutes": 1441}])
async def test_mint_invalid_422(kw):
    with pytest.raises(DashboardError) as e:
        await d.mint_preview_token(_mint_conn(), **kw)
    assert e.value.status == 422


@pytest.mark.parametrize("ttl", [5, 1440])
async def test_mint_ttl_bounds_accepted(ttl):
    out = await d.mint_preview_token(_mint_conn(), slug="sales", version=1, ttl_minutes=ttl)
    assert out["preview_token"]


# ------------------------------------------------------------------ preview_token_ok

async def test_ok_true_and_query_shape():
    c = RecConn([("FROM dashboard_preview_tokens", 1)])
    assert await d.preview_token_ok(c, version_id=7, token="t") is True
    (m, sql, args), = c.db_calls()
    assert m == "fetchval" and args == (d.preview_token_digest("t"), 7)
    for frag in ("token_sha256 = $1", "version_id = $2", "expires_at > now()"):
        assert frag in sql


async def test_ok_false_when_no_row():
    assert await d.preview_token_ok(RecConn(), version_id=7, token="t") is False


@pytest.mark.parametrize("tok", [None, "", 123, "x" * 129])
async def test_ok_false_without_db(tok):
    c = RecConn([("FROM dashboard_preview_tokens", 1)])
    assert await d.preview_token_ok(c, version_id=7, token=tok) is False
    assert c.calls == []
