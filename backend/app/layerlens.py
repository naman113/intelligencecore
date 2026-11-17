"""LayerLens — CAD-PDF layer deconstruction / reconstruction with LLM grouping.

A PDF plotted from CAD carries its layers as Optional Content Groups (OCGs).
This module:

  1. lists those layers,
  2. groups them into semantic systems (Power, Drainage, Annotation, ...) by
     asking the configured LLM; a keyword heuristic is the fail-soft fallback
     so the pipeline still works with no LLM key or a bad LLM response,
  3. DECONSTRUCTS: writes one layer-isolated vector PDF + PNG preview per
     group into a target directory, plus a machine-readable report,
  4. RECONSTRUCTS: builds a new PDF with any chosen subset of layers visible.

Ported from the standalone LayerLens demo (Civil Drawing/Layerlens).
Dependencies are pure-pip: pikepdf, PyMuPDF (fitz), Pillow.

All pikepdf/fitz work is synchronous CPU-bound code — the async entry points
run it via asyncio.to_thread so the gateway's event loop is never blocked.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from .config import get_settings

log = logging.getLogger("layerlens")

RENDER_DPI = 110
DIFF_INK_THRESHOLD = 150       # px of changed ink before a group counts as changed
MAX_LLM_LAYERS = 1500          # cap the layer list sent to the LLM


def report_name(stem: str) -> str:
    """Per-source report filename, so deconstructing two revisions in one
    workspace never clobbers state (required for diffing)."""
    return f"{stem}.layerlens.json"


# Kept for reconstruct-tool backward reference: the most recent deconstruct also
# writes this alias so single-drawing flows keep working without knowing stems.
REPORT_NAME = "layerlens_report.json"

# ── heuristic fallback grouping (first match wins, uppercase substrings) ──
GROUP_RULES = [
    ("Power — 132kV (HV)",        ["132KV", "132 KV", "B-EX-HV", "CABLEROUTE"]),
    ("Power — 11kV (MV)",         ["11KV", "MED_VOLT", "MV MH", "B-EX-MV", "E-HV-", "ETA-HV", "XMV", "E-X-MED"]),
    ("Communications",            ["COMM", "CMH", "TELEPHONE", "FIBER", "FIBRE"]),
    ("Drainage / Sewer / Storm",  ["SEWA", "SWMH", "SMH", "DRAIN", "STORM", "PL-SEW"]),
    ("Mechanical — CHW/Fire/Fuel", ["CWTR", "CHW", "FIRE", "FUEL", "GLYCOL", "PCA", "M-TRENCH", "SPCL-PIPE"]),
    ("Lighting & LV",             ["P-LIGHT", "LIGHT", "LTG", "ELEC-CABLE", "E-ANNO"]),
    ("Pavement & Rehabilitation", ["PAVEMENT", "ASPHALT", "CONCRETE", "REHAB", "INTERLOCK"]),
    ("Annotation, Text & Dims",   ["TXT", "TEXT", "ANNO", "-DIM", "DIMS", "NOTE", "LEDR", "REVS", "TITLE"]),
    ("Frame, Title-block & Grid", ["FRAME", "TTLB", "KEYPLAN", "GRID", "CUTSHEET", "LOGO"]),
    ("Base Map / Site",           ["ROAD", "TAXI", "RWY", "AFLD", "FENCE", "STAND", "APRON", "MARKING",
                                   "SITE PLAN", "WALL", "FLOR", "DOOR", "KERB", "CURB", "LDSP", "TUNNEL",
                                   "A-", "TR-", "CT-", "M-", "C-", "U-"]),
]
FALLBACK_GROUP = "Other / Unclassified"


# ── layer listing / low-level PDF ops (sync; call via asyncio.to_thread) ──


def list_layers(pdf_path: str) -> list[str]:
    """Names of the PDF's optional content groups; [] if not a layered PDF."""
    import pikepdf
    with pikepdf.open(pdf_path) as pdf:
        if "/OCProperties" not in pdf.Root:
            return []
        return [str(o.Name) for o in pdf.Root.OCProperties.OCGs]


