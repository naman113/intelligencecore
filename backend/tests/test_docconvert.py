"""Document conversion — pure (no DB, no network, no LLM).

Fixtures build real xlsx/docx files in tmp_path with the same libraries the
converter uses; csv/txt/xls paths are exercised with raw bytes.
"""

import pytest

from app import docconvert
from app.docconvert import DocConvertError, MAX_TEXT_CHARS, to_text


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_xlsx_converts_sheets_and_cells(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Invoices"
    ws1.append(["number", "amount"])
    ws1.append(["INV-2041", 84500])
    ws2 = wb.create_sheet("Notes")
    ws2.append(["due", "2026-09-15"])
    p = tmp_path / "book.xlsx"
    wb.save(p)

    out = to_text(p)
    assert "--- sheet: Invoices ---" in out
    assert "INV-2041\t84500" in out
    assert "--- sheet: Notes ---" in out
    assert "due\t2026-09-15" in out


def test_xlsx_row_cap_emits_omission_marker(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for i in range(520):
        ws.append([f"row-{i}"])
    p = tmp_path / "big.xlsx"
    wb.save(p)

    out = to_text(p)
    assert "[... 20 more rows omitted]" in out
    assert "row-519" not in out


def test_docx_paragraphs_and_tables(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Payment terms: net 30.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "vendor"
    table.rows[0].cells[1].text = "Acme LLC"
    p = tmp_path / "agreement.docx"
    doc.save(p)

    out = to_text(p)
    assert "Payment terms: net 30." in out
    assert "--- table ---" in out
    assert "vendor\tAcme LLC" in out


def test_csv_utf8_and_latin1_fallback(tmp_path):
    utf8 = _write(tmp_path, "a.csv", "name,city\nPriya,Mumbai\n".encode())
    assert "Priya,Mumbai" in to_text(utf8)

    latin = _write(tmp_path, "b.csv", b"caf\xe9,1\n")   # 0xE9 is not valid utf-8
    out = to_text(latin)
    assert "caf" in out and ",1" in out                  # decoded, no raise


def test_txt_converts(tmp_path):
    p = _write(tmp_path, "note.txt", b"pay before Sep 15")
    assert to_text(p) == "pay before Sep 15"


def test_data_bytes_path_needs_no_file_on_disk(tmp_path):
    p = tmp_path / "virtual.csv"                         # never written
    assert "x,y" in to_text(p, data=b"x,y\n1,2\n")


def test_corrupt_xls_raises_docconverterror(tmp_path):
    p = _write(tmp_path, "broken.xls", b"not an xls file at all")
    with pytest.raises(DocConvertError) as ei:
        to_text(p)
    assert "broken.xls" in str(ei.value)


def test_legacy_doc_always_raises(tmp_path):
    p = _write(tmp_path, "old.doc", b"\xd0\xcf\x11\xe0 whatever")
    with pytest.raises(DocConvertError):
        to_text(p)


def test_unknown_suffix_raises(tmp_path):
    p = _write(tmp_path, "archive.zip", b"PK\x03\x04")
    with pytest.raises(DocConvertError):
        to_text(p)


def test_truncation_cap(tmp_path):
    p = _write(tmp_path, "huge.txt", b"a" * (MAX_TEXT_CHARS + 1000))
    out = to_text(p)
    assert out.endswith("\n[TRUNCATED]")
    assert len(out) <= MAX_TEXT_CHARS + len("\n[TRUNCATED]")


def test_supported_convert_set_is_the_contract():
    assert docconvert.SUPPORTED_CONVERT == {".xlsx", ".docx", ".csv", ".txt", ".xls"}
