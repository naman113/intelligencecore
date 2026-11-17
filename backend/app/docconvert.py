"""Office/text attachment → plain text, for document extraction.

Pure conversion seam: no DB, no LLM, no app imports, so it stays unit-testable
and docintel remains the only intended caller. The heavy libraries (openpyxl,
python-docx, xlrd) are imported lazily inside the per-format helpers, so
importing this module never pulls them in and a missing library degrades to a
readable DocConvertError instead of an import-time crash.
"""

import io
from pathlib import Path

SUPPORTED_CONVERT = {".xlsx", ".docx", ".csv", ".txt", ".xls"}
MAX_TEXT_CHARS = 150_000
MAX_ROWS_PER_SHEET = 500


class DocConvertError(ValueError):
    """Any conversion failure — unsupported type, corrupt file, missing lib."""


def _bytes(path: Path, data: bytes | None) -> bytes:
    return data if data is not None else Path(path).read_bytes()


def _truncate(text: str) -> str:
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + "\n[TRUNCATED]"
    return text


def _rows_to_tsv(title: str, rows: list[list], omitted: int | None) -> str:
    """omitted: None = complete; -1 = truncated, count unknown; N = N rows cut."""
    lines = [f"--- sheet: {title} ---"]
    for row in rows:
        lines.append("\t".join("" if v is None else str(v) for v in row))
    if omitted == -1:
        lines.append("[... more rows omitted]")
    elif omitted:
        lines.append(f"[... {omitted} more rows omitted]")
    return "\n".join(lines)


def _xlsx_text(raw: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            # Stop ITERATING at the cap — a million-row dump must cost seconds,
            # not minutes; the omitted count comes from the dimension record.
            rows, truncated = [], False
            for row in ws.iter_rows(values_only=True):
                if len(rows) >= MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                rows.append(list(row))
            omitted = None
            if truncated:
                total = ws.max_row       # may be None/absent in read-only mode
                omitted = (total - len(rows)) if total and total > len(rows) else -1
            parts.append(_rows_to_tsv(ws.title, rows, omitted))
        return "\n".join(parts)
    finally:
        wb.close()   # read_only mode keeps a file handle open


def _xls_text(raw: bytes) -> str:
    import xlrd

    wb = xlrd.open_workbook(file_contents=raw)
    parts = []
    for ws in wb.sheets():
        rows = [ws.row_values(i) for i in range(min(ws.nrows, MAX_ROWS_PER_SHEET))]
        omitted = ws.nrows - len(rows) if ws.nrows > len(rows) else None
        parts.append(_rows_to_tsv(ws.name, rows, omitted))
    return "\n".join(parts)


def _docx_text(raw: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        parts.append("--- table ---")
        for row in table.rows[:MAX_ROWS_PER_SHEET]:
            parts.append("\t".join(c.text for c in row.cells))
        if len(table.rows) > MAX_ROWS_PER_SHEET:
            parts.append(f"[... {len(table.rows) - MAX_ROWS_PER_SHEET} more rows omitted]")
    return "\n".join(parts)


def _plain_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def to_text(path: Path, data: bytes | None = None) -> str:
    """Convert one office/text file to plain text for the extraction prompt.

    `data` may carry the bytes in-memory (no disk round-trip); when None the
    file at `path` is read. Raises DocConvertError on anything that can't be
    converted — including `.doc`, which is never attempted (callers surface a
    polite "please send .docx or PDF" instead).
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".doc":
        raise DocConvertError(
            f"legacy .doc is not convertible ({Path(path).name}) — ask for .docx or PDF")
    if suffix not in SUPPORTED_CONVERT:
        raise DocConvertError(f"unsupported file type {suffix!r} ({Path(path).name})")
    try:
        raw = _bytes(path, data)
        if suffix == ".xlsx":
            text = _xlsx_text(raw)
        elif suffix == ".xls":
            text = _xls_text(raw)
        elif suffix == ".docx":
            text = _docx_text(raw)
        else:                               # .csv / .txt — already text
            text = _plain_text(raw)
    except DocConvertError:
        raise
    except Exception as exc:
        raise DocConvertError(f"could not convert {Path(path).name}: {exc}") from exc
    return _truncate(text)
