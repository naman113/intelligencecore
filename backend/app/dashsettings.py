"""Phase 22: dashboard settings in one place.

Pure helpers over get_settings() and kpihistory._tz(); no database access. The
timezone the dashboard day buckets follow, the number locale, the v2 snapshot caps,
the naive-timestamp rule and the app address for the "Ask about this number" link.
"""
from .config import DASHBOARD_LOCALES as LOCALES, get_settings
from . import kpihistory

# One list: config.DASHBOARD_LOCALES (dashboardspec imports the same tuple).

_ZONE_LOCALE = {
    "Asia/Kolkata": "en-IN",
    "Asia/Calcutta": "en-IN",
    "Europe/London": "en-GB",
    "Asia/Dubai": "en-AE",
    "Asia/Singapore": "en-SG",
}


def day_timezone() -> str | None:
    """Zone name day buckets are computed in, or None when the database's own zone
    applies (dashboard_local_days off, or the operator zone is UTC)."""
    if not get_settings().dashboard_local_days:
        return None
    tz = kpihistory._tz()
    return None if tz == "UTC" else tz


def number_locale() -> str:
    """The configured locale, else one derived from the operator zone."""
    s = (get_settings().dashboard_number_locale or "").strip()
    if s in LOCALES:
        return s
    return _ZONE_LOCALE.get(kpihistory._tz(), "en-US")


def snapshot_caps() -> tuple[int, int]:
    """(rows, bytes) one v2 dashboard snapshot may hold."""
    s = get_settings()
    return int(s.dashboard_snapshot_rows), int(s.dashboard_snapshot_bytes)


def naive_utc() -> bool:
    """True when timestamps without a zone are read as UTC instead of local time."""
    return get_settings().dashboard_naive_timestamps == "utc"


def ask_base() -> str:
    """Normalised app_public_url, or "" for no link."""
    return get_settings().app_public_url or ""
