"""LayerLens module — pure parts + full deconstruct/reconstruct on a synthetic
layered PDF (no LLM, no network: llm grouping is forced to fail soft)."""

import asyncio
import json

import fitz  # PyMuPDF
import pytest

from app import layerlens

LAYERS = ["E-11KV-CABLE", "SEWA-DRAIN-MH", "TXT-NOTES"]


@pytest.fixture()
def layered_pdf(tmp_path):
    """A one-page PDF with three OCG layers, each owning some visible content."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    for i, name in enumerate(LAYERS):
        xref = doc.add_ocg(name, on=True)
        y = 50 + i * 80
        page.draw_rect(fitz.Rect(40, y, 200, y + 40), color=(0, 0, 0),
                       fill=(0.2, 0.4, 0.8), oc=xref)
        page.insert_text(fitz.Point(210, y + 25), name, fontsize=10, oc=xref)
    path = tmp_path / "drawing.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_list_layers(layered_pdf):
    assert sorted(layerlens.list_layers(str(layered_pdf))) == sorted(LAYERS)


def test_list_layers_plain_pdf(tmp_path):
    doc = fitz.open()
    doc.new_page()
    p = tmp_path / "plain.pdf"
    doc.save(str(p))
    doc.close()
    assert layerlens.list_layers(str(p)) == []


def test_heuristic_group_classifies_known_systems():
    groups = layerlens.heuristic_group(LAYERS)
    flat = {layer: g for g, ls in groups.items() for layer in ls}
    assert flat["E-11KV-CABLE"] == "Power — 11kV (MV)"
    assert flat["SEWA-DRAIN-MH"] == "Drainage / Sewer / Storm"
    assert flat["TXT-NOTES"] == "Annotation, Text & Dims"
    # every layer lands in exactly one group
    assert sorted(flat) == sorted(LAYERS)


def test_group_layers_fails_soft_to_heuristic(monkeypatch):
    async def boom(names):
        raise RuntimeError("no key")
    monkeypatch.setattr(layerlens, "llm_group_layers", boom)
    groups, method = asyncio.run(layerlens.group_layers(LAYERS))
    assert method == "heuristic"
    assert sum(len(v) for v in groups.values()) == len(LAYERS)


def test_deconstruct_and_reconstruct(layered_pdf, tmp_path, monkeypatch):
    async def boom(names):
        raise RuntimeError("no key")
    monkeypatch.setattr(layerlens, "llm_group_layers", boom)

    out = tmp_path / "out"
    report = asyncio.run(layerlens.deconstruct(layered_pdf, out))
    assert report["layer_count"] == 3
    assert report["grouping_method"] == "heuristic"
    assert (out / layerlens.REPORT_NAME).is_file()
    assert (out / report["full_render"]).is_file()
    for g in report["groups"]:
        assert (out / g["pdf"]).is_file() and (out / g["png"]).is_file()

    # isolated group PDF: only that group's layers are ON
    power = next(g for g in report["groups"] if "11kV" in g["group"])
    import pikepdf
    with pikepdf.open(out / power["pdf"]) as pdf:
        d = pdf.Root.OCProperties.D
        assert str(d.BaseState) == "/OFF"
        on_names = {str(o.Name) for o in d.ON}
        assert on_names == {"E-11KV-CABLE"}

    # reconstruction: chosen union of layers visible in a fresh PDF
    res = asyncio.run(layerlens.reconstruct(
        layered_pdf, tmp_path / "rebuilt.pdf", {"E-11KV-CABLE", "TXT-NOTES", "GHOST"}))
    assert res["layers_on"] == 2 and res["layers_missing"] == ["GHOST"]
    with pikepdf.open(tmp_path / "rebuilt.pdf") as pdf:
        on_names = {str(o.Name) for o in pdf.Root.OCProperties.D.ON}
        assert on_names == {"E-11KV-CABLE", "TXT-NOTES"}
    assert (tmp_path / "rebuilt.png").is_file()


def test_reconstruct_rejects_unknown_layers(layered_pdf, tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(layerlens.reconstruct(layered_pdf, tmp_path / "x.pdf", {"NOPE"}))


def _make_revision(tmp_path, name, extra_text=None):
    """Synthetic revision: same three layers; optionally extra text on 11KV."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    for i, lname in enumerate(LAYERS):
        xref = doc.add_ocg(lname, on=True)
        y = 50 + i * 80
        page.draw_rect(fitz.Rect(40, y, 200, y + 40), color=(0, 0, 0),
                       fill=(0.2, 0.4, 0.8), oc=xref)
        page.insert_text(fitz.Point(210, y + 25), lname, fontsize=10, oc=xref)
        if extra_text and lname == "E-11KV-CABLE":
            page.insert_text(fitz.Point(60, y + 20), extra_text, fontsize=12, oc=xref)
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def test_text_extraction_multipage(tmp_path):
    doc = fitz.open()
    for pno in range(2):
        page = doc.new_page(width=300, height=200)
        xref = doc.add_ocg(f"TXT-P{pno + 1}", on=True)
        page.insert_text(fitz.Point(50, 100), f"HELLO-PAGE-{pno + 1}", oc=xref)
    p = tmp_path / "two-pages.pdf"
    doc.save(str(p))
    doc.close()
    rows = layerlens.extract_text_per_layer(str(p))
    pages = {r[0] for r in rows}
    texts = {r[2] for r in rows}
    assert pages == {1, 2}
    assert {"HELLO-PAGE-1", "HELLO-PAGE-2"} <= texts


