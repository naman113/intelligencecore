"""Phase 22 (DCAP-08/03/14): settings, normalisers, REGISTRY entries, dashsettings helpers."""
import pytest

from app import config, configservice, dashsettings


def _settings(**kw):
    return config.Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _clear():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_defaults():
    s = _settings()
    assert s.dashboard_number_locale == ""
    assert s.dashboard_local_days is True
    assert s.dashboard_naive_timestamps == "local"
    assert s.dashboard_snapshot_rows == 10000
    assert s.dashboard_snapshot_bytes == 2097152
    assert s.app_public_url == ""


@pytest.mark.parametrize("bad", ['https://a"b.com', "http://exa<mple.com", "https://a b.com", "http://[::1]",
                                 "https://my_host.com", "https://h:123456", "https://h:99999", "https://"])
def test_app_public_url_contract_regex(bad):
    assert _settings(app_public_url=bad).app_public_url == ""


def test_app_public_url_lowercases_scheme():
    assert _settings(app_public_url="HTTPS://App.Example.com:8443/").app_public_url == "https://App.Example.com:8443"


def test_normalisers():
    assert _settings(dashboard_naive_timestamps="UTC ").dashboard_naive_timestamps == "utc"
    assert _settings(dashboard_naive_timestamps="bogus").dashboard_naive_timestamps == "local"
    assert _settings(dashboard_number_locale="en-in").dashboard_number_locale == ""
    assert _settings(dashboard_number_locale="en-GB").dashboard_number_locale == "en-GB"
    assert _settings(app_public_url="https://app.example.com/").app_public_url == "https://app.example.com"
    assert _settings(app_public_url="javascript:x").app_public_url == ""
    assert _settings(app_public_url="https://a.b/path").app_public_url == ""
    assert _settings(app_public_url="http://localhost:8080").app_public_url == "http://localhost:8080"
    assert _settings(dashboard_snapshot_rows=50000).dashboard_snapshot_rows == 10000
    assert _settings(dashboard_snapshot_rows=10).dashboard_snapshot_rows == 1000
    assert _settings(dashboard_snapshot_bytes=1).dashboard_snapshot_bytes == 262144
    assert _settings(dashboard_snapshot_bytes=10**9).dashboard_snapshot_bytes == 2097152


def test_registry_entries():
    r = configservice.REGISTRY
    for k in ("dashboard_number_locale", "dashboard_local_days", "dashboard_naive_timestamps",
              "dashboard_snapshot_rows", "dashboard_snapshot_bytes"):
        assert r[k]["group"] == "tuning"
    assert r["app_public_url"]["group"] == "interface"
    assert r["dashboard_naive_timestamps"]["choices"] == ["local", "utc"]
    for k in ("dashboard_snapshot_rows", "dashboard_snapshot_bytes"):
        assert "min" in r[k] and "max" in r[k]
    assert "applied live" in r["dashboard_local_days"]["label"]


def _set(monkeypatch, **kw):
    st = _settings(**kw)
    monkeypatch.setattr(dashsettings, "get_settings", lambda: st)
    monkeypatch.setattr(dashsettings.kpihistory, "get_settings", lambda: st)


def test_day_timezone(monkeypatch):
    _set(monkeypatch, timezone="Asia/Kolkata")
    assert dashsettings.day_timezone() == "Asia/Kolkata"
    _set(monkeypatch, timezone="Asia/Kolkata", dashboard_local_days=False)
    assert dashsettings.day_timezone() is None
    _set(monkeypatch, timezone="UTC")
    assert dashsettings.day_timezone() is None


def test_number_locale(monkeypatch):
    for tz, loc in [("Asia/Kolkata", "en-IN"), ("Asia/Calcutta", "en-IN"),
                    ("Europe/London", "en-GB"), ("Asia/Dubai", "en-AE"),
                    ("Asia/Singapore", "en-SG"), ("America/New_York", "en-US")]:
        _set(monkeypatch, timezone=tz)
        assert dashsettings.number_locale() == loc
    _set(monkeypatch, timezone="Asia/Kolkata", dashboard_number_locale="en-GB")
    assert dashsettings.number_locale() == "en-GB"


def test_caps_and_flags(monkeypatch):
    _set(monkeypatch, dashboard_snapshot_rows=5000, dashboard_naive_timestamps="utc",
         app_public_url="https://x.io/")
    assert dashsettings.snapshot_caps() == (5000, 2097152)
    assert dashsettings.naive_utc() is True
    assert dashsettings.ask_base() == "https://x.io"


def test_locales_equal_dashboardspec():
    spec = pytest.importorskip("app.dashboardspec")
    if not hasattr(spec, "LOCALES"):
        pytest.skip("dashboardspec.LOCALES not present")
    assert tuple(spec.LOCALES) == dashsettings.LOCALES == config.DASHBOARD_LOCALES
