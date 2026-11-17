"""Phase 8: in-app chat channel guards (appchannel, whatsapp, worker)."""
import httpx
import pytest

from app import appchannel, whatsapp, worker


def test_is_app_thread():
    assert appchannel.is_app_thread("ui:owner")
    for t in ("dashboard", "919876543210", None, ""):
        assert not appchannel.is_app_thread(t)


def test_thread_for():
    assert appchannel.thread_for(None) == "ui:owner"
    assert appchannel.thread_for(" Owner ") == "ui:owner"
    assert appchannel.thread_for("ops_lead") == "ui:ops_lead"
    for bad in ("919876543210", "a1", "x" * 33, "1abc", "a b"):
        with pytest.raises(ValueError):
            appchannel.thread_for(bad)
    assert appchannel.thread_for("") == "ui:owner"


async def test_whatsapp_never_sends_for_app_thread(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(httpx, "AsyncClient", boom)
    monkeypatch.setattr(whatsapp, "_record_self", boom, raising=False)
    assert await whatsapp.send_message("ui:owner", "hi") is False
    assert await whatsapp.send_document("ui:owner", "/tmp/x.pdf") is False


async def test_resolve_sender_and_onboarding_skip_app_thread(monkeypatch):
    class Pool:
        def acquire(self):
            raise AssertionError("pool touched")
    assert await worker._resolve_sender(Pool(), "ui:owner") == (None, None)

    from app import chats

    async def boom():
        raise AssertionError("awaiting_answer called")
    monkeypatch.setattr(chats, "awaiting_answer", boom)
    assert await worker._handle_chat_onboarding("ui:owner", "yes") is None


async def test_worker_run_on_app_thread(monkeypatch):
    from tests.test_prompt_layout import FakePool, _wire_worker  # same fakes
    pool = FakePool(thread_id="ui:owner")

    async def ok_run(history, user_message, context_text=""):
        return "Here you go."

    _, reflections = _wire_worker(monkeypatch, pool, ok_run)

    async def no_send(to, text):
        return False
    monkeypatch.setattr(worker.whatsapp, "send_message", no_send)
    assert await worker._process_one() is True
    done = [a for sql, a in pool.executed if "SET status = 'done'" in sql]
    assert done and "WA SEND FAILED" not in done[0][1]
    assert reflections == [{"run_id": 7, "source": "dashboard"}]
