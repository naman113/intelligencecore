"""Phase 12 (CTX-08): context-pack-import skill parity and the hide_table executor."""

import json
import re
from pathlib import Path

import pytest

from app import actionsvc, contextstore, skillservice

LIB = skillservice.library_root()
SKILL = LIB / "intelligence-corner" / "context-pack-import" / "SKILL.md"
SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8")
TAG = "$skill_context_pack_import$"
HEADER = "-- Phase 12 (CTX-08)"
END = "-- End Phase 12 (CTX-08)"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _seed() -> str:
    assert SCHEMA.count(TAG) == 2 and SCHEMA.count(END) == 1
    return SCHEMA[SCHEMA.index(HEADER):SCHEMA.index(END)]


def test_frontmatter():
    meta = skillservice.parse_skill_md(_text())
    assert meta["name"] == "context-pack-import"
    d = meta["description"].lower()
    assert "context pack" in d and "proposals only" in d and "template" in d


def test_skill_content():
    t = _text()
    assert 'propose_context_pack_items(filename, "all")' in t
    assert "never page" in t.lower()
    assert "propose_kpi_candidates" in t and "propose_kpi(" not in t
    assert "propose_context_changes" in t and "list_artifacts" in t


def test_manifest_entry():
    data = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    hits = [e for e in data if e["id"] == "intelligence-corner/context-pack-import"]
    assert len(hits) == 1
    e = hits[0]
    assert e["description"] == skillservice.parse_skill_md(_text())["description"]
    assert (e["source"], e["license"], e["compat"], e["relevant"], e["path"]) == (
        "intelligence-corner", "Proprietary", "general", True,
        "intelligence-corner/context-pack-import")


def test_seed_parity_and_disabled():
    s = _seed()
    a = s.index(TAG)
    b = s.index(TAG, a + len(TAG))
    assert s[a + len(TAG):b].replace("\r\n", "\n") == _text().replace("\r\n", "\n")
    m = re.search(r"VALUES\s*\(\s*'context-pack-import',\s*'((?:[^']|'')*)'", s)
    assert m
    assert m.group(1).replace("''", "'") == skillservice.parse_skill_md(_text())["description"]
    assert "FALSE)" in s and "ON CONFLICT (name) DO NOTHING;" in s
    assert "DO UPDATE" not in s.upper()


@pytest.mark.asyncio
async def test_hide_table_executor(monkeypatch):
    calls = []

    async def hid(ref, hidden, decided_by=None):
        calls.append((ref, hidden))
        return {"ref": ref} if ref.endswith("ok") else None

    monkeypatch.setattr(contextstore, "set_table_hidden", hid)
    assert await actionsvc._execute("hide_table", {"ref": "business_db.public.ok"}) is True
    assert await actionsvc._execute("hide_table", {"ref": "business_db.public.gone"}) is False
    assert calls == [("business_db.public.ok", True), ("business_db.public.gone", True)]
    for bad in ({}, {"ref": 5}, {"ref": "other.public.t"}, {"ref": "business_db." + "x" * 400}):
        assert await actionsvc._execute("hide_table", bad) is False
    assert len(calls) == 2
