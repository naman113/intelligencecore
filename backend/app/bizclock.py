"""Quick 049: the one clock the agent and triage share.

"Today" means the BUSINESS day: the calendar day in the operator `timezone`
setting (an IANA name, e.g. Asia/Kolkata), not UTC and not the owner's own zone.
This module only computes dates and times (plumbing); what to DO with them
("today so far" vs the last full day, naming the business date) is prompt
guidance in agents/graph.py (GROUNDING_RULES) and triage.py.

zoneinfo (Python docs, https://docs.python.org/3/library/zoneinfo.html):
`datetime.now(ZoneInfo(name))` is the aware local time and `astimezone(ZoneInfo)`
converts an aware UTC instant; both follow the IANA rules, DST included. Date
arithmetic goes through `.date() - timedelta(days=1)`, so "yesterday" is a calendar
day and never "now minus 24 h". Unknown names raise ZoneInfoNotFoundError, which
falls back to UTC here, as kpihistory._tz does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import get_settings

# The business day counts as "just started" for its first 6 hours (00:00-05:59).
# Before 06:00 local almost no shift has booked anything, so "today so far" is a
# near-empty slice; after 6 hours there is enough of a day to be a fair answer.
JUST_STARTED_HOURS = 6


def tz_name() -> str:
    """Operator timezone if it is a valid IANA zone, else UTC."""
    try:
        name = (get_settings().timezone or "UTC").strip() or "UTC"
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


def local_now(now: datetime | None = None, tz: str | None = None) -> datetime:
    """The aware business-local time. `now` (aware, or naive meaning UTC) is for tests."""
    zone = ZoneInfo(tz or tz_name())
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone)


def day_just_started(local: datetime) -> bool:
    return local.hour < JUST_STARTED_HOURS


def _fmt_day(d) -> str:
    return f"{d.isoformat()} ({d.strftime('%a')})"


def clock_line(now: datetime | None = None, tz: str | None = None) -> str:
    """One-line clock block for the agent's context. Facts only."""
    zone = tz or tz_name()
    local = local_now(now, zone)
    utc = local.astimezone(timezone.utc)
    today = local.date()
    abbr = local.tzname() or zone
    line = (f"CLOCK: business timezone {zone}. Now {local.strftime('%Y-%m-%d %H:%M')} {abbr} "
            f"({local.strftime('%A')}) = {utc.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Business today = {_fmt_day(today)}; last full business day = "
            f"{_fmt_day(today - timedelta(days=1))}.")
    if day_just_started(local):
        mins = local.hour * 60 + local.minute
        line += (f" The business day only started {mins // 60}h{mins % 60:02d}m ago "
                 f"(under {JUST_STARTED_HOURS}h): day_just_started = true.")
    else:
        line += f" day_just_started = false (the day began {local.hour}h ago)."
    return line


def business_now_text(now: datetime | None = None, tz: str | None = None) -> str:
    """Triage's "now" anchor: business-local time first, UTC for reference."""
    zone = tz or tz_name()
    local = local_now(now, zone)
    utc = local.astimezone(timezone.utc)
    return (f"{local.strftime('%Y-%m-%dT%H:%M:%S')} {zone} ({local.strftime('%A')}); "
            f"business date {local.date().isoformat()}; UTC {utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
