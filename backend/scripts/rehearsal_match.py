"""Pure reply matcher for the Phase 13 dress rehearsal (GOLIVE-08).

Standard library only. Decides whether an Ask reply contains the expected
answer, so the rehearsal driver never needs an LLM to grade a reply.
"""
import re
from typing import List, Optional

_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(thousand|million|billion|bn|k|m)\b)?(?![\w])",
    re.I,
)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def numbers_in(text: str) -> List[float]:
    """Numbers in text: thousands separators removed, k/m/million/bn applied."""
    out = []
    for m in _NUM.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        if m.group(2):
            value *= _MULT[m.group(2).lower()]
        out.append(value)
    return out


_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
    re.I,
)


def match_int(reply: str, expected: int) -> bool:
    # Dates are stripped first so "2026-10-08" cannot satisfy an expected 8.
    return any(v == float(expected) for v in numbers_in(_DATE.sub(" ", reply or "")))


def match_text(reply: str, expected: str, candidates: Optional[List[str]] = None) -> bool:
    """True when the earliest-mentioned candidate is the expected one."""
    low = (reply or "").lower()
    names = list(candidates or [])
    if expected not in names:
        names.append(expected)
    best_pos, best = None, None
    for name in names:
        m = re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", low)
        if m and (best_pos is None or m.start() < best_pos):
            best_pos, best = m.start(), name
    return best is not None and best.lower() == expected.lower()


def match_money(reply: str, expected: float, tol: float = 0.005) -> bool:
    if not expected:
        return any(v == 0 for v in numbers_in(reply))
    return any(abs(v - expected) / abs(expected) <= tol for v in numbers_in(reply))


def check(question: dict, reply: str, expected, candidates: Optional[List[str]] = None) -> bool:
    rule = question.get("match")
    if rule == "int":
        return match_int(reply, int(expected))
    if rule == "text":
        return match_text(reply, str(expected), candidates)
    if rule == "money":
        return match_money(reply, float(expected), question.get("tolerance", 0.005))
    raise ValueError(f"unknown match rule: {rule!r}")
