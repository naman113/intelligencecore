"""use_skill(name, part) (agents/graph.py): on-demand recipes/ and reference/
files of a library skill. Pure logic: DB helpers faked, the library is a
tmp_path tree behind the REAL library_entry/asset_path chain."""

import json

import pytest

from app import skillservice
from app.agents import graph

ENTRY = {"id": "ic/dash", "name": "dash", "description": "d", "source": "ic",
         "license": "MIT", "compat": "general", "path": "ic/dash"}
ROW = {"id": 1, "name": "dash", "description": "d", "body": "BODY TEXT",
       "source": "library", "origin_ref": "ic/dash", "license": "MIT",
       "compat": "general", "enabled": True}


def _wire(monkeypatch, tmp_path, row=ROW):
    calls = []

    async def fake_log(name, args, result, start, refused=False):
        calls.append({"name": name, "args": args, "result": result,
                      "refused": refused})

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    lib = tmp_path / "skills_library"
    d = lib / "ic" / "dash"
    (d / "recipes").mkdir(parents=True)
    (d / "reference").mkdir()
    (d / "scripts").mkdir()
    (d / "recipes" / "daily.md").write_text("RECIPE", encoding="utf-8")
    (d / "recipes" / "big.md").write_text("x" * 30000, encoding="utf-8")
    (d / "scripts" / "x.py").write_text("print(1)", encoding="utf-8")
    (lib / "manifest.json").write_text(json.dumps([ENTRY]), encoding="utf-8")
    (lib / "secret.md").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(skillservice, "library_root", lambda: lib)

    async def fake_get(name):
        return dict(row) if row and name == row["name"] else None

    async def fake_slugs():
        return ["dash"]

    monkeypatch.setattr(skillservice, "get_skill", fake_get)
    monkeypatch.setattr(skillservice, "enabled_slugs", fake_slugs)
    return calls


async def _use(name, part=None):
    fn = graph.use_skill.coroutine or graph.use_skill.func
    return await fn(name=name, part=part)


async def test_no_part_unchanged(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    r = await _use("dash")
    assert r == skillservice.FRAMING + "\n\nBODY TEXT"
    assert calls[-1]["args"] == {"name": "dash"}


async def test_part_returns_framed_file_and_marks_loaded(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    loaded = []
    tok = graph.ctx_skills_loaded.set(loaded)
    try:
        r = await _use("dash", "recipes/daily.md")
    finally:
        graph.ctx_skills_loaded.reset(tok)
    assert r == skillservice.FRAMING + "\n\nRECIPE"
    assert loaded == ["dash"]
    assert calls[-1]["args"] == {"name": "dash", "part": "recipes/daily.md"}


async def test_part_capped_with_cut_line(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    r = await _use("dash", "recipes/big.md")
    text = r[len(skillservice.FRAMING) + 2:]
    assert len(text) <= 20000
    assert text.endswith("[cut: ask for a narrower part]")


@pytest.mark.parametrize("part", [
    "../SKILL.md", "SKILL.md", "scripts/x.py", "recipes/../../secret.md",
    "/etc/passwd", "recipes"+chr(92)+"daily.md", "recipes/daily.txt", "recipes/a.b.md",
    "recipes/", "", "recipes/daily.md/..", "reference/../recipes/daily.md",
])
async def test_bad_part_refused(monkeypatch, tmp_path, part):
    calls = _wire(monkeypatch, tmp_path)
    r = await _use("dash", part)
    assert r == ("REFUSED: part must be a recipes/ or reference/ .md file "
                 "named in the skill")
    assert calls[-1]["refused"] is True


async def test_missing_file(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    r = await _use("dash", "reference/none.md")
    assert r == "ERROR: part reference/none.md not found in skill dash"


async def test_custom_skill_refused(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, row=dict(ROW, source="custom", origin_ref=None))
    r = await _use("dash", "recipes/daily.md")
    assert r == "REFUSED: dash is a custom skill and has no parts"


async def test_disabled_skill_refused(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, row=dict(ROW, enabled=False))
    r = await _use("dash", "recipes/daily.md")
    assert r.startswith("REFUSED: no enabled skill")


def test_docstring_growth_is_small():
    assert len(graph.use_skill.description) < 700