def save_isolated_pdf(src: str, dst: str, on_names: set[str]) -> None:
    """Save a copy of `src` with ONLY `on_names` layers visible (vector, zoomable).
    This is both the deconstruction primitive (one group ON) and the
    reconstruction primitive (any chosen union of layers ON)."""
    import pikepdf
    pdf = pikepdf.open(src)
    try:
        ocp = pdf.Root.OCProperties
        on = [o for o in ocp.OCGs if str(o.Name) in on_names]
        off = [o for o in ocp.OCGs if str(o.Name) not in on_names]
        ocp.D = pikepdf.Dictionary(BaseState=pikepdf.Name("/OFF"),
                                   ON=pikepdf.Array(on), OFF=pikepdf.Array(off))
        pdf.save(dst)
    finally:
        pdf.close()


def render_pdf_png(src: str, out_png: str, dpi: int = RENDER_DPI) -> None:
    """Render page 1 of a saved PDF to PNG. Render from the SAVED file:
    PyMuPDF caches layer visibility at open time, so the OC config must
    already be baked in (by save_isolated_pdf)."""
    import fitz
    doc = fitz.open(src)
    try:
        doc[0].get_pixmap(dpi=dpi).save(out_png)
    finally:
        doc.close()


def render_full_png(src: str, out_png: str, dpi: int = RENDER_DPI) -> None:
    """Render page 1 with ALL layers forced on."""
    import fitz
    doc = fitz.open(src)
    try:
        ocgs = doc.get_ocgs()
        if ocgs:
            doc.set_layer(-1, on=list(ocgs.keys()), off=[])
        doc[0].get_pixmap(dpi=dpi).save(out_png)
    finally:
        doc.close()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "group"


# ── per-layer text extraction (sync; ported from the LayerLens demo) ─────
# Walks every page's content stream (BDC/EMC marked content + form XObjects),
# decoding CID text via ToUnicode, and attributes each string to its OCG layer.


def _parse_tounicode(font):
    m = {}
    try:
        data = font.ToUnicode.read_bytes().decode("latin1")
    except Exception:
        return m
    for block in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for s, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            m[int(s, 16)] = "".join(chr(int(d[i:i + 4], 16)) for i in range(0, len(d), 4))
    for block in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            for c in range(int(lo, 16), int(hi, 16) + 1):
                m[c] = chr(int(d, 16) + c - int(lo, 16))
    return m


