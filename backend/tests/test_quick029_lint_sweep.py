"""Quick 029 F4: the read-only lint sweep over fake API responses."""
import importlib.util
import json
from pathlib import Path

from _lintclean import BAD_PAGE, QUERIES, clean_page

from app import dashboardlint

_spec = importlib.util.spec_from_file_location(
    "dashboard_lint_sweep", Path(__file__).resolve().parents[1] / "scripts" / "dashboard_lint_sweep.py")
sweepmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweepmod)


def test_sweep_reports_only_failing_published_and_retired():
    pages = {("a", 1): (BAD_PAGE, "retired"), ("a", 2): (clean_page(), "published"),
             ("a", 3): (BAD_PAGE, "draft"), ("b", 1): (BAD_PAGE, "published")}
    calls = []

    def get(path):
        calls.append(path)
        if path == "/api/dashboards":
            return [{"slug": "a"}, {"slug": "b"}]
        parts = path.strip("/").split("/")
        if len(parts) == 3:
            return {"versions": [{"version": n, "status": st}
                                 for (s, n), (_, st) in pages.items() if s == parts[2]]}
        code, st = pages[(parts[2], int(parts[4]))]
        return {"code": code, "status": st, "queries": json.dumps(QUERIES)}

    out = sweepmod.sweep(get, dashboardlint.lint)
    assert [(f["slug"], f["version"], f["status"]) for f in out] == [("a", 1, "retired"), ("b", 1, "published")]
    assert all(f["rules"] == ["tags"] for f in out)
    assert not any("/versions/3" in c for c in calls)     # drafts are never fetched
    assert all("code" not in f for f in out)


def test_sweep_clean_is_empty():
    def get(path):
        if path == "/api/dashboards":
            return [{"slug": "a"}]
        if path.count("/") == 3:
            return {"versions": [{"version": 1, "status": "published"}]}
        return {"code": clean_page(), "queries": QUERIES}
    assert sweepmod.sweep(get, dashboardlint.lint) == []
