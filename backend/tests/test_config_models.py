"""Phase 13 (GOLIVE-01): model/effort registry, list_config provenance, per-role probe."""
import asyncio
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import configservice as cs
from app import llmclient

ROLE_MODELS = ["supervisor_model", "onboarding_model", "context_model",
               "document_model", "research_model", "triage_model"]
EFFORTS = [m.replace("_model", "_effort") for m in ROLE_MODELS]


def _clear():
    from app.config import get_settings
    get_settings.cache_clear()


class _Pool:
    def __init__(self, keys):
        self.keys = keys

    async def fetch(self, sql, *a):
        return [{"key": k} for k in self.keys]


def _list(monkeypatch, tmp_path, db_keys=()):
    async def gp():
        return _Pool(db_keys)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cs, "get_pool", gp)
    _clear()
    try:
        out = asyncio.run(cs.list_config())
    finally:
        _clear()
    return out


def test_registry_keys_and_options():
    for k in ROLE_MODELS:
        m = cs.REGISTRY[k]
        assert m["group"] == "llm" and m["label"]
        assert m["options"] == llmclient.model_options()
        assert "choices" not in m
    for k in EFFORTS:
        m = cs.REGISTRY[k]
        assert m["group"] == "llm" and m["label"]
        assert m["choices"] == ["low", "medium", "high", "xhigh", "max"]
    keys = list(cs.REGISTRY)
    ctx = keys.index("context_pack_timeout_seconds")
    assert keys.index("onboarding_model") > ctx
    assert all(keys.index(e) > ctx for e in EFFORTS)
    assert keys.index("supervisor_model") < keys.index("triage_model") < keys.index("document_model")


def test_last_phase_header_is_ui12():
    src = Path(cs.__file__).read_text(encoding="utf-8")
    assert re.findall(r"# ── Phase [^\n]*", src)[-1].startswith("# ── Phase 12 (UI-12)")


def test_validate_values():
    with pytest.raises(ValueError):
        cs._validate_value("supervisor_effort", "bogus")
    cs._validate_value("supervisor_effort", "xhigh")
    cs._validate_value("research_model", "claude-future-9-9")
    with pytest.raises(ValueError):
        cs._validate_value("research_model", "bad id; drop")
    with pytest.raises(ValueError):
        cs._validate_value("research_model", "x" * 101)


def test_list_config_source_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_MODEL", raising=False)
    monkeypatch.delenv("RESEARCH_MODEL", raising=False)
    out = _list(monkeypatch, tmp_path, db_keys={"research_model"})
    llm = {f["key"]: f for f in out["config"]["llm"]}
    s = llm["supervisor_model"]
    assert (s["value"], s["default"], s["source"]) == ("claude-opus-5-5", "claude-opus-5-5", "default")
    assert s["options"]
    assert llm["research_model"]["source"] == "db"
    assert llm["supervisor_effort"]["choices"][0] == "low"
    for g in out["config"].values():
        for f in g:
            assert "default" in f
            if f["secret"]:
                assert f["default"] == ""


def test_list_config_env_source(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERVISOR_MODEL", "claude-sonnet-5-5")
    out = _list(monkeypatch, tmp_path)
    s = {f["key"]: f for f in out["config"]["llm"]}["supervisor_model"]
    assert s["source"] == "env" and s["value"] == "claude-sonnet-5-5"


def _fake_anthropic(monkeypatch, fail=None):
    calls = []

    class Msgs:
        async def create(self, **kw):
            calls.append(kw)
            if fail and fail[0] == kw["model"]:
                raise Exception(fail[1])
            return SimpleNamespace()

    class Client:
        def __init__(self, **kw):
            self.messages = Msgs()

    mod = ModuleType("anthropic")
    mod.AsyncAnthropic = Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def _settings(**over):
    d = dict(anthropic_api_key="k", openai_api_key="",
             supervisor_model="claude-opus-5-5", onboarding_model="claude-opus-5-5",
             context_model="claude-opus-5-5", document_model="claude-sonnet-5-5",
             research_model="claude-sonnet-5-5", triage_model="claude-haiku-5-5",
             supervisor_effort="medium", onboarding_effort="high", context_effort="medium",
             document_effort="high", research_effort="medium", triage_effort="low")
    d.update(over)
    return SimpleNamespace(**d)


def test_probe_one_call_per_distinct_pair(monkeypatch):
    calls = _fake_anthropic(monkeypatch)
    ok, detail = asyncio.run(cs._probe_llm_roles(_settings()))
    assert ok and len(calls) == 5
    assert all(c["output_config"]["effort"] for c in calls)
    assert "supervisor claude-opus-5-5 (medium) ok" in detail
    assert "triage claude-haiku-5-5 (low) ok" in detail


def test_probe_retention_failure(monkeypatch):
    _fake_anthropic(monkeypatch, fail=("claude-fable-5-1",
        "In order to access this model, your organization or workspace must have data retention enabled."))
    ok, detail = asyncio.run(cs._probe_llm_roles(_settings(research_model="claude-fable-5-1")))
    assert not ok
    assert "research claude-fable-5-1 (medium) FAILED: needs 30-day data retention on the Anthropic account" in detail


def test_probe_other_failure_and_no_effort(monkeypatch):
    _fake_anthropic(monkeypatch, fail=("claude-haiku-4-5", "boom " * 100))
    ok, detail = asyncio.run(cs._probe_llm_roles(_settings(triage_model="claude-haiku-4-5")))
    assert not ok
    assert "triage claude-haiku-4-5 (no effort) FAILED: Exception: boom" in detail


def test_test_llm_branches(monkeypatch):
    calls = _fake_anthropic(monkeypatch)
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    assert asyncio.run(cs._test_llm())[0] and calls
    monkeypatch.setattr(cs, "get_settings", lambda: _settings(anthropic_api_key=""))
    assert asyncio.run(cs._test_llm()) == (False, "no LLM key set")
