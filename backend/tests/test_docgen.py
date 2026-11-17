"""Document generation — real rendering, pure (no DB, no network, no LLM).

Every test renders a real file into tmp_path with the same libraries the
tools use, then reopens it with fitz / python-docx / openpyxl and asserts on
what a reader would actually see. Style mirrors test_docconvert.py.

NOTE on ordering: test_font_fallback_renders_without_bundled_ttfs is defined
FIRST so that, when this file runs alone, reportlab's process-global font
registry is still empty and the Helvetica fallback path genuinely executes.
It leaves docgen._fonts = None so every later test re-registers the real
fonts.
"""

import pytest

from app import docgen
from app.docgen import (
    MAX_MD_CHARS,
    MAX_SHEETS,
    DocGenError,
    docx_from_markdown,
    markdown_file,
    pdf_from_markdown,
    xlsx_from_spec,
)


def _pdf_text(path) -> str:
    import fitz

    with fitz.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def _make_png(path, size=(40, 30), color=(200, 40, 40)):
    from PIL import Image

    Image.new("RGB", size, color).save(str(path))


# ── font fallback (must stay first — see module docstring) ───────────────


@pytest.fixture
def _empty_font_dir(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(docgen, "_FONT_DIR", empty)
    docgen._fonts = None
    yield
    # leave the cache cold so subsequent tests re-register the real TTFs
    docgen._fonts = None


def test_font_fallback_renders_without_bundled_ttfs(tmp_path, _empty_font_dir):
    import fitz

    p = tmp_path / "fallback.pdf"
    pdf_from_markdown("# Plain Heading\n\nBody text without fonts.", p,
                      title="Fallback")
    with fitz.open(str(p)) as doc:
        assert doc.page_count >= 1
        text = doc[0].get_text()
        used = {f[3] for page in doc for f in page.get_fonts()}
    assert "Plain Heading" in text
    assert "Body text without fonts." in text
    # The fallback must have ENGAGED, not just rendered: reportlab's font
    # registry is process-global, so without this assertion the test passes
    # vacuously whenever another test registered Inter first.
    assert not any("Inter" in f or "SpaceGrotesk" in f for f in used), used


# ── PDF ──────────────────────────────────────────────────────────────────

FULL_MD = """# Quarterly Review

Intro paragraph with **bold**, *italic* and `inline_code` spans.

## Numbers Section

1. first ordered item
2. second ordered item

### Detail Level

- top bullet one
  - nested bullet a
  - nested bullet b
- top bullet two

| Vendor | Amount |
|--------|--------|
| Acme   | 1200   |
| Bolt   | 300    |

```python
total = 1200 + 300
```

> Costs held flat quarter over quarter.

---

Closing body sentence.
"""


def test_full_feature_markdown_pdf(tmp_path):
    import fitz

    p = tmp_path / "report.pdf"
    pdf_from_markdown(FULL_MD, p, title="Quarterly Report",
                      subtitle="Finance — internal")
    with fitz.open(str(p)) as doc:
        assert doc.page_count >= 1
        first = doc[0].get_text()
        all_text = "\n".join(pg.get_text() for pg in doc)

    assert "Quarterly Report" in first          # title band
    assert "Finance — internal" in first        # subtitle
    assert "Page 1" in first                    # footer
    assert "Quarterly Review" in all_text       # h1
    assert "Numbers Section" in all_text        # h2
    assert "Detail Level" in all_text           # h3
    assert "nested bullet a" in all_text
    assert "second ordered item" in all_text
    assert "Acme" in all_text and "Vendor" in all_text     # table
    assert "total = 1200 + 300" in all_text                # fenced code
    assert "Costs held flat" in all_text                   # blockquote
    assert "Closing body sentence." in all_text


def test_200_row_table_spans_pages(tmp_path):
    import fitz

    rows = "\n".join(f"| r{i} | value {i} |" for i in range(200))
    md = "| id | val |\n|----|-----|\n" + rows + "\n"
    p = tmp_path / "long.pdf"
    pdf_from_markdown(md, p)
    with fitz.open(str(p)) as doc:
        assert doc.page_count >= 2
        all_text = "\n".join(pg.get_text() for pg in doc)
    assert "value 0" in all_text
    assert "value 199" in all_text


def test_embedded_image(tmp_path):
    import fitz

    _make_png(tmp_path / "x.png")
    p = tmp_path / "img.pdf"
    pdf_from_markdown("Before.\n\n![Cap](x.png)\n\nAfter.", p)
    with fitz.open(str(p)) as doc:
        images = [im for pg in doc for im in pg.get_images(full=True)]
        text = "\n".join(pg.get_text() for pg in doc)
    assert images
    assert "Cap" in text            # caption from alt text
    assert "image not available" not in text


def test_missing_image_renders_placeholder(tmp_path):
    p = tmp_path / "missing.pdf"
    pdf_from_markdown("![c](nope.png)", p)
    text = _pdf_text(p)
    assert "image not available" in text
    assert "nope.png" in text


def test_image_containment_traversal_and_url(tmp_path):
    import fitz

    # the pdf lives in a subdir; a real decoy sits one level up
    work = tmp_path / "work"
    work.mkdir()
    _make_png(tmp_path / "secret.png")
    assert (tmp_path / "secret.png").is_file()

    p = work / "contained.pdf"
    pdf_from_markdown("![c](../secret.png)\n\n![c](http://evil/x.png)", p)
    with fitz.open(str(p)) as doc:
        images = [im for pg in doc for im in pg.get_images(full=True)]
        text = "\n".join(pg.get_text() for pg in doc)
    assert images == []             # the decoy was NOT embedded
    assert text.count("image not available") == 2


def test_raw_html_and_ampersand_are_escaped_not_parsed(tmp_path):
    md = "Literal <b>raw</b> tags & an ampersand, plus <font> too."
    p = tmp_path / "escape.pdf"
    pdf_from_markdown(md, p)      # a paraparser crash would raise here
    text = _pdf_text(p)
    assert "<b>raw</b>" in text
    assert "& an ampersand" in text
    assert "<font>" in text


def test_oversize_markdown_raises(tmp_path):
    with pytest.raises(DocGenError) as ei:
        pdf_from_markdown("a" * (MAX_MD_CHARS + 1), tmp_path / "big.pdf")
    assert "split" in str(ei.value)


def test_empty_markdown_still_produces_a_page(tmp_path):
    import fitz

    p = tmp_path / "empty.pdf"
    pdf_from_markdown("", p)
    with fitz.open(str(p)) as doc:
        assert doc.page_count >= 1


def test_pdf_table_over_max_cols_raises(tmp_path):
    ncols = docgen.MAX_COLS + 1
    header = "|" + "h|" * ncols
    sep = "|" + "---|" * ncols
    row = "|" + "x|" * ncols
    with pytest.raises(DocGenError) as ei:
        pdf_from_markdown("\n".join([header, sep, row]), tmp_path / "wide.pdf")
    assert "create_xlsx" in str(ei.value)


# ── DOCX ─────────────────────────────────────────────────────────────────


def test_docx_headings_and_paragraphs(tmp_path):
    import docx

    p = tmp_path / "plain.docx"
    docx_from_markdown("# Report Heading\n\nBody paragraph here.", p,
                       title="My Title")
    d = docx.Document(str(p))
    h1 = [par for par in d.paragraphs if par.style.name == "Heading 1"]
    assert h1 and h1[0].text == "Report Heading"
    assert any(par.text == "Body paragraph here." for par in d.paragraphs)
    assert any(par.text == "My Title" for par in d.paragraphs)


def test_docx_round_trip(tmp_path):
    """Full round-trip incl. table + code shading — the exact input that once
    crashed _docx_shade (it received high-level _Cell/Paragraph wrappers whose
    oxml elements carry the get_or_add_* methods). No xfail: a docx with a
    table is the headline advertised feature and must render."""
    import docx

    md = ("# Report Heading\n\nBody paragraph here.\n\n"
          "| a | b |\n|---|---|\n| c1 | c2 |\n\n"
          "```\ncode line one\n```\n")
    p = tmp_path / "out.docx"
    docx_from_markdown(md, p, title="My Title")

    d = docx.Document(str(p))
    h1 = [par for par in d.paragraphs if par.style.name == "Heading 1"]
    assert h1 and h1[0].text == "Report Heading"
    assert any(par.text == "Body paragraph here." for par in d.paragraphs)

    assert d.tables, "table missing from docx"
    cells = [c.text for row in d.tables[0].rows for c in row.cells]
    assert "a" in cells and "b" in cells
    assert "c1" in cells and "c2" in cells

    code_runs = [run for par in d.paragraphs for run in par.runs
                 if run.font.name == "Consolas"]
    assert any(run.text == "code line one" for run in code_runs)


def test_docx_oversize_raises(tmp_path):
    with pytest.raises(DocGenError):
        docx_from_markdown("a" * (MAX_MD_CHARS + 1), tmp_path / "big.docx")


# ── XLSX ─────────────────────────────────────────────────────────────────


def test_xlsx_round_trip(tmp_path):
    import openpyxl

    spec = {"sheets": [
        {"name": "Summary",
         "headers": ["Item", "Amount"],
         "rows": [["Widgets", 1200.5], ["Bolts", 300]],
         "widths": [18, 12],
         "formats": {"Amount": "#,##0.00"}},
        {"name": "Notes",
         "headers": ["Note"],
         "rows": [["hello"]]},
    ]}
    p = tmp_path / "book.xlsx"
    xlsx_from_spec(spec, p)

    wb = openpyxl.load_workbook(str(p))
    assert wb.sheetnames == ["Summary", "Notes"]

    ws = wb["Summary"]
    assert ws["A1"].value == "Item" and ws["B1"].value == "Amount"
    assert ws["A2"].value == "Widgets" and ws["B2"].value == 1200.5
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref                    # e.g. A1:B3
    assert ws["B2"].number_format == "#,##0.00"  # formats hit the right column
    assert ws["B3"].number_format == "#,##0.00"
    assert ws["A2"].number_format == "General"   # ...and only that column
    assert ws.column_dimensions["A"].width == 18
    assert ws.column_dimensions["B"].width == 12

    assert wb["Notes"]["A1"].value == "Note"
    assert wb["Notes"]["A2"].value == "hello"


def test_xlsx_ragged_row_names_sheet_and_row(tmp_path):
    spec = {"sheets": [{"name": "Data", "headers": ["a", "b"],
                        "rows": [[1, 2], [3]]}]}
    with pytest.raises(DocGenError) as ei:
        xlsx_from_spec(spec, tmp_path / "ragged.xlsx")
    msg = str(ei.value)
    assert "Data" in msg
    assert "row 2" in msg
    assert "2 headers" in msg


def test_xlsx_missing_sheets_key_raises(tmp_path):
    with pytest.raises(DocGenError):
        xlsx_from_spec({"tabs": []}, tmp_path / "x.xlsx")
    with pytest.raises(DocGenError):
        xlsx_from_spec({"sheets": []}, tmp_path / "x.xlsx")


def test_xlsx_too_many_sheets_raises(tmp_path):
    spec = {"sheets": [{} for _ in range(MAX_SHEETS + 1)]}
    with pytest.raises(DocGenError) as ei:
        xlsx_from_spec(spec, tmp_path / "many.xlsx")
    assert str(MAX_SHEETS) in str(ei.value)


def test_xlsx_long_sheet_name_truncated_not_raised(tmp_path):
    import openpyxl

    long_name = "X" * 40
    spec = {"sheets": [{"name": long_name, "headers": ["a"], "rows": []}]}
    p = tmp_path / "long.xlsx"
    xlsx_from_spec(spec, p)
    wb = openpyxl.load_workbook(str(p))
    assert wb.sheetnames == [long_name[:31]]


def test_xlsx_duplicate_sheet_names_deduped(tmp_path):
    import openpyxl

    spec = {"sheets": [
        {"name": "Dup", "headers": ["a"], "rows": []},
        {"name": "Dup", "headers": ["a"], "rows": []},
    ]}
    p = tmp_path / "dup.xlsx"
    xlsx_from_spec(spec, p)
    wb = openpyxl.load_workbook(str(p))
    assert len(wb.sheetnames) == 2
    assert wb.sheetnames[0] == "Dup"
    assert wb.sheetnames[1] != "Dup"            # renamed, not clobbered


# ── Markdown ─────────────────────────────────────────────────────────────


def test_markdown_file_writes_utf8(tmp_path):
    p = tmp_path / "note.md"
    markdown_file("# Résumé — naïve ✓\n", p)
    assert p.read_bytes().decode("utf-8") == "# Résumé — naïve ✓\n"


def test_markdown_file_cap(tmp_path):
    with pytest.raises(DocGenError) as ei:
        markdown_file("a" * (MAX_MD_CHARS + 1), tmp_path / "big.md")
    assert "split" in str(ei.value)
