"""Document generation — house-styled pdf/docx/xlsx/md from model-written content.

The other half of docconvert.py: that module turns attachments INTO text, this
one turns text into deliverables. The model supplies CONTENT ONLY (markdown, or
a table spec for workbooks); every visual decision — fonts, palette, table
chrome, page furniture — is made HERE, deterministically, so two reports made a
month apart look like they came from the same shop. That is the entire reason
this module exists: hand-written reportlab code in the sandbox produced a
different-looking document every run.

Doctrine (same as docconvert): pure generation, no DB/LLM/app imports, heavy
libraries imported lazily, every failure raised as DocGenError with a message
the MODEL can act on ("row 14 has 4 cells but there are 3 headers"), because
these messages become tool results, not stack traces.

House style: light pages, dark-navy chrome. SpaceGrotesk for display, Inter for
body (TTFs in assets/fonts/, registered once, Helvetica fallback if missing —
styling must never cost a deliverable). Word output carries the same structure
by font NAME only: python-docx cannot embed fonts, so .docx renders with the
reader's substitutes. Known limitation, accepted.
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger("docgen")


class DocGenError(ValueError):
    """Any generation failure — bad spec, oversized input, unusable image."""


MAX_MD_CHARS = 200_000          # ~50 pages; past this, split into documents
MAX_ROWS_PER_SHEET = 50_000
MAX_SHEETS = 20
MAX_COLS = 100
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # per image; the connector caps sends at 25MB
MAX_LIST_DEPTH = 2

# ── palette ──────────────────────────────────────────────────────────────
NAVY = "#0E1420"      # header band, table headers, headings
INK = "#1A2233"       # body text
BLUE = "#2B7FD4"      # accent, links, quote bar
GOLD = "#D4952B"      # accent gradient end
ZEBRA = "#F4F6FA"     # alternating table rows
CODE_BG = "#F0F2F6"
CODE_FG = "#1E2D42"
GRAY = "#6B7280"      # captions, footer

_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_fonts_lock = threading.Lock()
_fonts: dict | None = None


# ── parsing ──────────────────────────────────────────────────────────────

def _parse(md: str) -> list[dict]:
    if len(md) > MAX_MD_CHARS:
        raise DocGenError(
            f"markdown too large ({len(md)} chars, max {MAX_MD_CHARS}) — "
            "split the content into multiple documents")
    import mistune
    parser = mistune.create_markdown(renderer=None,
                                     plugins=["table", "strikethrough"])
    tokens = parser(md)
    return tokens if isinstance(tokens, list) else []


def _plain(children) -> str:
    """Inline tokens → plain text (for table sizing, docx cells, alt text)."""
    out = []
    for t in children or []:
        if not isinstance(t, dict):
            continue
        if t.get("raw"):
            out.append(str(t["raw"]))
        if t.get("children"):
            out.append(_plain(t["children"]))
        if t.get("type") in ("linebreak", "softbreak"):
            out.append(" ")
    return "".join(out)


# ── fonts (process-global in reportlab: register once, thread-safe) ──────

def _ensure_fonts() -> dict:
    global _fonts
    with _fonts_lock:
        if _fonts is not None:
            return _fonts
        mapping = {"body": "Helvetica", "bold": "Helvetica-Bold",
                   "light": "Helvetica", "display": "Helvetica-Bold",
                   "mono": "Courier"}
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            from reportlab.pdfbase.ttfonts import TTFont

            files = {"Inter": "Inter-Regular.ttf",
                     "Inter-Bold": "Inter-Bold.ttf",
                     "Inter-Light": "Inter-Light.ttf",
                     "SpaceGrotesk-Bold": "SpaceGrotesk-Bold.ttf"}
            registered = set(pdfmetrics.getRegisteredFontNames())
            for name, fn in files.items():
                # File check BEFORE the registered-skip: reportlab's registry
                # is process-global, so an already-registered name must not
                # mask a missing font dir — the fallback decision has to be
                # reproducible whatever rendered earlier in this process.
                p = _FONT_DIR / fn
                if not p.is_file():
                    raise FileNotFoundError(fn)
                if name in registered:
                    continue
                pdfmetrics.registerFont(TTFont(name, str(p)))
            # <b>/<i> inside Paragraphs resolve through the family. Inter has
            # no italic cut bundled; italic renders upright — acceptable.
            registerFontFamily("Inter", normal="Inter", bold="Inter-Bold",
                               italic="Inter", boldItalic="Inter-Bold")
            mapping = {"body": "Inter", "bold": "Inter-Bold",
                       "light": "Inter-Light", "display": "SpaceGrotesk-Bold",
                       "mono": "Courier"}
        except Exception:
            log.debug("font registration failed — Helvetica fallback",
                      exc_info=True)
        _fonts = mapping
        return mapping


# ── shared: image containment ────────────────────────────────────────────

def _resolve_image(src: str, base_dir: Path) -> Path | None:
    """Bare filenames under base_dir only — the same rule as graph._work_path.

    URLs are never fetched (the model's content includes text from untrusted
    documents; an image ref must not become a network request or a file read
    outside the workspace). Anything unusable renders as a placeholder."""
    try:
        src = (src or "").strip()
        if not src or "://" in src:
            return None
        if Path(src).name != src:            # separators / traversal / drives
            return None
        p = (base_dir / src).resolve()
        if p.parent != base_dir.resolve() or not p.is_file():
            return None
        if p.stat().st_size > MAX_IMAGE_BYTES:
            return None
        return p
    except Exception:
        return None


# ── PDF ──────────────────────────────────────────────────────────────────

def _pdf_styles(fonts: dict) -> dict:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle

    def st(name, **kw):
        return ParagraphStyle(name, **kw)

    return {
        "h1": st("h1", fontName=fonts["display"], fontSize=20, leading=24,
                 textColor=HexColor(NAVY), spaceBefore=14, spaceAfter=6,
                 keepWithNext=1),
        "h2": st("h2", fontName=fonts["display"], fontSize=15, leading=19,
                 textColor=HexColor(NAVY), spaceBefore=12, spaceAfter=5,
                 keepWithNext=1),
        "h3": st("h3", fontName=fonts["bold"], fontSize=12, leading=15,
                 textColor=HexColor(NAVY), spaceBefore=10, spaceAfter=4,
                 keepWithNext=1),
        "body": st("body", fontName=fonts["body"], fontSize=10, leading=14.5,
                   textColor=HexColor(INK), spaceAfter=6),
        "bullet": st("bullet", fontName=fonts["body"], fontSize=10,
                     leading=14.5, textColor=HexColor(INK), spaceAfter=3),
        "cell": st("cell", fontName=fonts["body"], fontSize=8.5, leading=11,
                   textColor=HexColor(INK)),
        "cell_head": st("cell_head", fontName=fonts["bold"], fontSize=8.5,
                        leading=11, textColor=HexColor("#FFFFFF")),
        "code": st("code", fontName=fonts["mono"], fontSize=8.5, leading=11.5,
                   textColor=HexColor(CODE_FG)),
        "quote": st("quote", fontName=fonts["body"], fontSize=10, leading=14,
                    textColor=HexColor(GRAY), leftIndent=4),
        "caption": st("caption", fontName=fonts["light"], fontSize=8.5,
                      leading=11, textColor=HexColor(GRAY), alignment=1,
                      spaceAfter=8),
        "missing": st("missing", fontName=fonts["light"], fontSize=9,
                      leading=12, textColor=HexColor(GRAY), spaceAfter=6),
    }


def _inline(children, fonts: dict) -> str:
    """Inline tokens → reportlab intra-paragraph markup. Every piece of model
    text is XML-escaped FIRST — a literal '<' in content must never reach the
    paragraph parser."""
    out = []
    for t in children or []:
        if not isinstance(t, dict):
            continue
        typ = t.get("type", "")
        if typ == "text":
            out.append(escape(str(t.get("raw", ""))))
        elif typ == "strong":
            out.append(f"<b>{_inline(t.get('children'), fonts)}</b>")
        elif typ == "emphasis":
            out.append(f"<i>{_inline(t.get('children'), fonts)}</i>")
        elif typ == "codespan":
            out.append(f'<font face="{fonts["mono"]}" backColor="{CODE_BG}">'
                       f'{escape(str(t.get("raw", "")))}</font>')
        elif typ == "strikethrough":
            out.append(f"<strike>{_inline(t.get('children'), fonts)}</strike>")
        elif typ == "link":
            url = escape(str((t.get("attrs") or {}).get("url", "")),
                         {'"': "&quot;"})
            body = _inline(t.get("children"), fonts) or url
            out.append(f'<link href="{url}" color="{BLUE}"><u>{body}</u></link>')
        elif typ in ("linebreak", "softbreak"):
            out.append("<br/>" if typ == "linebreak" else " ")
        elif typ == "image":
            # inline images degrade to their alt text; block images are
            # handled by the block walker
            out.append(escape(_plain(t.get("children"))))
        elif t.get("raw"):
            out.append(escape(str(t["raw"])))
        elif t.get("children"):
            out.append(_inline(t.get("children"), fonts))
    return "".join(out)


def _accent_line(width, height=2.2):
    """The blue→gold signature line, approximated in segments."""
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.lib.colors import HexColor

    d = Drawing(width, height)
    b, g = HexColor(BLUE), HexColor(GOLD)
    n = 24
    seg = width / n
    for i in range(n):
        f = i / (n - 1)
        c = HexColor(
            "#%02X%02X%02X" % (
                int(b.red * 255 + (g.red - b.red) * 255 * f),
                int(b.green * 255 + (g.green - b.green) * 255 * f),
                int(b.blue * 255 + (g.blue - b.blue) * 255 * f)))
        d.add(Rect(i * seg, 0, seg + 0.5, height, fillColor=c, strokeColor=None))
    return d


def _pdf_table(tok: dict, styles: dict, fonts: dict, avail_w: float):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import LongTable, Paragraph, TableStyle

    head_cells, body_rows = [], []
    for part in tok.get("children") or []:
        if part.get("type") == "table_head":
            head_cells = part.get("children") or []
        elif part.get("type") == "table_body":
            for row in part.get("children") or []:
                body_rows.append(row.get("children") or [])
    # List form, not star-unpacking: a header-only table (valid GFM, and a
    # model abandoning a table mid-write emits exactly this) has no body rows,
    # and max(int) raises TypeError.
    ncols = max([len(head_cells), *(len(r) for r in body_rows)])
    if ncols == 0:
        return None
    if ncols > MAX_COLS:
        raise DocGenError(f"table has {ncols} columns (max {MAX_COLS}) — "
                          "use create_xlsx for wide data")

    small = ncols > 8
    cs, hs = styles["cell"].clone("c2"), styles["cell_head"].clone("h2c")
    if small:
        cs.fontSize, cs.leading = 7.5, 9.5
        hs.fontSize, hs.leading = 7.5, 9.5

    def cells(row, style):
        got = [Paragraph(_inline(c.get("children"), fonts), style) for c in row]
        return got + [Paragraph("", style)] * (ncols - len(got))

    data = [cells(head_cells, hs)] if head_cells else []
    data += [cells(r, cs) for r in body_rows]
    if not data:
        return None

    # Column widths from relative plain-text weight, floored so a short
    # column never collapses.
    weights = []
    for i in range(ncols):
        w = max((len(_plain(r[i].get("children"))) if i < len(r) else 0)
                for r in ([head_cells] if head_cells else []) + body_rows)
        weights.append(max(w, 4))
    total = sum(weights)
    widths = [max(avail_w * w / total, avail_w * 0.05) for w in weights]
    scale = avail_w / sum(widths)
    widths = [w * scale for w in widths]

    t = LongTable(data, colWidths=widths,
                  repeatRows=1 if head_cells else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, HexColor("#D6DAE2")),
    ]
    body_start = 0
    if head_cells:
        style += [("BACKGROUND", (0, 0), (-1, 0), HexColor(NAVY))]
        body_start = 1
    for i in range(body_start, len(data)):
        if (i - body_start) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), HexColor(ZEBRA)))
    t.setStyle(TableStyle(style))
    return t


def _flowables(tokens, styles, fonts, base_dir: Path, avail_w: float) -> list:
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (Image as RLImage, Paragraph, Preformatted,
                                    Spacer, Table, TableStyle)

    flow: list = []

    def walk_list(tok, depth, ordered):
        n = 0
        for item in tok.get("children") or []:
            n += 1
            marker = (f"{n}." if ordered else ("•" if depth == 0 else "◦"))
            first = True
            for blk in item.get("children") or []:
                btyp = blk.get("type", "")
                if btyp in ("block_text", "paragraph"):
                    s = styles["bullet"].clone(f"li{depth}")
                    s.leftIndent = 14 + depth * 14
                    s.bulletIndent = 2 + depth * 14
                    text = _inline(blk.get("children"), fonts)
                    if first:
                        flow.append(Paragraph(text, s, bulletText=marker))
                        first = False
                    else:
                        flow.append(Paragraph(text, s))
                elif btyp == "list":
                    walk_list(blk, min(depth + 1, MAX_LIST_DEPTH - 1),
                              bool((blk.get("attrs") or {}).get("ordered")))
                else:
                    # Code blocks, quotes, tables nested in a list item:
                    # render them un-indented rather than silently dropping
                    # content from the deliverable.
                    flow.extend(_flowables([blk], styles, fonts, base_dir,
                                           avail_w))

    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        typ = tok.get("type", "")
        if typ == "heading":
            level = min(int((tok.get("attrs") or {}).get("level", 1)), 3)
            style = styles[f"h{level}"]
            flow.append(Paragraph(_inline(tok.get("children"), fonts), style))
            if level == 1:
                flow.append(_accent_line(avail_w * 0.28))
                flow.append(Spacer(0, 6))
        elif typ == "paragraph":
            kids = tok.get("children") or []
            # A paragraph that is exactly one image renders as a block image.
            if len(kids) == 1 and kids[0].get("type") == "image":
                img = kids[0]
                src = str((img.get("attrs") or {}).get("url", ""))
                alt = _plain(img.get("children"))
                p = _resolve_image(src, base_dir)
                if p is None:
                    flow.append(Paragraph(
                        f"<i>[image not available: {escape(src or 'unnamed')}]</i>",
                        styles["missing"]))
                else:
                    try:
                        from PIL import Image as PILImage
                        w0, h0 = PILImage.open(p).size
                        w_pt = w0 * 72.0 / 96.0
                        w_draw = min(w_pt, avail_w)
                        h_draw = h0 * 72.0 / 96.0 * (w_draw / w_pt)
                        # Clamp height too: a tall matplotlib chart otherwise
                        # exceeds the frame and doc.build raises LayoutError
                        # AFTER a partial file is written.
                        max_h = 620.0        # < the ~727pt frame, with slack
                        if h_draw > max_h:
                            w_draw *= max_h / h_draw
                            h_draw = max_h
                        flow.append(RLImage(str(p), width=w_draw, height=h_draw,
                                            hAlign="CENTER"))
                        if alt:
                            flow.append(Paragraph(escape(alt), styles["caption"]))
                    except Exception:
                        flow.append(Paragraph(
                            f"<i>[image not available: {escape(src)}]</i>",
                            styles["missing"]))
                continue
            flow.append(Paragraph(_inline(kids, fonts), styles["body"]))
        elif typ == "list":
            walk_list(tok, 0, bool((tok.get("attrs") or {}).get("ordered")))
            flow.append(Spacer(0, 4))
        elif typ == "table":
            t = _pdf_table(tok, styles, fonts, avail_w)
            if t is not None:
                flow += [Spacer(0, 4), t, Spacer(0, 8)]
        elif typ == "block_code":
            code = Preformatted(str(tok.get("raw", "")).rstrip("\n"),
                                styles["code"])
            box = Table([[code]], colWidths=[avail_w])
            box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(CODE_BG)),
                ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#D6DAE2")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            flow += [box, Spacer(0, 6)]
        elif typ == "block_quote":
            inner = _flowables(tok.get("children") or [], styles, fonts,
                               base_dir, avail_w - 20)
            if inner:
                bar = Table([[None, inner]], colWidths=[3, avail_w - 23])
                bar.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (0, -1), HexColor(BLUE)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (1, 0), (1, -1), 10),
                ]))
                flow += [bar, Spacer(0, 6)]
        elif typ == "thematic_break":
            flow += [Spacer(0, 8), _accent_line(avail_w), Spacer(0, 8)]
        elif typ == "blank_line":
            continue
        else:
            raw = tok.get("raw") or _plain(tok.get("children"))
            if raw and str(raw).strip():
                flow.append(Paragraph(escape(str(raw)), styles["body"]))
    return flow


def pdf_from_markdown(md: str, path: Path, *, title: str | None = None,
                      subtitle: str | None = None) -> None:
    tokens = _parse(md)
    fonts = _ensure_fonts()
    styles = _pdf_styles(fonts)

    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate,
                                    PageTemplate)

    page_w, page_h = A4
    margin = 18 * mm
    avail_w = page_w - 2 * margin
    band_h = (30 * mm) if title else 0
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y")

    def footer(canvas):
        canvas.saveState()
        canvas.setFillColor(HexColor(GRAY))
        canvas.setFont(fonts["light"], 7.5)
        canvas.drawString(margin, 10 * mm, stamp)
        canvas.drawRightString(page_w - margin, 10 * mm,
                               f"Page {canvas.getPageNumber()}")
        canvas.setFillColor(HexColor(BLUE))
        canvas.rect(margin, 13 * mm, avail_w, 0.6, stroke=0, fill=1)
        canvas.restoreState()

    def first_page(canvas, doc):
        canvas.saveState()
        if title:
            canvas.setFillColor(HexColor(NAVY))
            canvas.rect(0, page_h - band_h, page_w, band_h, stroke=0, fill=1)
            canvas.setFillColor(HexColor("#FFFFFF"))
            canvas.setFont(fonts["display"], 19)
            canvas.drawString(margin, page_h - band_h + 12 * mm, title[:90])
            canvas.setFillColor(HexColor("#B8C2D4"))
            canvas.setFont(fonts["light"], 9.5)
            sub = subtitle[:140] if subtitle else stamp
            canvas.drawString(margin, page_h - band_h + 6 * mm, sub)
            # accent line under the band
            _accent_line(page_w).drawOn(canvas, 0, page_h - band_h - 2.2)
        canvas.restoreState()
        footer(canvas)

    def later_page(canvas, doc):
        footer(canvas)

    top_first = band_h + 8 * mm if title else margin
    frame_first = Frame(margin, 18 * mm, avail_w,
                        page_h - top_first - 18 * mm, id="f1")
    frame_later = Frame(margin, 18 * mm, avail_w,
                        page_h - margin - 18 * mm, id="f2")

    doc = BaseDocTemplate(str(path), pagesize=A4, title=title or path.stem,
                          leftMargin=margin, rightMargin=margin)
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[frame_first], onPage=first_page),
        PageTemplate(id="later", frames=[frame_later], onPage=later_page),
    ])

    story = [NextPageTemplate("later")]
    story += _flowables(tokens, styles, fonts, path.parent, avail_w)
    if not story[1:]:
        story.append(_flowables(
            [{"type": "paragraph",
              "children": [{"type": "text", "raw": " "}]}],
            styles, fonts, path.parent, avail_w)[0])
    doc.build(story)


# ── DOCX ─────────────────────────────────────────────────────────────────

def _docx_shade(element, hex_color: str) -> None:
    """Shade a table cell or paragraph. python-docx exposes no shading API, so
    this goes to the underlying oxml element: _Cell wraps `._tc` (w:tc),
    Paragraph wraps `._p` (w:p) — the get_or_add_* methods live THERE, not on
    the high-level wrappers."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color.lstrip("#"))
    el = getattr(element, "_tc", None) or getattr(element, "_p", None)
    if el is None:
        return
    pr = el.get_or_add_tcPr() if hasattr(el, "get_or_add_tcPr") else el.get_or_add_pPr()
    pr.append(shd)


