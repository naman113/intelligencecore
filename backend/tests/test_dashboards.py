"""Phase 3 dashboards.py pure layer: validators, CSP hashes, tokens. No DB, LLM or network."""

import base64
import hashlib
import re

import pytest

from app.dashboards import (
    MAX_CODE_BYTES, DashboardError, normalize_code, require_int, validate_note,
    new_view_token, script_hashes, validate_queries, validate_slug, validate_title,
)


def _status(fn, *a):
    with pytest.raises(DashboardError) as ei:
        fn(*a)
    assert ei.value.status == 422
    return str(ei.value)


def test_dashboard_error_is_valueerror_with_status():
    e = DashboardError(404, "nope")
    assert isinstance(e, ValueError) and e.status == 404 and str(e) == "nope"


def test_slug_valid():
    assert validate_slug("sales-overview") == "sales-overview"
    assert validate_slug("a") == "a"
    s = "a" + "b" * 62 + "c"
    assert len(s) == 64 and validate_slug(s) == s


@pytest.mark.parametrize("bad", ["", "-a", "a-", "Sales", "a_b", "_lib", "a" * 65, None, 5, "a/b", "a b"])
def test_slug_invalid(bad):
    _status(validate_slug, bad)


def test_title():
    assert validate_title("  Sales  ") == "Sales"
    for bad in ["", "   ", "x" * 201, "a\x00b", None, 3]:
        _status(validate_title, bad)
    assert validate_title("x" * 200) == "x" * 200


def test_note():
    assert validate_note(None) is None
    assert validate_note("") is None
    assert validate_note("  hi ") == "hi"
    assert validate_note("x" * 2000) == "x" * 2000
    _status(validate_note, "x" * 2001)
    _status(validate_note, "a\x00")
    _status(validate_note, 4)
    assert "reason" in _status(validate_note, 4, "reason")


def test_require_int():
    assert require_int(3, "version") == 3
    for bad in [True, False, "3", 3.0, None]:
        assert "version" in _status(require_int, bad, "version")


def test_normalize_code_newlines():
    assert normalize_code("a\r\nb\rc\n") == "a\nb\nc\n"


def test_normalize_code_rejects():
    for bad in [None, 5, "", "  \n ", "a\x00b"]:
        _status(normalize_code, bad)


def test_normalize_code_byte_cap():
    assert len(normalize_code("a" * MAX_CODE_BYTES).encode()) == MAX_CODE_BYTES
    _status(normalize_code, "a" * (MAX_CODE_BYTES + 1))
    _status(normalize_code, "é" * 131073)
    assert normalize_code("é" * 131072)


