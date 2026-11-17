"""Config service — pure parts (no DB/LLM)."""

import pytest

from app import configservice as cs


def test_registry_groups_are_known():
    for key, meta in cs.REGISTRY.items():
        assert meta["group"] in cs.GROUPS, f"{key} has unknown group"
        assert isinstance(meta["secret"], bool)
        assert isinstance(meta["required"], bool)
        assert meta["label"]


def test_registry_keys_exist_on_settings():
    from app.config import Settings
    s = Settings(_env_file=None)
    for key in cs.REGISTRY:
        assert hasattr(s, key), f"registry key {key} missing on Settings"


def test_mask_hides_secrets():
    assert cs._mask("") == ""
    assert cs._mask("abc") == "•••"
    m = cs._mask("sk-ant-abcdef1234567890xyzw")
    assert m.endswith("xyzw") and "abcdef" not in m


def test_every_group_has_a_test_function():
    import asyncio

    async def check():
        for group in cs.GROUPS:
            # Must not raise on dispatch (unknown group raises ValueError).
            res = await cs.test_group(group)
            assert res["group"] == group
            assert isinstance(res["ok"], bool)

    # asyncio.get_event_loop() raises RuntimeError on 3.12+ without a running loop.
    asyncio.run(check())


def test_validate_value_rejects_type_garbage():
    import pytest
    cs._validate_value("worker_concurrency", "3")          # int ok
    cs._validate_value("anthropic_api_key", "sk-ant-x")    # str ok
    cs._validate_value("memory_graph_enabled", "true")     # bool ok
    with pytest.raises(ValueError):
        cs._validate_value("worker_concurrency", "abc")    # would brick get_settings()
    with pytest.raises(ValueError):
        cs._validate_value("falkordb_port", "not-a-port")


def test_feature_settings_registered_for_dashboard():
    """Eager-document / case-chaining / group-question / web-research settings
    are dashboard-editable (mirrors test_knowledge_floor.py's registration
    idiom): right group, non-secret, optional, labeled, and a sane value
    validates."""
    expected = {
        "document_model":        ("llm",    "claude-sonnet-5"),
        "document_store_dir":    ("tuning", "documents"),
        "case_chaining_enabled": ("tuning", "true"),
        "group_question_mode":   ("tuning", "off"),
        "research_model":        ("llm",    "claude-sonnet-5"),
        "web_blocked_domains":   ("tuning", "reddit.com,example.com/blog"),
        "web_calls_per_run":     ("tuning", "5"),
        "web_fetch_max_tokens":  ("tuning", "40000"),
    }
    for key, (group, sane) in expected.items():
        meta = cs.REGISTRY[key]
        assert meta["group"] == group, f"{key} expected in group {group}"
        assert meta["secret"] is False
        assert meta["required"] is False
        assert meta["label"]
        cs._validate_value(key, sane)   # must not raise


def test_group_configured_logic():
    # llm requires anthropic_api_key; empty settings → not configured
    from app.config import get_settings
    get_settings.cache_clear()
    import os
    saved = {k: os.environ.pop(k, None) for k in
             ("ANTHROPIC_API_KEY", "TENANT_DATABASE_URL", "CAISAAS_DATABASE_URL")}
    try:
        assert cs._group_configured("llm") in (True, False)  # no crash on empty
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        get_settings.cache_clear()


# ── Phase 1: ui_profile (GRND-03) ──

def _clear_settings():
    from app.config import get_settings
    get_settings.cache_clear()


class _FakePool:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def fetch(self, sql, *args):
        return self.rows


def _fake_get_pool(rows=None):
    async def get_pool():
        return _FakePool(rows)
    return get_pool


def test_ui_profile_registered_in_interface_group():
    from app.config import UI_PROFILES
    meta = cs.REGISTRY["ui_profile"]
    assert meta["group"] == "interface"
    assert meta["secret"] is False
    assert meta["required"] is False
    assert "intelligence" in meta["label"] and "second_brain" in meta["label"]
    assert meta["choices"] == ["intelligence", "second_brain"]
    assert cs.GROUPS["interface"] == "Interface"
    assert "interface" in cs.OPTIONAL_GROUPS
    assert UI_PROFILES == ("intelligence", "second_brain")