def _docx_inline(par, children, *, bold=False, italic=False, strike=False,
                 mono=False, color=None) -> None:
    from docx.shared import Pt, RGBColor
    for t in children or []:
        if not isinstance(t, dict):
            continue
        typ = t.get("type", "")
        if typ == "text":
            run = par.add_run(str(t.get("raw", "")))
            run.bold, run.italic = bold, italic
            run.font.strike = strike
            if mono:
                run.font.name = "Consolas"
                run.font.size = Pt(9)
            if color:
                run.font.color.rgb = RGBColor.from_string(color.lstrip("#"))
        elif typ == "strong":
            _docx_inline(par, t.get("children"), bold=True, italic=italic,
                         strike=strike, mono=mono, color=color)
        elif typ == "emphasis":
            _docx_inline(par, t.get("children"), bold=bold, italic=True,
                         strike=strike, mono=mono, color=color)
        elif typ == "strikethrough":
            _docx_inline(par, t.get("children"), bold=bold, italic=italic,
                         strike=True, mono=mono, color=color)
        elif typ == "codespan":
            run = par.add_run(str(t.get("raw", "")))
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        elif typ == "link":
            url = str((t.get("attrs") or {}).get("url", ""))
            text = _plain(t.get("children")) or url
            run = par.add_run(text if text == url else f"{text} ({url})")
            run.font.color.rgb = RGBColor.from_string(BLUE.lstrip("#"))
            run.underline = True
        elif typ in ("linebreak", "softbreak"):
            par.add_run("\n" if typ == "linebreak" else " ")
        elif typ == "image":
            alt = _plain(t.get("children"))
            if alt:
                par.add_run(alt).italic = True
        elif t.get("children"):
            _docx_inline(par, t.get("children"), bold=bold, italic=italic,
                         strike=strike, mono=mono, color=color)


