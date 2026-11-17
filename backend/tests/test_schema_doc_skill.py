"""schema-documentation first-party skill: parse, manifest entry, disabled seed, drift."""

import hashlib
import json
import re
from pathlib import Path

from app import skillservice

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = ROOT / "skills_library" / "intelligence-corner" / "schema-documentation" / "SKILL.md"
MANIFEST = ROOT / "skills_library" / "manifest.json"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
START = "Phase 2: Context store"
END = "End Phase 2: Context store"


def _text() -> str:
    return SKILL_PATH.read_bytes().decode("ascii").replace("\r\n", "\n")


def _block() -> str:
    s = [m.start() for m in re.finditer(re.escape(START), SCHEMA)
         if not SCHEMA[m.start() - 4:m.start()].endswith("End ")]
    assert len(s) == 1
    return SCHEMA[s[0]:SCHEMA.index(END)]


def test_skill_parses_and_is_clean():
    p = skillservice.parse_skill_md(_text())
    assert p["name"] == "schema-documentation"
    assert p["license"] == "Proprietary"
    assert len(p["description"]) <= 200
    assert not set(p["description"]) & set("#`<>")
    assert SKILL_PATH.stat().st_size < 16 * 1024


def test_skill_caps_and_tables_arg():
    t = _text()
    m = re.search(r"at most (\d+) tables per message", t)
    assert m and 1 <= int(m.group(1)) <= 3
    assert re.search(r"at most (\d+) tables per run", t)
    assert "tables=" in t
    for word in ("DELETE", "CASCADE", "CONCURRENTLY", "DROP"):
        assert word not in t.upper()


def test_manifest_entry():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    hits = [e for e in data if e["id"] == "intelligence-corner/schema-documentation"]
    assert len(hits) == 1
    e = hits[0]
    assert (e["name"], e["source"], e["license"], e["compat"], e["relevant"], e["path"]) == (
        "schema-documentation", "intelligence-corner", "Proprietary", "general", True,
        "intelligence-corner/schema-documentation")
    ids = [x["id"] for x in data]
    assert ids == sorted(ids)


def test_seed_row_is_disabled_and_idempotent():
    b = _block()
    m = re.search(r"INSERT INTO skills.*?ON CONFLICT \(name\) DO NOTHING;", b, re.S)
    assert m
    ins = m.group(0)
    assert "'schema-documentation'" in ins
    assert "'library', 'intelligence-corner/schema-documentation', 'Proprietary', 'general', FALSE" in ins


def test_seed_body_and_description_equal_file():
    b = _block()
    body = re.search(r"\$skill_schema_doc\$(.*?)\$skill_schema_doc\$", b, re.S).group(1)
    assert body.rstrip() == _text().rstrip()
    desc = skillservice.parse_skill_md(_text())["description"]
    assert f"'{desc.replace(chr(39), chr(39) * 2)}'" in b


# Phase 16: two lanes and the md5-guarded refresh
P16_START = "-- Phase 16: semantic model (MODEL-01, MODEL-06)"
P16_END = "-- End Phase 16: semantic model"
GUARDED = {"8888fca53fb9e75022f8377f0856f8e9", "69621a748ad5ba47bf18cd9b7095d8e0"}


def _md5(s: str) -> str:
    return hashlib.md5(s.replace(chr(13), "").strip(" " + chr(10) + chr(9)).encode()).hexdigest()


def test_two_lanes_text():
    t = _text()
    for w in ("Two lanes", "query_measure", "unverified"):
        assert w in t


def test_phase16_refresh_matches_file_and_fixture_hash():
    b = SCHEMA[SCHEMA.index(P16_START):SCHEMA.index(P16_END)]
    assert b.count("$p16_schema_doc$") == 2
    body = re.search(r"\$p16_schema_doc\$(.*?)\$p16_schema_doc\$", b, re.S).group(1)
    assert body.rstrip() == _text().rstrip()
    fx = (Path(__file__).parent / "fixtures" / "schema_doc_skill_v1.md").read_bytes().decode("utf-8")
    assert _md5(fx) == "8888fca53fb9e75022f8377f0856f8e9"
    assert _md5(_text()) not in GUARDED
    for h in GUARDED:
        assert h in b
