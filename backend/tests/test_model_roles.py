"""Phase 13 (GOLIVE-01): every Anthropic call site carries its role's effort."""
import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app import llmclient

APP = Path(__file__).resolve().parent.parent / "app"


def _s(**kw):
    base = dict(anthropic_api_key="k", openai_api_key="", supervisor_model="claude-opus-5-5",
                triage_model="claude-haiku-5-5", document_model="claude-sonnet-5-5",
                research_model="claude-sonnet-5-5", onboarding_model="claude-opus-5-5",
                context_model="claude-opus-5-5", supervisor_effort="medium",
                triage_effort="low", document_effort="high", research_effort="medium",
                onboarding_effort="high", context_effort="high",
                openai_supervisor_model="gpt-4o")
    base.update(kw)
    return SimpleNamespace(**base)


def _payload(model):
    return model._get_request_payload([HumanMessage("x")])


def _eff(p):
    return (p.get("output_config") or {}).get("effort")


def test_sentinel_playbooks_draft(monkeypatch):
    from app import playbooks, sentinel
    for mod in (sentinel, playbooks):
        monkeypatch.setattr(mod, "get_settings", lambda: _s())
    p = _payload(sentinel._model(1200))
    assert _eff(p) == "medium" and p["max_tokens"] >= 16000
    p = _payload(playbooks._model())
    assert _eff(p) == "medium" and p["max_tokens"] >= 16000


def test_triage_and_poller(monkeypatch):
    from app import triage
    from app.connectors import db_poller
    monkeypatch.setattr(triage, "get_settings", lambda: _s())
    from app import config
    monkeypatch.setattr(config, "get_settings", lambda: _s())
    assert _eff(_payload(triage._model())) == "low"
    assert _eff(_payload(db_poller._cheap_model())) == "low"


def test_graph_model_and_make_model(monkeypatch):
    from app.agents import graph
    monkeypatch.setattr(graph, "get_settings", lambda: _s())
    assert _eff(_payload(graph._model())) == "medium"
    cfg = {"model": "auto", "temperature": 0.2, "max_tokens": 4096}
    p = _payload(graph._make_model(cfg, role="supervisor"))
    assert _eff(p) == "medium" and p["max_tokens"] == 16000 and "temperature" not in p
    p = _payload(graph._make_model({**cfg, "model": "claude-opus-5-5"}, role="context"))
    assert _eff(p) == "high"
    p = _payload(graph._make_model({**cfg, "model": "claude-sonnet-4-5"}, role="supervisor"))
    assert "output_config" not in p and p["max_tokens"] == 4096 and p["temperature"] == 0.2


def test_settings_without_effort_fields_use_fallback(monkeypatch):
    from app import triage
    bare = SimpleNamespace(anthropic_api_key="k", triage_model="claude-haiku-5-5",
                           openai_api_key="")
    monkeypatch.setattr(triage, "get_settings", lambda: bare)
    assert _eff(_payload(triage._model())) == llmclient.ROLE_EFFORT_FALLBACK["triage"]


class _Client:
    calls: list = []

    def __init__(self, *a, **k):
        self.messages = self

    async def create(self, **kw):
        _Client.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")],
                               stop_reason="end_turn")


@pytest.fixture
def fake_anthropic(monkeypatch):
    _Client.calls = []
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(AsyncAnthropic=_Client))
    return _Client


@pytest.mark.asyncio
async def test_layerlens(monkeypatch, fake_anthropic):
    from app import layerlens
    monkeypatch.setattr(layerlens, "get_settings", lambda: _s())
    try:
        await layerlens.llm_group_layers(["A", "B"])
    except Exception:
        pass
    kw = fake_anthropic.calls[0]
    assert kw["output_config"] == {"effort": "low"} and kw["max_tokens"] == 16000
    assert kw["model"] == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_research_retry(monkeypatch, fake_anthropic):
    from app.agents import graph
    monkeypatch.setattr(graph, "get_settings", lambda: _s())
    n = {"i": 0}

    async def create(self, **kw):
        _Client.calls.append(kw)
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("bad allowed_callers")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               stop_reason="end_turn")
    monkeypatch.setattr(_Client, "create", create)
    try:
        await graph._server_tool_call([{"type": "web_search_20250305", "name": "web_search"}], "q")
    except Exception:
        pass
    assert len(fake_anthropic.calls) == 2
    assert fake_anthropic.calls[0]["output_config"] == {"effort": "medium"}
    assert fake_anthropic.calls[0]["max_tokens"] == 16000
    assert fake_anthropic.calls[1]["tools"][0]["allowed_callers"] == ["direct"]
    assert fake_anthropic.calls[1]["output_config"] == {"effort": "medium"}


def test_legacy_raw_kwargs():
    kw = llmclient.messages_kwargs("claude-sonnet-4-5", role="document", max_tokens=8192,
                                   settings=_s())
    assert "output_config" not in kw and kw["max_tokens"] == 8192
    kw = llmclient.messages_kwargs("claude-sonnet-5-5", role="document", max_tokens=8192,
                                   settings=_s())
    assert kw["output_config"] == {"effort": "high"} and kw["max_tokens"] == 16000
    kw = llmclient.messages_kwargs("claude-opus-5-5", role="onboarding", max_tokens=16000,
                                   settings=_s(), output_config={"format": {"type": "json_schema"}})
    assert kw["output_config"]["effort"] == "high" and "format" in kw["output_config"]
    assert kw["max_tokens"] <= 21000


def _calls():
    for f in APP.rglob("*.py"):
        if f.name == "llmclient.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                yield f, n, name


def test_ast_every_llm_call_has_role():
    bad = [f"{f.name}:{n.lineno}" for f, n, name in _calls()
           if name in ("chat_anthropic", "messages_kwargs")
           and "role" not in {k.arg for k in n.keywords}]
    assert not bad, bad


def test_ast_no_direct_model_in_messages_create():
    bad = [f"{f.name}:{n.lineno}" for f, n, name in _calls()
           if name == "create" and f.name != "configservice.py"
           and isinstance(n.func, ast.Attribute)
           and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "messages"
           and "model" in {k.arg for k in n.keywords}]
    assert not bad, bad