def _docx_blocks(doc, tokens, base_dir: Path, list_depth: int = 0) -> None:
    from docx.shared import Inches, Pt, RGBColor

    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        typ = tok.get("type", "")
        if typ == "heading":
            level = min(int((tok.get("attrs") or {}).get("level", 1)), 3)
            par = doc.add_heading(level=level)
            _docx_inline(par, tok.get("children"), color=NAVY)
            for run in par.runs:
                run.font.name = "Space Grotesk"
        elif typ == "paragraph":
            kids = tok.get("children") or []
            if len(kids) == 1 and kids[0].get("type") == "image":
                src = str((kids[0].get("attrs") or {}).get("url", ""))
                p = _resolve_image(src, base_dir)
                if p is None:
                    par = doc.add_paragraph()
                    par.add_run(f"[image not available: {src}]").italic = True
                else:
                    try:
                        doc.add_picture(str(p), width=Inches(6.0))
                    except Exception:
                        doc.add_paragraph(f"[image not available: {src}]")
                continue
            par = doc.add_paragraph()
            _docx_inline(par, kids)
        elif typ == "list":
            ordered = bool((tok.get("attrs") or {}).get("ordered"))
            depth = min(list_depth, MAX_LIST_DEPTH - 1)
            style = (("List Number", "List Number 2")[depth] if ordered
                     else ("List Bullet", "List Bullet 2")[depth])
            for item in tok.get("children") or []:
                for blk in item.get("children") or []:
                    if blk.get("type") in ("block_text", "paragraph"):
                        try:
                            par = doc.add_paragraph(style=style)
                        except KeyError:
                            par = doc.add_paragraph()
                        _docx_inline(par, blk.get("children"))
                    elif blk.get("type") == "list":
                        _docx_blocks(doc, [blk], base_dir, list_depth + 1)
                    else:
                        # nested code/quote/table: render, don't drop
                        _docx_blocks(doc, [blk], base_dir, list_depth)
        elif typ == "table":
            head, body = [], []
            for part in tok.get("children") or []:
                if part.get("type") == "table_head":
                    head = part.get("children") or []
                elif part.get("type") == "table_body":
                    body = [r.get("children") or []
                            for r in part.get("children") or []]
            ncols = max([len(head), *(len(r) for r in body)])
            if ncols == 0:
                continue
            table = doc.add_table(rows=0, cols=ncols)
            table.style = "Table Grid"
            if head:
                cells = table.add_row().cells
                for i in range(ncols):
                    if i < len(head):
                        par = cells[i].paragraphs[0]
                        _docx_inline(par, head[i].get("children"), bold=True)
                        for run in par.runs:
                            run.font.color.rgb = RGBColor.from_string("FFFFFF")
                    _docx_shade(cells[i], NAVY)
            for ri, row in enumerate(body):
                cells = table.add_row().cells
                for i in range(ncols):
                    if i < len(row):
                        _docx_inline(cells[i].paragraphs[0],
                                     row[i].get("children"))
                    if ri % 2 == 1:
                        _docx_shade(cells[i], ZEBRA)
            doc.add_paragraph()
        elif typ == "block_code":
            for line in str(tok.get("raw", "")).rstrip("\n").split("\n"):
                par = doc.add_paragraph()
                run = par.add_run(line or " ")
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                _docx_shade(par, CODE_BG)
        elif typ == "block_quote":
            for sub in tok.get("children") or []:
                if sub.get("type") == "paragraph":
                    par = doc.add_paragraph()
                    par.paragraph_format.left_indent = Inches(0.3)
                    _docx_inline(par, sub.get("children"), italic=True,
                                 color=GRAY)
                else:
                    _docx_blocks(doc, [sub], base_dir)
        elif typ == "thematic_break":
            doc.add_paragraph()
        elif typ == "blank_line":
            continue
        else:
            raw = tok.get("raw") or _plain(tok.get("children"))
            if raw and str(raw).strip():
                doc.add_paragraph(str(raw))


