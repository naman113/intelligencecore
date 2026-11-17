"""Quick 040: publish() runs the Quick 029 F4 HTML lint on HTML versions only.

Chart-spec versions (Phase 18) carry measure-bound queries with keys the HTML lint's
`queries` rule refuses ({id, sql, refresh_minutes?} only), so linting them blocked every
spec publish ("fails the dashboard lint (rules: queries, size)"). Spec versions are
validated when saved (prepare_spec) and are immutable, so publish skips the HTML lint
for them; HTML versions are still linted exactly as before.
"""
import json

import pytest

from app import dashboards

SPEC_QUERIES = [{"id": "m_total_orders", "sql": "SELECT * FROM (SELECT COUNT(*) AS n) q",
                 "refresh_minutes": 60, "measure": "order_count", "source": "measure",
                 "compiled": {"measure": "order_count", "grain": None}}]


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Conn:
    def __init__(self, *, is_spec: bool):
        self.is_spec = is_spec
        self.executed = []

    def transaction(self):
        return Tx()

    async def fetchrow(self, sql, *a):
        if "FROM dashboards WHERE id" in sql:
            return {"id": 1, "current_version_id": 10}
        if "FROM dashboard_versions" in sql and "RETURNING" not in sql:
            row = {"id": 11, "version": 2, "title": "Orders health", "status": "draft",
                   "code": None if self.is_spec else "<html><body>x</body></html>",
                   "queries": json.dumps(SPEC_QUERIES)}
            if "is_spec" in sql:
                row["is_spec"] = self.is_spec
            return row
        if "RETURNING" in sql:
            return {"id": 10, "version": 1}
        return None

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return "UPDATE 1"


async def test_spec_version_publishes_without_the_html_lint(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the HTML lint must not run on a spec version")
    monkeypatch.setattr(dashboards, "lint_refusal", boom)
    conn = Conn(is_spec=True)
    out = await dashboards.publish(conn, dashboard_id=1, version_id=11)
    assert out["version"] == 2
    assert any("status = 'published'" in s for s in conn.executed)


async def test_html_version_is_still_linted(monkeypatch):
    seen = []

    def refuse(code, queries):
        seen.append(code)
        return "fails the dashboard lint (rules: queries): bad"
    monkeypatch.setattr(dashboards, "lint_refusal", refuse)
    conn = Conn(is_spec=False)
    with pytest.raises(dashboards.DashboardError) as e:
        await dashboards.publish(conn, dashboard_id=1, version_id=11)
    assert e.value.status == 422 and "cannot publish version 2" in str(e.value)
    assert seen == ["<html><body>x</body></html>"]
    assert not any("status = 'published'" in s for s in conn.executed)
