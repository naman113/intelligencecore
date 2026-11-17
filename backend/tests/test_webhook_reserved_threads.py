"""Phase 8 (WR-05): the WhatsApp webhook refuses ui:* and internal thread ids."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main
from app import appchannel


class _Req:
    def __init__(self, payload, secret="s"):
        self._payload = payload
        self.headers = {"x-webhook-secret": secret}

    async def json(self):
        return self._payload


def _boom():
    raise AssertionError("db touched")


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(webhook_secret="s", allowlist=set(), whatsapp_open_to_anyone=True))
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=_boom))


def _call(payload, secret="s"):
    return asyncio.run(main.whatsapp_webhook(_Req(payload, secret), x_webhook_secret=secret))


@pytest.mark.parametrize("v", ["ui:owner", "UI:Owner", "  ui:alice ", "dashboard", "Inbox"])
def test_reserved_true(v):
    assert appchannel.is_reserved_thread(v) is True


@pytest.mark.parametrize("v", ["15551234567", "120363000000@g.us", "", None])
def test_reserved_false(v):
    assert appchannel.is_reserved_thread(v) is False


@pytest.mark.parametrize("payload", [
    {"phone": "ui:owner", "message": "hi"},
    {"phone": "UI:Owner", "message": "hi"},
    {"phone": "15550000000", "chatId": "ui:owner", "isGroup": True, "message": "hi"},
    {"phone": "dashboard", "message": "hi"},
    {"phone": "inbox", "message": "hi"},
])
def test_webhook_refuses_reserved(payload):
    with pytest.raises(HTTPException) as e:
        _call(payload)
    assert e.value.status_code == 422
    assert e.value.detail == "reserved thread id"


def test_secret_first():
    with pytest.raises(HTTPException) as e:
        _call({"phone": "ui:owner", "message": "hi"}, secret="wrong")
    assert e.value.status_code == 401


def test_normal_phone_unaffected():
    with pytest.raises(HTTPException) as e:
        _call({"phone": "15550000000", "message": ""})
    assert e.value.status_code == 422
    assert e.value.detail == "phone and message (or document) required"