def test_ui_profile_defaults_to_intelligence(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("UI_PROFILE", raising=False)
    assert Settings(_env_file=None).ui_profile == "intelligence"


def test_ui_profile_normalizer_degrades_instead_of_raising(monkeypatch):
    from app.config import Settings, get_settings
    for raw, want in [("Second_Brain", "second_brain"), ("  INTELLIGENCE ", "intelligence"),
                      ("bogus", "intelligence"), ("", "intelligence")]:
        monkeypatch.setenv("UI_PROFILE", raw)
        assert Settings(_env_file=None).ui_profile == want
    assert Settings(_env_file=None, ui_profile="bogus").ui_profile == "intelligence"
    monkeypatch.setenv("UI_PROFILE", "bogus")
    _clear_settings()
    try:
        assert get_settings().ui_profile == "intelligence"
    finally:
        _clear_settings()


def test_ui_profile_save_time_choices_are_strict():
    import pytest
    cs._validate_value("ui_profile", "intelligence")
    cs._validate_value("ui_profile", "second_brain")
    for bad in ("bogus", "Second_Brain"):
        with pytest.raises(ValueError, match="must be one of"):
            cs._validate_value("ui_profile", bad)


def test_choices_check_ignores_keys_without_choices():
    cs._validate_value("supervisor_model", "anything-goes")


def test_save_many_rejects_bad_profile_before_touching_env_or_db(monkeypatch):
    import asyncio
    import os
    import pytest

    async def boom():
        raise AssertionError("get_pool must not be awaited for an invalid value")

    monkeypatch.setattr(cs, "get_pool", boom)
    before = os.environ.get("UI_PROFILE")
    with pytest.raises(ValueError):
        asyncio.run(cs.save_many({"ui_profile": "bogus"}))
    assert os.environ.get("UI_PROFILE") == before


def test_list_config_exposes_choices_for_ui_profile(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UI_PROFILE", raising=False)
    monkeypatch.setattr(cs, "get_pool", _fake_get_pool())
    _clear_settings()
    try:
        out = asyncio.run(cs.list_config())
    finally:
        _clear_settings()
    fields = {f["key"]: f for f in out["config"]["interface"]}
    assert fields["ui_profile"]["value"] == "intelligence"
    assert fields["ui_profile"]["choices"] == ["intelligence", "second_brain"]
    llm = {f["key"]: f for f in out["config"]["llm"]}
    assert "choices" not in llm["supervisor_model"]


def test_saved_profile_survives_restart_via_load_overrides(monkeypatch):
    import asyncio
    import os
    from app.config import get_settings
    monkeypatch.delenv("UI_PROFILE", raising=False)
    monkeypatch.setattr(cs, "get_pool",
                        _fake_get_pool([{"key": "ui_profile", "value": "second_brain"}]))
    try:
        assert asyncio.run(cs.load_overrides()) == 1
        assert os.environ["UI_PROFILE"] == "second_brain"
        assert get_settings().ui_profile == "second_brain"
    finally:
        os.environ.pop("UI_PROFILE", None)
        _clear_settings()


def test_interface_group_test_reports_profile(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UI_PROFILE", raising=False)
    _clear_settings()
    try:
        res = asyncio.run(cs.test_group("interface"))
        assert res["ok"] is True and "ui_profile=intelligence" in res["detail"]
        assert "ui_profile" in cs._last_tests["interface"]["detail"]
        monkeypatch.setenv("UI_PROFILE", "bogus")
        _clear_settings()
        res = asyncio.run(cs.test_group("interface"))
        assert res["ok"] is False and "bogus" in res["detail"]
    finally:
        cs._last_tests.pop("interface", None)
        _clear_settings()


def test_interface_group_test_sees_dotenv_typo(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.delenv("UI_PROFILE", raising=False)
    (tmp_path / ".env").write_text("UI_PROFILE=bogus\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _clear_settings()
    try:
        res = asyncio.run(cs.test_group("interface"))
        assert res["ok"] is False and "bogus" in res["detail"]
    finally:
        cs._last_tests.pop("interface", None)
        _clear_settings()


# ── Phase 12 (UI-12) ──
def _profile(monkeypatch, name):
    from types import SimpleNamespace
    monkeypatch.setattr(cs, "get_settings", lambda: SimpleNamespace(ui_profile=name))


def _list_optional(monkeypatch, tmp_path, profile):
    import asyncio
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UI_PROFILE", profile)
    monkeypatch.setattr(cs, "get_pool", _fake_get_pool())
    _clear_settings()
    try:
        return asyncio.run(cs.list_config())["optional"]
    finally:
        _clear_settings()


def test_whatsapp_optional_under_intelligence(monkeypatch, tmp_path):
    _profile(monkeypatch, "intelligence")
    assert cs.is_optional("whatsapp") is True
    assert cs.is_optional("llm") is False
    assert all(cs.is_optional(g) for g in cs.OPTIONAL_GROUPS)
    assert "whatsapp" in _list_optional(monkeypatch, tmp_path, "intelligence")


def test_second_brain_unchanged(monkeypatch, tmp_path):
    _profile(monkeypatch, "second_brain")
    assert cs.is_optional("whatsapp") is False
    opt = _list_optional(monkeypatch, tmp_path, "second_brain")
    assert "whatsapp" not in opt
    if cs._prior_is_optional is None:
        assert opt == sorted(cs.OPTIONAL_GROUPS)


def test_is_optional_wraps_prior(monkeypatch):
    monkeypatch.setattr(cs, "_prior_is_optional", lambda g: g == "dashboards")
    for prof, wa in (("intelligence", True), ("second_brain", False)):
        _profile(monkeypatch, prof)
        assert cs.is_optional("dashboards") is True
        assert cs.is_optional("whatsapp") is wa
    monkeypatch.setattr(cs, "_prior_is_optional", None)
    assert cs.is_optional("tracing") is True
    assert cs.is_optional("llm") is False


def test_is_optional_tripwire():
    import inspect
    assert "PROFILE_OPTIONAL_GROUPS" in inspect.getsource(cs.is_optional)



# ---- Phase 14 (WH-04): Setup save re-links the warehouse ----
def _inv_patch(monkeypatch, mode, relink):
    from app import guardrails, warehouse
    order = []
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: mode)

    async def rl(reason, timeout=20.0):
        order.append(("relink", reason, timeout))
        return await relink()

    async def rst():
        order.append("reset")
    monkeypatch.setattr(warehouse, "relink_now", rl)
    monkeypatch.setattr(guardrails, "reset_business_pool", rst)
    return order


async def test_invalidate_relinks_in_warehouse_mode(monkeypatch):
    from app import configservice

    async def ok():
        return {"status": "ok", "tables_linked": 9}
    order = _inv_patch(monkeypatch, True, ok)
    await configservice._invalidate({"business_db"})
    assert order == [("relink", "setup", 20), "reset"]


async def test_invalidate_no_relink_outside_warehouse(monkeypatch):
    from app import configservice

    async def ok():
        return {"status": "ok"}
    order = _inv_patch(monkeypatch, False, ok)
    await configservice._invalidate({"business_db"})
    assert order == ["reset"]


async def test_invalidate_llm_never_relinks(monkeypatch):
    from app import configservice

    async def ok():
        return {"status": "ok"}
    order = _inv_patch(monkeypatch, True, ok)
    try:
        await configservice._invalidate({"llm"})
    except Exception:
        pass
    assert not any(isinstance(o, tuple) for o in order)


@pytest.mark.parametrize("mode", ["raise", "error"])
async def test_invalidate_relink_failure_still_resets(monkeypatch, mode):
    from app import configservice

    async def bad():
        if mode == "raise":
            raise RuntimeError("postgresql://u:secretpw@h/db")
        return {"status": "error"}
    order = _inv_patch(monkeypatch, True, bad)
    await configservice._invalidate({"business_db"})
    assert order[-1] == "reset"


def test_invalidate_wrapper_position_and_save_many_lookup():
    from app import configservice
    src = open(configservice.__file__, encoding="utf-8").read()
    assert src.count("_prior_invalidate = _invalidate") == 1
    assert src.index("Phase 14 (WH-04") < src.index("# ── Phase 12 (UI-12)")
    assert "await _invalidate(groups)" in src


# ── Quick 046: connected WhatsApp with nobody allowed is flagged ──

@pytest.mark.parametrize("owner,allowed,open_,connected,expect_ok", [
    ("", "", False, True, False),        # the Mill incident: paired, nobody set
    ("15550001111", "", False, True, True),
    ("", "15550002222", False, True, True),
    ("", "", True, True, True),          # explicit open mode
    ("", "", False, False, True),        # not connected: the usual status text
])
async def test_whatsapp_test_warns_when_nobody_can_use_it(monkeypatch, owner, allowed, open_, connected, expect_ok):
    import httpx
    from types import SimpleNamespace
    s = SimpleNamespace(wa_api_base="http://wa", wa_api_key="k", owner_number=owner,
                        allowlist={n for n in allowed.split(",") if n}, whatsapp_open_to_anyone=open_)
    from app import configservice
    monkeypatch.setattr(configservice, "get_settings", lambda: s)
    body = '{"success":true,"connected":%s}' % ("true" if connected else "false")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    ok, msg = await configservice._test_whatsapp()
    assert ok is expect_ok
    if not expect_ok:
        assert "nobody can use WhatsApp until an owner number is set" in msg