def test_diff_drawings_detects_change(tmp_path, monkeypatch):
    async def boom(names):
        raise RuntimeError("no key")
    monkeypatch.setattr(layerlens, "llm_group_layers", boom)

    a = _make_revision(tmp_path, "rev-a.pdf")
    b = _make_revision(tmp_path, "rev-b.pdf", extra_text="NEW FMH-11KV NOTE")
    work = tmp_path / "work"
    ra = asyncio.run(layerlens.deconstruct(a, work))
    rb = asyncio.run(layerlens.deconstruct(b, work))
    assert ra["stem"] != rb["stem"]
    # per-stem reports coexist (the old fixed name would have clobbered)
    assert (work / layerlens.report_name(ra["stem"])).is_file()
    assert (work / layerlens.report_name(rb["stem"])).is_file()

    res = asyncio.run(layerlens.diff_drawings(work, ra["stem"], rb["stem"]))
    assert res["changed_groups"] >= 1
    changed = {r["group"] for r in res["log"]}
    assert any("11kV" in g for g in changed)
    added = [t for r in res["log"] for t in r["samples_added"]]
    assert any("FMH-11KV" in t for t in added)
    assert (work / res["revision_log_csv"]).is_file()
    assert (work / res["diff_json"]).is_file()
    # unchanged groups stay quiet
    assert not any("Drainage" in g for g in changed)


def test_llm_grouping_validation(monkeypatch):
    """llm_group_layers drops hallucinated names and fails when coverage is poor.
    The anthropic client and settings are faked — no key or network needed."""
    import sys
    import types

    calls = {}

    class FakeBlock:
        type = "text"
        def __init__(self, t): self.text = t

    class FakeMessages:
        async def create(self, **kw):
            return types.SimpleNamespace(content=[FakeBlock(calls["payload"])])

    class FakeClient:
        def __init__(self, **kw): self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(AsyncAnthropic=FakeClient))
    monkeypatch.setattr(layerlens, "get_settings", lambda: types.SimpleNamespace(
        anthropic_api_key="sk-test", triage_model="claude-haiku-4-5"))

    good = {"groups": {"Power": ["E-11KV-CABLE", "HALLUCINATED-LAYER"],
                       "Drainage": ["SEWA-DRAIN-MH"],
                       "Notes": ["TXT-NOTES"]}}
    calls["payload"] = json.dumps(good)
    groups = asyncio.run(layerlens.llm_group_layers(LAYERS))
    flat = [l for ls in groups.values() for l in ls]
    assert sorted(flat) == sorted(LAYERS)          # hallucination dropped, all covered
    assert "HALLUCINATED-LAYER" not in flat

    calls["payload"] = json.dumps({"groups": {"Power": ["E-11KV-CABLE"]}})
    with pytest.raises(ValueError):                # <50% coverage → caller falls back
        asyncio.run(layerlens.llm_group_layers(LAYERS))