def extract_text_per_layer(pdf_path: str) -> list[tuple]:
    """Rows of (page, layer, text, x, y) across ALL pages."""
    import pikepdf
    pdf = pikepdf.open(pdf_path)
    rows: list[tuple] = []
    cmaps: dict = {}

    def get_cmap(res, fname):
        try:
            f = res.Font[fname]
        except Exception:
            return {}
        if f.objgen not in cmaps:
            cmaps[f.objgen] = _parse_tounicode(f)
        return cmaps[f.objgen]

    def decode(raw, cmap):
        if not cmap:
            return raw.decode("latin1", "replace")
        return "".join(cmap.get((raw[i] << 8) | raw[i + 1], "")
                       for i in range(0, len(raw) - 1, 2))

    ID = (1, 0, 0, 1, 0, 0)

    def mmul(m1, m2):
        a1, b1, c1, d1, e1, f1 = m1
        a2, b2, c2, d2, e2, f2 = m2
        return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2,
                c1 * b2 + d1 * d2, e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)

    def ocg_label(res, tag):
        try:
            o = res.Properties[tag]
            if "/Name" in o:
                return str(o.Name)
            if "/OCGs" in o:
                g = o.OCGs
                return str(g[0].Name) if isinstance(g, pikepdf.Array) else str(g.Name)
        except Exception:
            pass
        return "(untagged)"

    def walk(page_no, obj, res, inherited, ctm0, seen, depth=0):
        if depth > 8:
            return
        stack, cur_font = [], None
        ctm = ctm0
        gs = []
        tm = tlm = ID
        TL = 0.0

        def emit(s):
            if s.strip():
                trm = mmul(tm, ctm)
                rows.append((page_no, stack[-1] if stack else inherited,
                             s.strip(), trm[4], trm[5]))
        try:
            ins = pikepdf.parse_content_stream(obj)
        except Exception:
            return
        for inst in ins:
            op, ops = str(inst.operator), inst.operands
            if op == "q":
                gs.append(ctm)
            elif op == "Q":
                ctm = gs.pop() if gs else ctm0
            elif op == "cm":
                ctm = mmul(tuple(float(v) for v in ops), ctm)
            elif op == "BT":
                tm = tlm = ID
            elif op == "Tm":
                tm = tlm = tuple(float(v) for v in ops)
            elif op in ("Td", "TD"):
                tx, ty = float(ops[0]), float(ops[1])
                if op == "TD":
                    TL = -ty
                tlm = mmul((1, 0, 0, 1, tx, ty), tlm)
                tm = tlm
            elif op == "T*":
                tlm = mmul((1, 0, 0, 1, 0, -TL), tlm)
                tm = tlm
            elif op == "TL":
                TL = float(ops[0])
            elif op == "BDC":
                stack.append(ocg_label(res, str(ops[1]))
                             if len(ops) == 2 and str(ops[0]) == "/OC"
                             else (stack[-1] if stack else inherited))
            elif op == "BMC":
                stack.append(stack[-1] if stack else inherited)
            elif op == "EMC":
                if stack:
                    stack.pop()
            elif op == "Tf":
                cur_font = str(ops[0])
            elif op in ("Tj", "'", '"'):
                emit(decode(bytes(ops[-1]), get_cmap(res, cur_font)))
            elif op == "TJ":
                emit("".join(decode(bytes(e), get_cmap(res, cur_font))
                             for e in ops[0] if isinstance(e, pikepdf.String)))
            elif op == "Do":
                try:
                    xo = res.XObject[str(ops[0])]
                    if xo.get("/Subtype") == pikepdf.Name("/Form"):
                        key = (xo.objgen, round(ctm[4], 1), round(ctm[5], 1))
                        if key in seen:
                            continue
                        seen.add(key)
                        xm = tuple(float(v) for v in xo.Matrix) if "/Matrix" in xo else ID
                        il = inherited
                        if "/OC" in xo and "/Name" in xo.OC:
                            il = str(xo.OC.Name)
                        walk(page_no, xo, xo.get("/Resources", res),
                             stack[-1] if stack else il, mmul(xm, ctm), seen, depth + 1)
                except Exception:
                    pass

    try:
        for i, page in enumerate(pdf.pages):
            walk(i + 1, page.obj, page.Resources, "(untagged)", ID, set())
    finally:
        pdf.close()
    return rows


# ── grouping ─────────────────────────────────────────────────────────────


