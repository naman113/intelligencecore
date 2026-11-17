"""Phase 8: /health profile, dashboards_public_url, relabel."""
import os

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import configservice
from app.config import Settings, get_settings


async def test_health_shapes(monkeypatch):
    monkeypatch.setitem(main.APP_STATE, "db_ready", True)
    assert (await main.health())["ui_profile"] == "intelligence"
    monkeypatch.setitem(main.APP_STATE, "db_ready", False)
    out = await main.health()
    assert out["degraded"] is True and out["ui_profile"] == "intelligence"


def test_health_follows_config_no_key():
    old = os.environ.get("UI_PROFILE")
    try:
        os.environ["UI_PROFILE"] = "second_brain"
        get_settings.cache_clear()
        r = TestClient(main.app).get("/health")
        assert r.status_code == 200 and r.json()["ui_profile"] == "second_brain"
    finally:
        if old is None:
            os.environ.pop("UI_PROFILE", None)
        else:
            os.environ["UI_PROFILE"] = old
        get_settings.cache_clear()


@pytest.mark.parametrize("raw,want", [
    ("https://dash.example.com/", "https://dash.example.com"),
    ("  http://localhost:9082  ", "http://localhost:9082"),
    ("javascript:alert(1)", ""), ("ftp://x", ""), ("dash.example.com", "")])
def test_public_url_normalizes(raw, want):
    assert Settings(dashboards_public_url=raw).dashboards_public_url == want


def test_registry():
    e = configservice.REGISTRY["dashboards_public_url"]
    assert e["group"] == "interface" and not e["secret"] and not e["required"]
    label = configservice.REGISTRY["ui_profile"]["label"]
    assert "takes effect once" not in label
    assert "intelligence" in label and "second_brain" in label