def test_normalize_code_cap_after_normalization():
    # CRLF pairs: raw bytes over the cap, normalized bytes under it.
    code = "a\r\n" * (MAX_CODE_BYTES // 2)
    assert len(code.encode()) > MAX_CODE_BYTES
    assert len(normalize_code(code).encode()) <= MAX_CODE_BYTES


def test_validate_queries_basic():
    assert validate_queries([]) == []
    out = validate_queries([{"id": "rev", "sql": "SELECT 1;"}])
    assert out == [{"id": "rev", "sql": "SELECT 1", "refresh_minutes": 15}]
    out = validate_queries([{"id": "rev", "sql": "SELECT 1", "refresh_minutes": 5}])
    assert out[0]["refresh_minutes"] == 5


def _q(**kw):
    d = {"id": "q1", "sql": "SELECT 1"}
    d.update(kw)
    return d


@pytest.mark.parametrize("bad", [
    "x", {}, [_q(id=f"q{i}") for i in range(21)], ["x"],
    [_q(source="x")], [_q(params=1)], [{"id": "a"}], [{"sql": "SELECT 1"}],
    [_q(id="1abc")], [_q(id="Revenue")], [_q(id="a" * 49)], [_q(id=5)],
    [_q(), _q()],
    [_q(refresh_minutes=True)], [_q(refresh_minutes="15")], [_q(refresh_minutes=4)],
    [_q(refresh_minutes=1441)], [_q(refresh_minutes=15.0)],
    [_q(sql=5)], [_q(sql="SELECT 1\x00")],
    [_q(sql="DELETE FROM invoices")], [_q(sql="SELECT 1; SELECT 2")],
    [_q(sql="SELECT pg_read_file('x')")],
])
def test_validate_queries_rejects(bad):
    _status(validate_queries, bad)


def test_validate_queries_20_ok_and_guardrail_message():
    assert len(validate_queries([_q(id=f"q{i}") for i in range(20)])) == 20
    msg = _status(validate_queries, [_q(id="zap", sql="DELETE FROM invoices")])
    assert msg.startswith("query zap:") and "SELECT" in msg


LIB_HASH = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
A_HASH = "sha256-zv0N0Hv6Zw9+C4JbNy52ozAX+zm+quOk2wp0IZa7md8="
LIB_LINE = ('<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" '
            'integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>')


def _h(body):
    return "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()


def test_script_hashes_known_vector():
    assert script_hashes("<script>const a = 1;</script>") == [A_HASH]


def test_script_hashes_multiline_independent():
    body = "\nlet x = 1;\nfoo(x);\n"
    assert script_hashes(f"<html><script>{body}</script></html>") == [_h(body)]


def test_script_hashes_newline_variants_equal():
    doc = "<p>\n</p>\n<script>\nlet a = 1;\nlet b = 2;\n</script>\n"
    outs = {tuple(script_hashes(normalize_code(v))) for v in
            (doc, doc.replace("\n", "\r\n"), doc.replace("\n", "\r"))}
    assert len(outs) == 1


def test_script_hashes_src_without_integrity():
    assert script_hashes('<script src="/d/_lib/chart.js"></script>') == []


def test_script_hashes_canonical_library_line():
    assert script_hashes(LIB_LINE) == [LIB_HASH]


def test_script_hashes_library_plus_inline_order():
    inline = "<script>const a = 1;</script>"
    assert script_hashes(LIB_LINE + inline) == [LIB_HASH, A_HASH]
    assert script_hashes(inline + LIB_LINE) == [A_HASH, LIB_HASH]


@pytest.mark.parametrize("src", ["https://cdn.example.com/x.js", "//cdn.example.com/x.js", "lib.js"])
def test_script_hashes_foreign_src_ignored(src):
    assert script_hashes(f'<script src="{src}" integrity="sha256-AAAA"></script>') == []


def test_script_hashes_integrity_token_filter():
    code = '<script src="/d/_lib/x.js" integrity="sha384-abc sha512-def md5-zzz sha256-q?opt"></script>'
    assert script_hashes(code) == ["sha384-abc", "sha512-def"]


def test_script_hashes_inline_integrity_ignored():
    assert script_hashes('<script integrity="sha256-AAAA">const a = 1;</script>') == [A_HASH]


def test_script_hashes_empty_or_valueless_integrity():
    assert script_hashes('<script src="/d/_lib/x.js" integrity=""></script>') == []
    assert script_hashes('<script src="/d/_lib/x.js" integrity></script>') == []


def test_script_hashes_dedup_library():
    assert script_hashes(LIB_LINE + LIB_LINE) == [LIB_HASH]


def test_script_hashes_comments_ignored():
    assert script_hashes("<!-- <script>evil()</script> -->") == []
    assert script_hashes("<!-- " + LIB_LINE + " -->") == []


def test_script_hashes_case_insensitive_tags():
    assert script_hashes("<SCRIPT>x()</SCRIPT>") == [_h("x()")]
    assert script_hashes('<SCRIPT SRC="/d/_lib/x.js" INTEGRITY="sha256-AAAA"></SCRIPT>') == ["sha256-AAAA"]
    assert script_hashes('<script type="module">m()</script>') == [_h("m()")]


def test_script_hashes_entities_not_decoded():
    assert script_hashes("<script>a &amp;&amp; b</script>") == [_h("a &amp;&amp; b")]


def test_script_hashes_quoted_gt_in_attribute():
    assert script_hashes('<script data-x="a>b">y()</script>') == [_h("y()")]


def test_script_hashes_dedup_and_order():
    code = "<script>a()</script><script>b()</script><script>a()</script>"
    assert script_hashes(code) == [_h("a()"), _h("b()")]


def test_new_view_token():
    t1, t2 = new_view_token(), new_view_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", t1) and t1 != t2


# --- review CR-01 / WR-01 regressions ---------------------------------------
def test_script_hashes_reused_integrity_on_foreign_script_rejected():
    code = ('<script src="/d/_lib/x.js" integrity="sha256-EVIL"></script>'
            '<script src="https://evil.example/e.js" integrity="sha256-EVIL"></script>')
    with pytest.raises(DashboardError):
        script_hashes(code)


def test_script_hashes_duplicate_src_uses_first():
    code = '<script src="https://evil/x.js" src="/d/_lib/a.js" integrity="sha256-AAAA"></script>'
    assert script_hashes(code) == []


def test_script_hashes_base_tag_rejected():
    with pytest.raises(DashboardError):
        script_hashes('<base href="https://evil.com/"><script src="/d/_lib/a.js" integrity="sha384-BBBB"></script>')


def test_require_int_range_and_surrogate():
    from app.dashboards import require_int
    with pytest.raises(DashboardError):
        require_int(99999999999, "version")
    with pytest.raises(DashboardError):
        normalize_code("\ud800 x")
