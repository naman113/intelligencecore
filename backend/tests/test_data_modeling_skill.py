"""Phase 16 (MODEL-06): data-modeling skill parse, manifest, disabled seed parity."""

import hashlib
import json
import re
from pathlib import Path

from app import skillservice

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = ROOT / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md"
MANIFEST = ROOT / "skills_library" / "manifest.json"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
HEADER = "-- Phase 16: semantic model (MODEL-01, MODEL-06)"
END = "-- End Phase 16: semantic model"
TAG = "$skill_data_modeling$"


def _text() -> str:
    return SKILL_PATH.read_bytes().decode("ascii").replace("\r\n", "\n")


def _block() -> str:
    return SCHEMA[SCHEMA.index(HEADER):SCHEMA.index(END)]


def test_parses_and_is_clean():
    p = skillservice.parse_skill_md(_text())
    assert p["name"] == "data-modeling"
    assert len(p["description"]) <= 200
    assert not set(p["description"]) & set("#`<>")
    assert SKILL_PATH.stat().st_size < 16384


def test_body_terms():
    t = _text()
    for w in ("get_model", "propose_model_change", "propose_relationships_from_keys", "query_measure",
              "run_sql", "unverified", "NOT MODELED", "at most 12", "agg_time_dimension",
              "non_additive_dimension", "offset_window", "ratio", "derived", "match rate",
              "untrusted", "approves the relationships", "no path", "role-playing",
              "whether or not", "data agent"):
        assert w in t, w


def test_manifest_entry():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [e["id"] for e in data]
    assert ids.count("intelligence-corner/data-modeling") == 1
    i = ids.index("intelligence-corner/data-modeling")
    assert ids[i - 1] == "intelligence-corner/dashboard-authoring"
    e = data[i]
    assert (e["name"], e["source"], e["license"], e["compat"], e["relevant"], e["path"]) == (
        "data-modeling", "intelligence-corner", "Proprietary", "general", True,
        "intelligence-corner/data-modeling")
    assert e["description"] == skillservice.parse_skill_md(_text())["description"]


def test_seed_parity_and_disabled():
    b = _block()
    assert SCHEMA.count(TAG) == 2
    body = re.search(r"\$skill_data_modeling\$(.*?)\$skill_data_modeling\$", b, re.S).group(1)
    assert body.rstrip() == _text().rstrip()
    desc = skillservice.parse_skill_md(_text())["description"]
    assert f"'{desc.replace(chr(39), chr(39) * 2)}'" in b
    m = re.search(r"INSERT INTO skills.*?ON CONFLICT \(name\) DO NOTHING;", b, re.S)
    assert m and "'library', 'intelligence-corner/data-modeling', 'Proprietary', 'general', FALSE" in m.group(0)


def _md5(s: str) -> str:
    return hashlib.md5(s.replace(chr(13), "").strip(" " + chr(10) + chr(9)).encode()).hexdigest()


def test_answering_numbers_label_is_unconditional():
    t = _text()
    assert "If it answers NOT MODELED, either propose the missing measure or answer with run_sql and say plainly that the number is unverified." not in t
    sec = t[t.index("## Answering numbers"):t.index("## Item shapes")]
    assert "unverified" in sec and "whether or not" in sec


def test_refresh_matches_file_and_fixture_hash():
    b = _block()
    assert SCHEMA.count("$p16_data_modeling$") == 2
    m = re.search(r"UPDATE skills SET body = \$p16_data_modeling\$(.*?)\$p16_data_modeling\$(.*?);", b, re.S)
    assert m and m.group(1).rstrip() == _text().rstrip()
    stmt = m.group(0)
    assert "WHERE name = 'data-modeling'" in stmt and "enabled" not in stmt
    fx = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v1.md").read_bytes().decode("ascii")
    v1 = "2e58f0e3ada2f8f473345c501c51694d"
    assert _md5(fx) == v1
    assert _md5(_text()) != v1
    assert v1 in b
    assert b.index("$p16_data_modeling$") > b.index("$p16_schema_doc$")