def docx_from_markdown(md: str, path: Path, *, title: str | None = None) -> None:
    tokens = _parse(md)
    import docx
    from docx.shared import Pt, RGBColor

    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Inter"
    style.font.size = Pt(10.5)

    if title:
        par = doc.add_heading(level=0)
        run = par.add_run(title)
        run.font.name = "Space Grotesk"
        run.font.color.rgb = RGBColor.from_string(NAVY.lstrip("#"))
        sub = doc.add_paragraph()
        srun = sub.add_run(datetime.now(timezone.utc).strftime("%d %b %Y"))
        srun.font.color.rgb = RGBColor.from_string(GRAY.lstrip("#"))
        srun.font.size = Pt(9)

    _docx_blocks(doc, tokens, path.parent)
    doc.save(str(path))


# ── XLSX ─────────────────────────────────────────────────────────────────

_SHEET_BAD = set("[]:*?/\\")


def xlsx_from_spec(spec: dict, path: Path) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("sheets"), list):
        raise DocGenError(
            "spec must be {'sheets': [...]}, got "
            f"{'keys ' + str(sorted(spec.keys())) if isinstance(spec, dict) else type(spec).__name__}")
    sheets = spec["sheets"]
    if not sheets:
        raise DocGenError("spec has no sheets")
    if len(sheets) > MAX_SHEETS:
        raise DocGenError(f"{len(sheets)} sheets exceeds max {MAX_SHEETS}")

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used_names: set[str] = set()
    head_fill = PatternFill("solid", start_color=NAVY.lstrip("#"))
    zebra_fill = PatternFill("solid", start_color=ZEBRA.lstrip("#"))
    head_font = Font(name="Inter", bold=True, color="FFFFFF")

    for si, sheet in enumerate(sheets, 1):
        if not isinstance(sheet, dict):
            raise DocGenError(f"sheet {si}: must be an object")
        raw_name = str(sheet.get("name") or f"Sheet{si}")
        name = "".join(ch for ch in raw_name if ch not in _SHEET_BAD)[:31] or f"Sheet{si}"
        base, n = name, 2
        while name in used_names:
            name = f"{base[:28]}_{n}"
            n += 1
        used_names.add(name)

        headers = sheet.get("headers")
        rows = sheet.get("rows")
        if not isinstance(headers, list) or not headers:
            raise DocGenError(f"sheet '{raw_name}': 'headers' must be a non-empty list")
        if len(headers) > MAX_COLS:
            raise DocGenError(f"sheet '{raw_name}': {len(headers)} columns exceeds max {MAX_COLS}")
        if not isinstance(rows, list):
            raise DocGenError(f"sheet '{raw_name}': 'rows' must be a list")
        if len(rows) > MAX_ROWS_PER_SHEET:
            raise DocGenError(
                f"sheet '{raw_name}': {len(rows)} rows exceeds max "
                f"{MAX_ROWS_PER_SHEET} — filter or split the data")

        ws = wb.create_sheet(name)
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=str(h))
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(vertical="center")

        for ri, row in enumerate(rows, 2):
            if not isinstance(row, list):
                raise DocGenError(f"sheet '{raw_name}': row {ri - 1} is not a list")
            if len(row) != len(headers):
                raise DocGenError(
                    f"sheet '{raw_name}': row {ri - 1} has {len(row)} cells "
                    f"but there are {len(headers)} headers")
            for ci, val in enumerate(row, 1):
                if val is not None and not isinstance(val, (str, int, float, bool)):
                    val = str(val)
                c = ws.cell(row=ri, column=ci, value=val)
                if ri % 2 == 1:            # data rows 2,3,4... → zebra odd ones
                    c.fill = zebra_fill

        ws.freeze_panes = "A2"
        last_col = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A1:{last_col}{max(len(rows) + 1, 1)}"

        widths = sheet.get("widths")
        for ci, h in enumerate(headers, 1):
            letter = get_column_letter(ci)
            if isinstance(widths, list) and ci <= len(widths) and isinstance(widths[ci - 1], (int, float)):
                ws.column_dimensions[letter].width = float(widths[ci - 1])
            else:
                longest = max([len(str(h))] +
                              [len(str(r[ci - 1])) for r in rows[:200]
                               if r[ci - 1] is not None] or [4])
                ws.column_dimensions[letter].width = min(longest + 2, 60)

        formats = sheet.get("formats")
        if isinstance(formats, dict):
            for hname, fmt in formats.items():
                if hname in [str(h) for h in headers] and isinstance(fmt, str):
                    ci = [str(h) for h in headers].index(hname) + 1
                    for ri in range(2, len(rows) + 2):
                        ws.cell(row=ri, column=ci).number_format = fmt

    wb.save(str(path))


# ── Markdown ─────────────────────────────────────────────────────────────

def markdown_file(md: str, path: Path) -> None:
    if len(md) > MAX_MD_CHARS:
        raise DocGenError(
            f"content too large ({len(md)} chars, max {MAX_MD_CHARS}) — "
            "split into multiple files")
    path.write_text(md, encoding="utf-8", newline="\n")
