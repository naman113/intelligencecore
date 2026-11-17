"""Static lint of the Phase 8 block in the fallback console (UI-09)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "app" / "dashboard.html").read_text(encoding="utf-8")
MARK = "Phase 8: dashboards and context approvals (UI-09)"
END = "// ── end Phase 8 ──"


def _block() -> str:
    start = HTML.index("// ── " + MARK)
    return HTML[start:HTML.index(END, start) + len(END)]


def test_html_sections_and_markers():
    assert HTML.count(MARK) == 2
    assert 'id="p8Dashboards"' in HTML and 'id="p8ContextApprovals"' in HTML
    assert 'id="p8DashList"' in HTML and 'id="p8CtxList"' in HTML and 'id="p8CtxMeta"' in HTML
    assert HTML.index('id="p8Dashboards"') > HTML.index('id="approvals"')


def test_block_content_and_refresh_order():
    b = _block()
    for needle in ("/api/dashboards", "/rollback", "context_change", "publish_dashboard",
                   "loadP8Dashboards", "p8Versions", "p8Rollback", "loadP8ContextApprovals"):
        assert needle in b
    assert HTML.index("_p8Refresh = refresh") < HTML.index("setInterval(refresh, 5000)")


def test_interpolation_allowlist():
    b = _block()
    for m in re.finditer(r"\$\{", b):
        assert re.match(r"\$\{(esc|ago|Number)\(", b[m.start():]), b[m.start():m.start() + 40]


def test_no_template_builds_a_url_and_no_esc_in_onclick():
    b = _block()
    for lit in re.findall(r"`[^`]*`", b):
        assert not ("/api/" in lit and "${" in lit)
    for oc in re.findall(r'onclick="[^"]*"', b):
        assert "esc(" not in oc


def test_forbidden_access_absent():
    b = _block()
    assert not re.search(r"\.code\b", b)
    assert "/versions/" not in b
    assert "/d/" not in b


def test_slug_fetch_only_in_p8versions():
    b = _block()
    pat = "api('/api/dashboards/' + encodeURIComponent(slug))"
    assert b.count(pat) == 1
    fn = b[b.index("async function p8Versions"):b.index("async function p8Rollback")]
    assert pat in fn


def test_rollback_confirms_and_explains_decided_by():
    b = _block()
    fn = b[b.index("async function p8Rollback"):b.index("async function loadP8ContextApprovals")]
    assert "confirm(" in fn and "decided_by" in fn and "People registry" in fn


def test_node_check(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    m = re.search(r"<script>(.*)</script>", HTML, re.S)
    assert m
    f = tmp_path / "console.js"
    f.write_text(m.group(1), encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_context_approvals_exact_count():
    b = _block()
    assert "kind=context_change" in b
    assert "/api/context/sources" in b
    assert "api('/api/actions?status=proposed')).filter" not in b


# ── Phase 12 (UI-11) ──
P12_START = "// ── Phase 12: skill View button (UI-11) ──"
P12_END = "// ── end Phase 12 (UI-11) ──"


def _p12_block() -> str:
    assert HTML.count(P12_START) == 1 and HTML.count(P12_END) == 1
    start = HTML.index(P12_START)
    return HTML[start:HTML.index(P12_END, start) + len(P12_END)]


def test_p12_block_placement():
    assert HTML.index(P12_START) > HTML.index(END)
    assert HTML.index(P12_END) < HTML.index("setInterval(refresh, 5000)")


def test_p12_fetch_only_in_view_function_and_text_only():
    b = _p12_block()
    assert b.count("api('/api/skills/' + encodeURIComponent(name))") == 1
    fn = b[b.index("async function p12ViewSkill"):b.index("function p12AddViewButtons")]
    assert "api('/api/skills/' + encodeURIComponent(name))" in fn
    assert "textContent" in fn
    assert "innerHTML" not in b and "onclick=" not in b
    assert not re.search(r"`[^`]*/api/[^`]*`", b)


def test_p12_wraps_loadskills_and_adds_buttons():
    b = _p12_block()
    assert "const _p12LoadSkills = loadSkills" in b
    assert "await _p12LoadSkills()" in b and "p12AddViewButtons()" in b
    assert "document.createElement('button')" in b
    assert "addEventListener('click'" in b
    assert "p12ViewSkill(name)" in b


def test_p12_list_poll_stays_body_free():
    start = HTML.index("async function loadSkills()")
    body = HTML[start:HTML.index("async function previewSkill", start)]
    assert "api('/api/skills')" in body and "/api/skills/'" not in body


# ── Phase 12 (CTX-09e) ──
def test_p12_maxtok_input_matches_ceiling():
    from app import llmclient
    m = re.search(r'<input id="cfg-maxtok-[^"]*" type="number" min="(\d+)" max="(\d+)"', HTML)
    assert m, "cfg-maxtok input not found"
    assert m.group(1) == "256"
    assert int(m.group(2)) == llmclient.MAX_TOKENS_CEILING
    assert "Max tokens (256–32000)" in HTML
    assert 'max="8192"' not in HTML