def heuristic_group(names: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for n in names:
        u = n.upper()
        for group, keys in GROUP_RULES:
            if any(k in u for k in keys):
                groups[group].append(n)
                break
        else:
            groups[FALLBACK_GROUP].append(n)
    ordered = [g for g, _ in GROUP_RULES] + [FALLBACK_GROUP]
    return {g: groups[g] for g in ordered if groups.get(g)}


_LLM_PROMPT = """You are a senior civil/CAD engineer. Below is the list of CAD layer names
found in one construction-drawing PDF. Group them into 6-14 semantic systems an
engineer would recognize (e.g. "Power — 11kV (MV)", "Drainage / Sewer / Storm",
"Communications", "Annotation, Text & Dims", "Frame & Title-block", "Base Map").

Rules:
- EVERY layer must appear in EXACTLY one group. Do not invent layer names.
- Copy layer names EXACTLY as given (case, spaces, punctuation).
- Use "Other / Unclassified" for anything you cannot place.
- Answer with ONLY a JSON object, no prose, no code fences:
  {"groups": {"<group name>": ["<layer>", ...], ...}}

LAYERS:
"""


async def llm_group_layers(names: list[str]) -> dict[str, list[str]]:
    """Ask the configured LLM to group layer names. Raises on any failure —
    callers fall back to heuristic_group()."""
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("no anthropic_api_key configured")
    capped = names[:MAX_LLM_LAYERS]
    from . import llmclient  # Phase 13 (GOLIVE-01)
    client = llmclient.anthropic_async(s.anthropic_api_key, role="drawing")  # Quick 005
    msg = await client.messages.create(
        **llmclient.messages_kwargs(  # Phase 13 (GOLIVE-01)
            s.triage_model,        # cheap gate model is plenty for naming
            role="triage", max_tokens=8192, settings=s),
        messages=[{"role": "user",
                   "content": _LLM_PROMPT + "\n".join(capped)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    raw = data["groups"] if isinstance(data, dict) and "groups" in data else data
    if not isinstance(raw, dict) or not raw:
        raise ValueError("LLM returned no groups")

    known = set(capped)
    groups: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for gname, layers in raw.items():
        if not isinstance(layers, list):
            continue
        for layer in layers:
            if layer in known and layer not in seen:   # drop hallucinated/duped names
                groups[str(gname)[:80]].append(layer)
                seen.add(layer)
    leftovers = [n for n in capped if n not in seen] + names[MAX_LLM_LAYERS:]
    if leftovers:
        groups[FALLBACK_GROUP].extend(leftovers)
    # A grouping that dropped most layers into fallback is worse than the heuristic.
    if len(seen) < len(capped) * 0.5:
        raise ValueError(f"LLM placed only {len(seen)}/{len(capped)} layers")
    return dict(groups)


async def group_layers(names: list[str]) -> tuple[dict[str, list[str]], str]:
    """LLM grouping with heuristic fail-soft. Returns (groups, method)."""
    try:
        return await llm_group_layers(names), "llm"
    except Exception as e:
        log.warning("LLM grouping unavailable (%s: %s) — using heuristic",
                    type(e).__name__, e)
        return heuristic_group(names), "heuristic"


# ── high-level operations ────────────────────────────────────────────────


async def deconstruct(pdf_path: Path, out_dir: Path) -> dict:
    """Full deconstruction: layers → LLM grouping → per-group isolated PDF +
    PNG preview + full-sheet render + report (saved as layerlens_report.json).
    Output filenames are flat (no subdirs) so agents can send them directly."""
    pdf_path, out_dir = Path(pdf_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    layers = await asyncio.to_thread(list_layers, str(pdf_path))
    if not layers:
        raise ValueError(
            "no CAD layers found — this PDF was not plotted with layer "
            "information (optional content groups)")

    groups, method = await group_layers(layers)

    stem = slugify(pdf_path.stem)[:40]
    full_png = f"{stem}-full.png"
    await asyncio.to_thread(render_full_png, str(pdf_path), str(out_dir / full_png))

    group_meta = []
    for gname, names in groups.items():
        slug = slugify(gname)
        gpdf, gpng = f"{stem}-{slug}.pdf", f"{stem}-{slug}.png"
        await asyncio.to_thread(save_isolated_pdf, str(pdf_path),
                                str(out_dir / gpdf), set(names))
        await asyncio.to_thread(render_pdf_png, str(out_dir / gpdf),
                                str(out_dir / gpng))
        group_meta.append({"group": gname, "slug": slug, "layer_count": len(names),
                           "layers": sorted(names), "pdf": gpdf, "png": gpng})

    # Per-layer text across ALL pages → <stem>-text.csv (structured output +
    # the substrate for revision diffs).
    import csv as _csv
    layer_to_group = {n: g for g, ns in groups.items() for n in ns}
    rows = await asyncio.to_thread(extract_text_per_layer, str(pdf_path))
    text_csv = f"{stem}-text.csv"
    with open(out_dir / text_csv, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["page", "group", "layer", "text", "x_pt", "y_pt"])
        for page_no, layer, text, x, y in rows:
            w.writerow([page_no, layer_to_group.get(layer, "(untagged)"),
                        layer, text, round(x, 2), round(y, 2)])

    import fitz
    page_count = await asyncio.to_thread(lambda: len(fitz.open(str(pdf_path))))

    report = {
        "source": pdf_path.name,
        "stem": stem,
        "page_count": page_count,          # group PDFs carry ALL pages; PNG previews are page 1
        "layer_count": len(layers),
        "group_count": len(groups),
        "grouping_method": method,
        "full_render": full_png,
        "text_csv": text_csv,
        "text_strings": len(rows),
        "groups": group_meta,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = json.dumps(report, indent=1)
    (out_dir / report_name(stem)).write_text(payload)
    (out_dir / REPORT_NAME).write_text(payload)   # "most recent" alias
    return report


async def reconstruct(pdf_path: Path, out_pdf: Path, layer_names: set[str],
                      render_png_too: bool = True) -> dict:
    """Rebuild a drawing with exactly `layer_names` visible (vector PDF, plus a
    PNG preview alongside)."""
    pdf_path, out_pdf = Path(pdf_path), Path(out_pdf)
    available = set(await asyncio.to_thread(list_layers, str(pdf_path)))
    missing = sorted(layer_names - available)
    on = layer_names & available
    if not on:
        raise ValueError("none of the requested layers exist in this PDF")
    await asyncio.to_thread(save_isolated_pdf, str(pdf_path), str(out_pdf), on)
    png = None
    if render_png_too:
        png = out_pdf.with_suffix(".png")
        await asyncio.to_thread(render_pdf_png, str(out_pdf), str(png))
    return {"pdf": out_pdf.name, "png": png.name if png else None,
            "layers_on": len(on), "layers_missing": missing[:20]}


# ── revision diff ────────────────────────────────────────────────────────
# Compares two DECONSTRUCTED drawings (their reports + text CSVs + group PNGs
# must exist in `work_dir`): layer add/remove, per-group text add/remove, and
# green/red ink-change overlays. Groups are matched by NAME across the two
# reports — deconstruct both revisions in the same conversation so the LLM
# grouping stays comparable; unmatched groups still get text/layer diffs.


def _ink_mask(png_path, size=None):
    import numpy as np
    from PIL import Image
    im = Image.open(png_path).convert("L")
    if size and im.size != size:
        im = im.resize(size)
    return (np.asarray(im) < 200), im.size


def _load_text_csv(path: Path) -> dict[str, set]:
    import csv as _csv
    out: dict[str, set] = defaultdict(set)
    with open(path) as fh:
        for row in _csv.DictReader(fh):
            out[row["group"]].add((row["layer"], row["text"], row.get("page", "1"),
                                   round(float(row["x_pt"]) / 3) * 3,
                                   round(float(row["y_pt"]) / 3) * 3))
    return out


def _diff_sync(work_dir: Path, ra: dict, rb: dict, prefix: str) -> dict:
    import numpy as np
    from PIL import Image, ImageFilter

    # Reports store layers per group — flatten for the add/remove comparison.
    la = {n for g in ra["groups"] for n in g["layers"]}
    lb = {n for g in rb["groups"] for n in g["layers"]}
    layers_added, layers_removed = sorted(lb - la), sorted(la - lb)

    ta = _load_text_csv(work_dir / ra["text_csv"])
    tb = _load_text_csv(work_dir / rb["text_csv"])

    slug_a = {g["group"]: g for g in ra["groups"]}
    slug_b = {g["group"]: g for g in rb["groups"]}
    groups = sorted(set(list(ta) + list(tb) + list(slug_a) + list(slug_b)))

    log_rows, diff_groups = [], []
    for g in groups:
        added_txt = sorted({t[1] for t in tb.get(g, set()) - ta.get(g, set())})[:200]
        removed_txt = sorted({t[1] for t in ta.get(g, set()) - tb.get(g, set())})[:200]

        ink_added = ink_removed = 0
        overlay_rel = None
        pa = work_dir / slug_a[g]["png"] if g in slug_a else None
        pb = work_dir / slug_b[g]["png"] if g in slug_b else None
        if pa and pb and pa.exists() and pb.exists():
            ma, _ = _ink_mask(pa)
            mb, _ = _ink_mask(pb, size=Image.open(pa).size)
            ma_d = np.asarray(Image.fromarray(ma).filter(ImageFilter.MaxFilter(3)))
            mb_d = np.asarray(Image.fromarray(mb).filter(ImageFilter.MaxFilter(3)))
            added_m, removed_m = mb & ~ma_d, ma & ~mb_d
            ink_added, ink_removed = int(added_m.sum()), int(removed_m.sum())
            if ink_added + ink_removed > DIFF_INK_THRESHOLD:
                base = np.asarray(Image.open(pb).convert("L").resize(Image.open(pa).size))
                rgb = np.stack([base] * 3, axis=-1).copy()
                rgb[base < 250] = [205, 205, 205]     # unchanged ink → light gray
                rgb[removed_m] = [220, 38, 38]        # removed → red
                rgb[added_m] = [22, 163, 74]          # added → green
                overlay_rel = f"{prefix}-overlay-{slugify(g)}.png"
                Image.fromarray(rgb.astype(np.uint8)).save(work_dir / overlay_rel)

        changed = bool(added_txt or removed_txt
                       or ink_added + ink_removed > DIFF_INK_THRESHOLD)
        if changed:
            log_rows.append({"group": g,
                             "text_added": len(added_txt), "text_removed": len(removed_txt),
                             "ink_added_px": ink_added, "ink_removed_px": ink_removed,
                             "samples_added": added_txt[:12],
                             "samples_removed": removed_txt[:12],
                             "overlay": overlay_rel})
        diff_groups.append({"group": g, "changed": changed, "overlay": overlay_rel,
                            "text_added": added_txt, "text_removed": removed_txt,
                            "ink_added_px": ink_added, "ink_removed_px": ink_removed})

    result = {"from": ra["source"], "to": rb["source"],
              "layers_added": layers_added, "layers_removed": layers_removed,
              "changed_groups": len(log_rows), "log": log_rows, "groups": diff_groups}

    import csv as _csv
    (work_dir / f"{prefix}-diff.json").write_text(json.dumps(result, indent=1))
    with open(work_dir / f"{prefix}-revision-log.csv", "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["from", "to", "group", "text_added", "text_removed",
                    "ink_added_px", "ink_removed_px", "sample_added", "sample_removed"])
        for r in log_rows:
            w.writerow([ra["source"], rb["source"], r["group"],
                        r["text_added"], r["text_removed"],
                        r["ink_added_px"], r["ink_removed_px"],
                        " | ".join(r["samples_added"][:5]),
                        " | ".join(r["samples_removed"][:5])])
    result["diff_json"] = f"{prefix}-diff.json"
    result["revision_log_csv"] = f"{prefix}-revision-log.csv"
    return result


async def diff_drawings(work_dir: Path, stem_a: str, stem_b: str) -> dict:
    """Diff two deconstructed drawings (older stem_a → newer stem_b)."""
    work_dir = Path(work_dir)
    ra = json.loads((work_dir / report_name(stem_a)).read_text())
    rb = json.loads((work_dir / report_name(stem_b)).read_text())
    prefix = f"diff-{stem_a}-vs-{stem_b}"[:60]
    return await asyncio.to_thread(_diff_sync, work_dir, ra, rb, prefix)
