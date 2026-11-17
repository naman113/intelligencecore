"""Phase 13 (GOLIVE-08): pure reply matcher and the ten known-answer questions."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "rehearsal_match", BACKEND / "scripts" / "rehearsal_match.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)

QUESTIONS = json.loads((BACKEND / "tests" / "fixtures" / "rehearsal_questions.json")
                       .read_text(encoding="utf-8"))
SEGMENTS = ["Retail", "Hospitality", "Wholesale", "Online"]


@pytest.mark.parametrize("reply,expected,ok", [
    ("We have 224 active customers.", 224, True),
    ("2240 rows", 224, False),
    ("1,224 customers", 224, False),
    ("691 orders in September 2026", 691, True),
    ("There are 2,005 open lines", 2005, True),
    ("No number here", 5, False),
])
def test_match_int(reply, expected, ok):
    assert rm.match_int(reply, expected) is ok


def test_match_text_first_candidate_wins():
    assert rm.match_text("Hospitality pays latest, then Retail.", "Hospitality", SEGMENTS)
    assert not rm.match_text("Retail is fastest; Hospitality is slowest", "Hospitality", SEGMENTS)
    assert rm.match_text("the HOSPITALITY segment", "Hospitality", SEGMENTS)
    assert not rm.match_text("Retailers differ", "Retail", SEGMENTS)
    assert rm.match_text("Dry Goods then Frozen", "Dry Goods", ["Frozen", "Dry Goods"])
    assert not rm.match_text("nothing relevant", "Retail", SEGMENTS)


@pytest.mark.parametrize("reply,ok", [
    ("The unpaid balance is $2,005,218.07.", True),
    ("about 2.01 million", True),
    ("roughly $2.0M", True),
    ("1.9 million", False),
    ("2005k", True),
    ("5 minutes ago", False),
])
def test_match_money(reply, ok):
    assert rm.match_money(reply, 2005218.07) is ok


def test_check_dispatch():
    assert rm.check({"match": "int"}, "84 open", 84)
    assert rm.check({"match": "text"}, "South fell", "South", ["North", "South"])
    assert rm.check({"match": "money", "tolerance": 0.005}, "2.0M", 2005218.07)
    with pytest.raises(ValueError):
        rm.check({"match": "bogus"}, "x", 1)


def test_questions_file():
    assert len(QUESTIONS) == 10
    assert [q["id"] for q in QUESTIONS] == [f"q{i}" for i in range(1, 11)]
    for q in QUESTIONS:
        assert q["match"] in {"int", "text", "money"}
        assert q["sql"].lstrip().upper().startswith("SELECT") and ";" not in q["sql"]
        assert q["ask"].strip()
        if q["match"] == "text":
            assert q["candidates_sql"].startswith("SELECT")
    tracked = [q for q in QUESTIONS if q.get("track_this")]
    assert len(tracked) == 1 and tracked[0]["id"] == "q6"
    assert "status = 'open'" in tracked[0]["sql"]
    assert next(q for q in QUESTIONS if q["id"] == "q10")["tolerance"] == 0.005


def test_matcher_module_is_stdlib_only():
    src = (BACKEND / "scripts" / "rehearsal_match.py").read_text(encoding="utf-8")
    mods = set(re.findall(r"^(?:import|from)\s+(\w+)", src, re.M))
    assert mods <= {"re", "json", "typing"}, mods
