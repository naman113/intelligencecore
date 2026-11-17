"""Phase 1 — identity normalization (the load-bearing part of exact resolution).

Pure-function tests; no DB required.
"""

from app.people import _normalize_alt, normalize_email, normalize_phone, resolve


def test_normalize_phone_strips_formatting():
    assert normalize_phone("+91 90000-00101") == "919000000101"
    assert normalize_phone("91-9000000101") == "919000000101"
    assert normalize_phone("919000000101@s.whatsapp.net") == "919000000101"


def test_normalize_phone_drops_leading_trunk_zeros():
    assert normalize_phone("0091 9000000101") == "919000000101"
    assert normalize_phone("") == ""
    assert normalize_phone(None) == ""


def test_normalize_phone_variants_converge():
    canonical = normalize_phone("919000000101")
    for variant in ("+919000000101", "91 9000000101", "919000000101@s.whatsapp.net"):
        assert normalize_phone(variant) == canonical


def test_normalize_email_lowercases_and_trims():
    assert normalize_email("  Priya@ACME.com ") == "priya@acme.com"
    assert normalize_email(None) == ""


# ── one person, several numbers ────────────────────────────────────────────

def test_normalize_alt_dedupes_and_excludes_primary():
    # The primary repeated as an alt would shadow the whatsapp UNIQUE check;
    # duplicates and formatting junk collapse the same way as the primary.
    out = _normalize_alt(["+91 90000-00104", "919000000104", "919000000105", ""],
                         "919000000105")
    assert out == ["919000000104"]


async def test_resolve_matches_alternate_numbers():
    """An inbound message from ANY of a person's numbers must resolve to them —
    the whole point of whatsapp_alt. Pins the SQL shape: primary OR ANY(alt)."""

    class Conn:
        def __init__(self):
            self.sqls = []

        async def fetchval(self, sql, *args):
            self.sqls.append(sql)
            if "whatsapp = $1" in sql:
                assert "ANY(whatsapp_alt)" in sql       # both columns, one query
                return 7 if args[0] == "919000000104" else None
            return None

    conn = Conn()
    assert await resolve(conn, phone="+91 90000-00104") == 7
    assert await resolve(Conn(), phone="910000000000") is None
