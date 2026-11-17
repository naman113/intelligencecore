"""Pure C-04 date math for dashboard date controls (Phase 22).

The renderer (JavaScript), the digest and the compiled presets SQL all follow
the same rules; the shared vectors live in tests/fixtures/dashboard_date_vectors.json.
Stdlib `datetime` only: no clock, no database. Ranges are inclusive [from, to].
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

PRESETS = ("today", "yesterday", "last_7_days", "last_30_days", "last_90_days",
           "this_month", "last_month", "this_quarter", "this_fy")
PERIODS = ("current", "previous", "last_year")

_ROLLING = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}
_CALENDAR_MONTHS = {"this_month": 1, "this_quarter": 3, "this_fy": 12}


def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _minus_year(d: date) -> date:
    return _add_months(d, -12)  # 29 Feb becomes 28 Feb


def _fy_start(today: date, fy_start_month: int) -> date:
    year = today.year - (1 if today.month < fy_start_month else 0)
    return date(year, fy_start_month, 1)


def _current(preset: str, today: date, fy_start_month: int) -> tuple:
    if preset == "today":
        return today, today
    if preset == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if preset in _ROLLING:
        return today - timedelta(days=_ROLLING[preset] - 1), today
    if preset == "this_month":
        return today.replace(day=1), today
    if preset == "last_month":
        first = today.replace(day=1)
        return _add_months(first, -1), first - timedelta(days=1)
    if preset == "this_quarter":
        return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1), today
    if preset == "this_fy":
        return _fy_start(today, fy_start_month), today
    raise ValueError(f"unknown preset {preset}")


def previous_range(start: date, end: date, preset: str | None = None) -> tuple:
    """Calendar rule for this_month, this_quarter, this_fy, last_month; shift by length otherwise."""
    if preset in _CALENDAR_MONTHS:
        prev_from = _add_months(start, -_CALENDAR_MONTHS[preset])
        prev_to = min(prev_from + (end - start), start - timedelta(days=1))
        return prev_from, prev_to
    if preset == "last_month":
        return _add_months(start, -1), start - timedelta(days=1)
    n = (end - start).days + 1
    return start - timedelta(days=n), end - timedelta(days=n)


def last_year_range(start: date, end: date) -> tuple:
    return _minus_year(start), _minus_year(end)


def preset_range(preset: str, period: str, today: date, fy_start_month: int = 4) -> tuple:
    cur = _current(preset, today, fy_start_month)
    if period == "current":
        return cur
    if period == "previous":
        return previous_range(cur[0], cur[1], preset)
    if period == "last_year":
        return last_year_range(*cur)
    raise ValueError(f"unknown period {period}")


def horizon_start(today: date, days: int) -> date:
    return today - timedelta(days=days - 1)


def bucket_for(start: date, end: date) -> str:
    n = (end - start).days + 1
    if n <= 45:
        return "day"
    if n <= 200:
        return "week"
    return "month"
