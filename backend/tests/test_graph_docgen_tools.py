"""Document-generation tools (agents/graph.py) — create_pdf / create_docx /
create_xlsx / create_markdown pins.

Contract under test: filenames resolve through the REAL _work_path (containment
errors surface as "ERROR: ..." strings, matching run_sql_to_csv); missing
extensions are APPENDED, never rejected; bad specs come back as "TOOL ERROR: ..."
strings (DocGenError text, never a raised exception); success strings say
"created /work/<fname> (<n> KB) — now deliver it with send_document"; every call
logs exactly once through _log_tool with content truncated (run_python style);
the four tools live in _tool_registry() and ONLY in the document agent's
superset; playbook tiers gain nothing by import.

Pure logic: sandbox.scratch_dir is pointed at tmp_path and _log_tool is stubbed,
but rendering is REAL (reportlab/mistune/openpyxl/python-docx are installed in
the testrunner), so the bytes on disk are asserted too."""

import json

from app import playbook_tools, whatsapp
from app.agents import graph

DOCGEN_TOOL_NAMES = ["create_pdf", "create_docx", "create_xlsx", "create_markdown"]


def _capture_log(monkeypatch, tmp_path):
    """Real _work_path resolving into tmp_path + a recording _log_tool stub."""
    monkeypatch.setattr(graph.sandbox, "scratch_dir", lambda run_id: tmp_path)
    calls = []

    async def fake_log(name, args, result, start, refused=False):
        calls.append({"name": name, "args": args, "result": result})

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    return calls


async def _call(tool_obj, **kwargs):
    fn = tool_obj.coroutine or tool_obj.func
    tok = graph.ctx_run_id.set(77)
    try:
        return await fn(**kwargs)
    finally:
        graph.ctx_run_id.reset(tok)


# ── happy paths ──────────────────────────────────────────────────────────

async def test_create_pdf_happy_path(monkeypatch, tmp_path):
    calls = _capture_log(monkeypatch, tmp_path)

    result = await _call(graph.create_pdf,
                         markdown="# Hi\n\nBody", filename="r.pdf", title="T")

    assert "created /work/r.pdf" in result
    assert "KB" in result
    assert "send_document" in result, \
        "success string must point the model at delivery"
    out = tmp_path / "r.pdf"
    assert out.is_file()
    assert out.read_bytes().startswith(b"%PDF"), "output must be a real PDF"
    assert [c["name"] for c in calls] == ["create_pdf"], \
        "exactly one _log_tool call, filed under the tool name"


async def test_create_markdown_writes_content_verbatim(monkeypatch, tmp_path):
    _capture_log(monkeypatch, tmp_path)
    content = "# Notes\n\n- one\n- two\n\nliteral <tags> & symbols\n"

    result = await _call(graph.create_markdown,
                         content=content, filename="notes.md")

    assert "created /work/notes.md" in result
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == content, \
        "markdown is a pass-through format — bytes in, bytes out"


async def test_create_docx_appends_missing_extension(monkeypatch, tmp_path):
    calls = _capture_log(monkeypatch, tmp_path)

    # Markdown includes a TABLE deliberately: the docstring advertises GFM
    # tables and a shading bug once broke every docx containing one — the
    # tool-level happy path must exercise the advertised feature.
    result = await _call(graph.create_docx,
                         markdown="# Report\n\nText.\n\n| a | b |\n|---|---|\n| 1 | 2 |",
                         filename="report")

    assert "report.docx" in result, \
        "result must name the corrected filename so the model can send it"
    assert not result.startswith(("ERROR:", "TOOL ERROR:")), \
        "a missing extension is autocorrected, never an error"
    out = tmp_path / "report.docx"
    assert out.is_file()
    assert out.read_bytes()[:2] == b"PK", "docx is a zip container"
    assert len(calls) == 1


# ── containment: the REAL _work_path is the gate ─────────────────────────

async def test_create_pdf_rejects_path_traversal(monkeypatch, tmp_path):
    calls = _capture_log(monkeypatch, tmp_path)

    result = await _call(graph.create_pdf,
                         markdown="# x", filename="../../evil.pdf",
                         title="", subtitle="")

    assert isinstance(result, str) and result.startswith("ERROR:"), \
        "containment failures return the run_sql_to_csv-style ERROR string"
    # nothing escaped the workspace
    assert not (tmp_path / "evil.pdf").exists()
    assert not (tmp_path.parent / "evil.pdf").exists()
    assert not (tmp_path.parent.parent / "evil.pdf").exists()
    assert list(tmp_path.iterdir()) == [], "no file may be created on refusal"
    assert len(calls) == 1, "the refused attempt still lands in the audit log"


