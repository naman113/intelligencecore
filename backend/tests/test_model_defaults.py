"""Phase 13 (GOLIVE-01): locked model and effort defaults."""
from pathlib import Path

import pytest

from app import llmclient
from app.config import Settings

MODELS = {"supervisor": "claude-opus-5-5", "onboarding": "claude-opus-5-5",
          "context": "claude-opus-5-5", "document": "claude-haiku-5-5",  # Quick 022
          "research": "claude-sonnet-5-5", "triage": "claude-haiku-5-5",
          "data": "claude-sonnet-5-5",  # Quick 012
          "reflection": "claude-haiku-5-5", "graph": "claude-sonnet-5-5"}  # Quick 022


@pytest.fixture
def clean(monkeypatch):
    for r in llmclient.ROLES:
        monkeypatch.delenv(f"{r.upper()}_MODEL", raising=False)
        monkeypatch.delenv(f"{r.upper()}_EFFORT", raising=False)
    return monkeypatch


def test_model_defaults(clean):
    s = Settings(_env_file=None)
    for role, model in MODELS.items():
        assert getattr(s, f"{role}_model") == model


def test_effort_defaults(clean):
    s = Settings(_env_file=None)
    assert {r: getattr(s, f"{r}_effort") for r in llmclient.ROLES} == llmclient.ROLE_EFFORT_FALLBACK


def test_defaults_not_clamped_and_current(clean):
    s = Settings(_env_file=None)
    current = {m["value"] for m in llmclient.CURRENT_MODELS}
    for role in llmclient.ROLES:
        model, eff = getattr(s, f"{role}_model"), getattr(s, f"{role}_effort")
        assert model in current
        assert llmclient.effort_for(model, eff) == eff


def test_env_overrides_effort(clean):
    clean.setenv("SUPERVISOR_EFFORT", "xhigh")
    assert Settings(_env_file=None).supervisor_effort == "xhigh"


def test_env_example_unpinned():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert not [ln for ln in text.splitlines() if ln.startswith(("SUPERVISOR_MODEL=", "REFLECTION_MODEL="))]


def test_reactive_token_budget_default(clean):
    clean.delenv("REACTIVE_TOKEN_BUDGET", raising=False)
    assert Settings(_env_file=None).reactive_token_budget == 300_000
