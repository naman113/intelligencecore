"""Phase 14 (WH-03, T-14-23): static proof that every tenant read uses guardrails."""
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
CONSUMERS = ["agents/graph.py", "sentinel.py", "connectors/db_poller.py", "dashboard_refresh.py",
             "dashboardtools.py", "insights.py", "investigation.py", "schemadiscovery.py",
             "healthwatch.py"]
URL_ALLOWED = {"config.py", "configservice.py", "guardrails.py", "healthwatch.py",
               "dashboard_refresh.py", "warehouse.py", "secretbox.py"}
# whlink receives the URL as an argument; readonlykit is handed a connection
CONNECT_ALLOWED = {"db.py", "guardrails.py", "warehouse.py", "configservice.py", "readonlykit.py"}


def _src(rel):
    return (APP / rel).read_text(encoding="utf-8")


def test_consumers_exist_and_never_open_connections():
    for rel in CONSUMERS:
        s = _src(rel)
        assert "asyncpg.connect(" not in s, rel
        assert "asyncpg.create_pool(" not in s, rel


def test_consumers_that_read_tenant_data_use_run_readonly_sql():
    # modules that query the business DB reference the guardrails entry point
    readers = ["agents/graph.py", "sentinel.py", "connectors/db_poller.py", "dashboard_refresh.py",
               "schemadiscovery.py"]
    for rel in readers:
        s = _src(rel)
        assert "run_readonly_sql" in s or "guardrails" in s, rel


def test_tenant_url_names_only_in_allowed_modules():
    bad = []
    for p in APP.rglob("*.py"):
        s = p.read_text(encoding="utf-8")
        if re.search(r"tenant_database_url|business_database_url", s) and p.name not in URL_ALLOWED:
            bad.append(p.relative_to(APP).as_posix())
    assert not bad, bad


def test_connections_opened_only_in_allowed_modules():
    bad = []
    for p in APP.rglob("*.py"):
        s = p.read_text(encoding="utf-8")
        if re.search(r"asyncpg\.(connect|create_pool)\(|\bcreate_pool\(", s) and p.name not in CONNECT_ALLOWED:
            bad.append(p.relative_to(APP).as_posix())
    assert not bad, bad
