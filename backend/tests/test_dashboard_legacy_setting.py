"""Phase 18 (SPEC-06): the dashboard_legacy_html setting and the HTML-draft gate.
Fake connections only; no DB, LLM or network."""

import re
from pathlib import Path

import pytest

from app import config, configservice
from app import dashboards as d
from app.dashboards import DashboardError

SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text(encoding="utf-8")


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class Conn:
    """Answers the SQL the gate and the two publish paths run."""

    def __init__(self, is_html, status="draft"):
        self.is_html = is_html
        self.status = status
        self.sql = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "(spec IS NULL) AS is_html" in sql:
            return {"is_html": self.is_html}
        if "FROM dashboards d" in sql:
            return {"slug": "sales", "version": 2}
        if "FOR UPDATE" in sql:
            return {"id": 7, "slug": "sales", "title": "S", "current_version_id": None}
        if "FROM dashboard_versions" in sql:
            return {"id": 11, "version": 2, "title": "S", "status": self.status,
                    "change_note": None}
        return None

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return 5

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return "UPDATE 1"


def _payload():
    return {"slug": "sales", "dashboard_id": 7, "version_id": 11, "version": 2}


@pytest.fixture
def published(monkeypatch):
    calls = []

    async def fake_publish(conn, **kw):
        calls.append(kw)
        return {"changed": True, "from_version_id": None, "from_version": None}

    monkeypatch.setattr(d, "publish", fake_publish)
    return calls


def test_setting_defaults_off_and_reads_env(monkeypatch):
    monkeypatch.delenv("DASHBOARD_LEGACY_HTML", raising=False)
    config.get_settings.cache_clear()
    assert config.Settings(_env_file=None).dashboard_legacy_html is False
    monkeypatch.setenv("DASHBOARD_LEGACY_HTML", "true")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().dashboard_legacy_html is True
        assert d.legacy_html_on() is True
    finally:
        monkeypatch.delenv("DASHBOARD_LEGACY_HTML", raising=False)
        config.get_settings.cache_clear()


def test_registry_entry():
    e = configservice.REGISTRY["dashboard_legacy_html"]
    assert e["group"] == "interface" and e["secret"] is False and e["required"] is False
    label = e["label"].lower()
    assert "true/false" in label and "default false" in label and "applied live" in label
    assert "t-12-51" in label


def test_legacy_html_on_false_when_settings_raise(monkeypatch):
    def boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr(config, "get_settings", boom)
    assert d.legacy_html_on() is False


async def test_proposal_for_html_draft_refused_while_off(monkeypatch, published):
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    with pytest.raises(DashboardError) as ei:
        await d.apply_publish_proposal(Conn(is_html=True), _payload())
    assert ei.value.status == 409 and "convert it" in ei.value.args[0]
    assert published == []


async def test_proposal_for_html_draft_publishes_while_on(monkeypatch, published):
    monkeypatch.setattr(d, "legacy_html_on", lambda: True)
    assert await d.apply_publish_proposal(Conn(is_html=True), _payload()) is True
    assert len(published) == 1


async def test_proposal_for_spec_draft_publishes_while_off(monkeypatch, published):
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    assert await d.apply_publish_proposal(Conn(is_html=False), _payload()) is True
    assert len(published) == 1


async def test_operator_publish_html_refused_while_off(monkeypatch, published):
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(Conn(is_html=True), slug="sales", version=2,
                                     op="publish", decided_by=5)
    assert ei.value.status == 409
    assert published == []


async def test_operator_publish_spec_ok_and_rollback_html_ok_while_off(monkeypatch, published):
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    async def decider(conn, decided_by=None):
        return 5
    monkeypatch.setattr(d, "resolve_decider", decider)
    await d.operator_set_version(Conn(is_html=False), slug="sales", version=2,
                                 op="publish", decided_by=5)
    await d.operator_set_version(Conn(is_html=True, status="retired"), slug="sales",
                                 version=2, op="rollback", decided_by=5)
    assert len(published) == 2


def test_schema_block_markers_and_guards():
    assert SCHEMA.count("-- ── Phase 18: chart-spec dashboards") == 1
    assert SCHEMA.count("-- End Phase 18: chart-spec dashboards") == 1
    assert SCHEMA.index("-- End Phase 16: semantic model") < SCHEMA.index("-- ── Phase 18")
    block = SCHEMA[SCHEMA.index("-- ── Phase 18"):]
    assert "ADD COLUMN IF NOT EXISTS spec JSONB" in block
    assert "ADD COLUMN IF NOT EXISTS renderer TEXT" in block
    assert re.search(r"IF NOT EXISTS \(SELECT 1 FROM pg_constraint WHERE conname = "
                     r"'dashboard_versions_spec_shape'", block)
    assert "CREATE OR REPLACE FUNCTION dashboard_versions_spec_immutable()" in block
    assert re.search(r"IF NOT EXISTS \(SELECT 1 FROM pg_trigger WHERE tgname = "
                     r"'trg_dashboard_versions_spec_immutable'", block)


def test_phase3_trigger_function_unchanged():
    m = re.search(r"CREATE OR REPLACE FUNCTION dashboard_versions_immutable\(\).*?LANGUAGE plpgsql;",
                  SCHEMA, re.S)
    assert m
    body = m.group(0)
    assert "NEW.queries, NEW.change_note, NEW.author, NEW.task_run_id, NEW.created_at" in body
    assert "spec" not in body and "renderer" not in body
