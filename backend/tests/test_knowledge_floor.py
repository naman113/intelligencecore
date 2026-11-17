"""Score floor + payload caps on knowledge.search_business_context (over a fake
contextstore), and the config plumbing for knowledge_score_floor /
reactive_token_budget."""

import types

import pytest

from app import knowledge


def _fake_settings(**over):
    base = dict(knowledge_score_floor=0.30)
    base.update(over)
    return types.SimpleNamespace(**base)


def _patch(monkeypatch, hits, **settings):
    async def search(query, limit=8):
        return hits

    monkeypatch.setattr(knowledge.contextstore, "search", search)
    monkeypatch.setattr(knowledge, "get_settings", lambda: _fake_settings(**settings))


def _hit(score, **payload):
    return {"score": score, **payload}


async def test_below_floor_hits_dropped(monkeypatch):
    _patch(monkeypatch, [_hit(0.9, text="keep-high"), _hit(0.2, text="drop")])
    out = await knowledge.search_business_context("q")
    assert [h["text"] for h in out] == ["keep-high"]


async def test_floor_boundary_kept(monkeypatch):
    _patch(monkeypatch, [_hit(0.30, text="at"), _hit(0.29, text="below")])
    out = await knowledge.search_business_context("q")
    assert [h["text"] for h in out] == ["at"]


async def test_floor_setting_is_respected(monkeypatch):
    _patch(monkeypatch, [_hit(0.9, text="a"), _hit(0.6, text="b")], knowledge_score_floor=0.85)
    out = await knowledge.search_business_context("q")
    assert [h["text"] for h in out] == ["a"]


async def test_long_payload_strings_truncated(monkeypatch):
    _patch(monkeypatch, [_hit(0.9, text="x" * 5000, short="ok")])
    out = await knowledge.search_business_context("q")
    assert len(out[0]["text"]) == 1000
    assert out[0]["short"] == "ok"
    assert out[0]["score"] == 0.9


async def test_payload_key_count_capped(monkeypatch):
    payload = {f"k{i}": f"v{i}" for i in range(12)}
    _patch(monkeypatch, [_hit(0.9, **payload)])
    out = await knowledge.search_business_context("q")
    assert len(set(out[0]) - {"score"}) == 8


async def test_non_string_values_pass_through(monkeypatch):
    _patch(monkeypatch, [_hit(0.9, n=42, arr=[1, 2, 3])])
    out = await knowledge.search_business_context("q")
    assert out[0]["n"] == 42 and out[0]["arr"] == [1, 2, 3]


async def test_none_score_kept(monkeypatch):
    _patch(monkeypatch, [_hit(None, text="fts")])
    out = await knowledge.search_business_context("q")
    assert len(out) == 1 and out[0]["text"] == "fts"


async def test_empty_store_returns_hint(monkeypatch):
    _patch(monkeypatch, [])
    out = await knowledge.search_business_context("q")
    assert len(out) == 1
    assert "score" not in out[0]
    assert out[0]["text"] == knowledge.EMPTY_HINT


async def test_all_below_floor_returns_empty_list(monkeypatch):
    _patch(monkeypatch, [_hit(0.1, text="a")])
    assert await knowledge.search_business_context("q") == []


async def test_kpi_hit_contract(monkeypatch):
    _patch(monkeypatch, [_hit(0.8, type="kpi", kpi_name="Revenue", text="sum of paid")])
    out = await knowledge.search_business_context("q", limit=12)
    assert out[0]["kpi_name"] == "Revenue" and out[0]["text"] == "sum of paid"


# ── config plumbing ─────────────────────────────────────────────────────


def test_new_settings_defaults():
    from app.config import Settings
    f = Settings.model_fields["knowledge_score_floor"]
    assert f.annotation is float and f.default == pytest.approx(0.30)
    f = Settings.model_fields["reactive_token_budget"]
    assert f.annotation is int and f.default == 300_000


def test_new_settings_registered_for_dashboard():
    from app import configservice as cs
    for key, group in [("knowledge_score_floor", "tuning"),
                       ("reactive_token_budget", "tuning")]:
        meta = cs.REGISTRY[key]
        assert meta["group"] == group
        assert meta["secret"] is False
        assert meta["required"] is False
        assert meta["label"]
        cs._validate_value(key, "0.5" if key == "knowledge_score_floor" else "100000")
    with pytest.raises(ValueError):
        cs._validate_value("reactive_token_budget", "abc")
