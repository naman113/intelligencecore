"""Phase 22 (DCAP-08), real Postgres: SET LOCAL TimeZone does not leak and the
transaction stays read-only. Skipped unless DASH_TEST_DATABASE_URL is set."""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)


@pytest.fixture
async def direct(monkeypatch):
    from app import guardrails
    url = os.environ["DASH_TEST_DATABASE_URL"]
    monkeypatch.setattr(guardrails, "get_settings", lambda: type("S", (), {
        "tenant_database_url": url, "business_database_url": "", "sql_row_limit": 100})())
    monkeypatch.setattr(guardrails.warehouse, "warehouse_mode", lambda: False)
    await guardrails.reset_business_pool()
    yield guardrails
    await guardrails.reset_business_pool()


async def test_timezone_applies_and_does_not_leak(direct):
    sql = ("SELECT current_setting('TimeZone') AS tz, "
           "current_setting('transaction_read_only') AS ro")
    rows = await direct.run_readonly_sql(sql, timezone="Asia/Kolkata")
    assert rows[0]["tz"] == "Asia/Kolkata" and rows[0]["ro"] == "on"
    after = await direct.run_readonly_sql(sql)
    assert after[0]["tz"] != "Asia/Kolkata" and after[0]["ro"] == "on"