# ── create_xlsx error surfaces ───────────────────────────────────────────

async def test_create_xlsx_invalid_json_is_tool_error(monkeypatch, tmp_path):
    _capture_log(monkeypatch, tmp_path)

    result = await _call(graph.create_xlsx,
                         spec_json="{not valid json", filename="d.xlsx")

    assert result.startswith("TOOL ERROR: spec_json is not valid JSON"), result
    assert list(tmp_path.iterdir()) == []


async def test_create_xlsx_ragged_row_names_the_sheet(monkeypatch, tmp_path):
    _capture_log(monkeypatch, tmp_path)
    spec = {"sheets": [{"name": "Data",
                        "headers": ["a", "b"],
                        "rows": [[1]]}]}          # 1 cell, 2 headers

    result = await _call(graph.create_xlsx,
                         spec_json=json.dumps(spec), filename="d.xlsx")

    assert result.startswith("TOOL ERROR:"), result
    assert "Data" in result, \
        "DocGenError text names the offending sheet so the model can fix it"


# ── registration pins ────────────────────────────────────────────────────

def test_docgen_tools_registered():
    registry = graph._tool_registry()
    for name in DOCGEN_TOOL_NAMES:
        assert name in registry, f"{name} missing from _tool_registry()"


def test_document_superset_is_exactly_the_contract_list():
    # get_document/search_documents joined after the TC-006 incident: a
    # document agent with no retrieval tools could not reach the stored
    # extraction it was asked to render, so it made one up.
    assert graph.AGENT_TOOL_SUPERSET["document"] == [
        "create_pdf", "create_docx", "create_xlsx", "create_markdown",
        "get_document", "search_documents",
        "run_python", "list_artifacts", "send_document",
        "use_skill", "skill_asset",   # Agent Skills (test_skills_tools.py)
    ]


def test_docgen_tools_not_leaked_into_other_agents():
    # EVERY non-document agent, not a hand-picked subset — a leak into e.g.
    # the data agent must fail this test too.
    for agent, tools in graph.AGENT_TOOL_SUPERSET.items():
        if agent == "document":
            continue
        assert not set(DOCGEN_TOOL_NAMES) & set(tools), \
            f"agent '{agent}' must not gain docgen tools"


def test_playbook_tiers_untouched_by_docgen():
    # graph is imported at module top — if wiring the docgen tools leaked
    # capabilities into the tier matrix, this catches it.
    for tier, caps in playbook_tools.TIER_CAPABILITIES.items():
        assert not caps & set(DOCGEN_TOOL_NAMES), \
            f"tier '{tier}' must not gain docgen capabilities"


# ── MIMETYPES pins (send_document must be able to deliver the outputs) ──

def test_mimetypes_cover_docgen_outputs():
    # ALL docgen output types, not just the newly-added ones — deleting .pdf
    # from the map would silently break send_document for the main format.
    assert whatsapp.MIMETYPES[".pdf"] == "application/pdf"
    assert whatsapp.MIMETYPES[".docx"].startswith("application/vnd.openxmlformats")
    assert whatsapp.MIMETYPES[".xlsx"].startswith("application/vnd.openxmlformats")
    assert whatsapp.MIMETYPES[".md"] == "text/markdown"
    assert whatsapp.MIMETYPES[".jpg"] == "image/jpeg"
    assert whatsapp.MIMETYPES[".jpeg"] == "image/jpeg"


# ── audit-log truncation (run_python pattern) ────────────────────────────

async def test_log_tool_truncates_large_content(monkeypatch, tmp_path):
    calls = _capture_log(monkeypatch, tmp_path)
    big = "x" * 5000

    await _call(graph.create_markdown, content=big, filename="big.md")

    assert len(calls) == 1, "exactly one _log_tool call per tool call"
    args = calls[0]["args"]
    for key, val in args.items():
        if isinstance(val, str):
            assert len(val) <= 520, \
                (f"logged arg {key!r} is {len(val)} chars — content must be "
                 "truncated to ~500 before logging (run_python pattern)")
    # the file itself is NOT truncated
    assert (tmp_path / "big.md").read_text(encoding="utf-8") == big
