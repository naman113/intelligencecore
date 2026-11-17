"""Quick 004 pins for the analytics skills (forecasting, statistical-tests,
anomaly-detection, trend-and-seasonality, driver-analysis), the refreshed
exploratory-data-analysis and dashboard-authoring bodies, the sandbox libraries
they rely on, and the run_sql_to_csv export cap. Skills are data: seeded
installed-but-disabled by the Quick 004 block of schema.sql."""

import ast
import importlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import guardrails, sandbox, skillservice
from app.agents import graph
from app.guardrails import validate_readonly_sql

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root()
SLUGS = ("forecasting", "statistical-tests", "anomaly-detection", "trend-and-seasonality",
         "driver-analysis")
TAGS = {"forecasting": "$skill_forecasting$", "statistical-tests": "$skill_statistical_tests$",
        "anomaly-detection": "$skill_anomaly_detection$",
        "trend-and-seasonality": "$skill_trend_seasonality$",
        "driver-analysis": "$skill_driver_analysis$"}
REFRESH = {"exploratory-data-analysis": "$q004_eda$",
           "dashboard-authoring": "$q004_dashboard_authoring$"}
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
HEADER = "-- ── Quick 004: analytics skills ──"
END = "-- End Quick 004: analytics skills"
SANDBOX_LIBS = {"numpy", "pandas", "scipy", "statsmodels", "sklearn", "matplotlib", "warnings"}


def _text(slug: str) -> str:
    p = LIB / "intelligence-corner" / slug / "SKILL.md"
    assert p.is_file(), f"missing {p}"
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _blocks(text: str, lang: str) -> list[str]:
    return re.findall(r"```" + re.escape(lang) + r"[ \t]*\n(.*?)```", text, re.DOTALL)


def _block() -> str:
    assert SCHEMA.count(HEADER) == 1 and SCHEMA.count(END) == 1
    a, b = SCHEMA.index(HEADER), SCHEMA.index(END)
    assert b > a
    return SCHEMA[a:b]


def _between(s: str, tag: str) -> str:
    assert s.count(tag) == 2, f"{tag} must occur exactly twice"
    a = s.index(tag)
    return s[a + len(tag):s.index(tag, a + len(tag))]


# ── the skill files ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug", SLUGS)
def test_skill_parses_with_owner_language_description(slug):
    p = skillservice.parse_skill_md(_text(slug))
    assert p["name"] == slug and p["license"] == "Proprietary"
    d = p["description"]
    assert 1 <= len(d) <= 200 and d == skillservice.sanitize_desc(d)
    for ch in ("#", "`", "<", ">", "'"):
        assert ch not in d, ch
    assert "Use for" in d


@pytest.mark.parametrize("slug", SLUGS)
def test_skill_is_ascii_compact_and_url_free(slug):
    raw = (LIB / "intelligence-corner" / slug / "SKILL.md").read_bytes()
    t = raw.decode("ascii")
    # Quick 030: forecasting also carries the model data-access section, so its cap is 14000.
    cap = 14000 if slug == "forecasting" else 12000
    assert len(raw) < cap, "keep each skill compact: it is loaded into the agent's context"
    for bad in ("http://", "https://", "www.", "$skill_", "$q004_"):
        assert bad not in t, bad
    assert not re.search(r"\b(DELETE|DROP|INSERT|UPDATE|TRUNCATE|ALTER|GRANT)\b", t)


@pytest.mark.parametrize("slug", SLUGS)
def test_skill_sections_and_tool_steps(slug):
    t = _text(slug)
    for h in ("## When to use", "## Who runs what", "## Budget", "## Reporting", "## Safety"):
        assert h in t, h
    for term in ("run_sql", "run_sql_to_csv", "run_python", "delegate_to_data_agent",
                 "delegate_to_analysis_agent", f"({slug})", "SELECT *",
                 "Quote only numbers from tool output"):
        assert term in t, term


@pytest.mark.parametrize("slug", SLUGS)
def test_sql_examples_pass_the_guardrails(slug):
    for sql in _blocks(_text(slug), "sql"):
        validate_readonly_sql(sql)
        assert "now()" not in sql.lower() and "current_date" not in sql.lower()


@pytest.mark.parametrize("slug", SLUGS)
def test_python_blocks_parse_and_use_only_sandbox_libraries(slug):
    blocks = _blocks(_text(slug), "python")
    assert blocks, "each analytics skill carries at least one script"
    for code in blocks:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [(node.module or "").split(".")[0]]
            for m in mods:
                assert m in SANDBOX_LIBS, f"{slug}: {m} is not in the sandbox image"
        assert "/work/" in code


def test_methods_the_task_asked_for_are_taught():
    need = {
        "forecasting": ("seasonal_naive", "moving_avg", "ETSModel", "ARIMA", "acorr_ljungbox",
                        "MAPE", "MAE", "80", "TOO SHORT", "AT TIME ZONE", "generate_series",
                        "never a fact", "Simple forecast"),
        "statistical-tests": ("equal_var=False", "mannwhitneyu", "kruskal", "chi2_contingency",
                              "fisher_exact", "multipletests", "Hedges", "Cramer", "95 percent",
                              "chance"),
        "anomaly-detection": ("STL", "1.4826", "3.5", "IsolationForest", "percentile_cont",
                              "false alarm"),
        "trend-and-seasonality": ("STL", "year-on-year", "level shift", "season removed",
                                  "binary segmentation"),
        "driver-analysis": ("pct_of_change", "GROUPING SETS", "count(DISTINCT o.id)", "spearman", "smf.ols", "HC3",
                            "variance_inflation_factor", "permutation_importance",
                            "association, not cause"),
    }
    for slug, terms in need.items():
        t = _text(slug)
        for term in terms:
            assert term in t, (slug, term)


def test_volume_mix_rate_split_reconciles():
    """The driver-analysis split (volume, mix, rate) must add up to the change."""
    t = _text("driver-analysis")
    assert "volume = (Q1 - Q0) x sum(m0 x p0)" in t and "rate = sum(q1 x (p1 - p0))" in t
    q0, p0, q1, p1 = [10, 30, 60], [5.0, 2.0, 1.0], [12, 20, 80], [5.5, 2.5, 0.9]
    Q0, Q1 = sum(q0), sum(q1)
    m0, m1 = [q / Q0 for q in q0], [q / Q1 for q in q1]
    volume = (Q1 - Q0) * sum(a * b for a, b in zip(m0, p0))
    mix = Q1 * sum((a - b) * c for a, b, c in zip(m1, m0, p0))
    rate = sum(a * (b - c) for a, b, c in zip(q1, p1, p0))
    r0, r1 = sum(a * b for a, b in zip(q0, p0)), sum(a * b for a, b in zip(q1, p1))
    assert volume + mix + rate == pytest.approx(r1 - r0)


# ── the refreshed first-party skills ────────────────────────────────────────

def test_eda_points_at_every_analytics_skill():
    t = _text("exploratory-data-analysis")
    assert "## Deeper methods" in t and "statsmodels" in t
    for slug in SLUGS:
        assert slug in t, slug


def test_dashboard_authoring_teaches_a_sql_forecast_panel():
    t = _text("dashboard-authoring")
    assert "## Forecast panel" in t
    # Phase 22: SKILL.md points at reference/forecast-panel.md, which holds the one SQL panel
    sec = t[t.index("## Forecast panel"):t.index("\n## ", t.index("## Forecast panel") + 5)]
    assert 'part="reference/forecast-panel.md"' in sec
    ref = (LIB / "intelligence-corner" / "dashboard-authoring" / "reference"
           / "forecast-panel.md").read_text(encoding="utf-8")
    sqls = _blocks(ref, "sql")
    assert len(sqls) == 1
    validate_readonly_sql(sqls[0])
    for term in ("regr_slope", "regr_intercept", "generate_series", "stddev_samp", "1.28",
                 "AS week", "AS actual", "AS forecast", "AS low", "AS high"):
        assert term in sqls[0], term
    for term in ("Simple forecast", "moving average", "52 weeks", '"dashed": true', '"band"'):
        assert term in ref, term


def test_forecasting_sends_dashboards_to_the_dashboard_agent():
    t = _text("forecasting")
    sec = t[t.index("## Dashboard forecast panel"):]
    assert "dashboard-authoring" in sec and "cannot run Python" in sec


# ── manifest, fetch script, seeds ───────────────────────────────────────────

def test_manifest_entries():
    data = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    ids = [e["id"] for e in data]
    assert ids == sorted(ids)
    for slug in SLUGS:
        eid = f"intelligence-corner/{slug}"
        hits = [e for e in data if e["id"] == eid]
        assert len(hits) == 1
        e = hits[0]
        assert (e["name"], e["source"], e["license"], e["compat"], e["relevant"], e["path"]) == (
            slug, "intelligence-corner", "Proprietary", "general", True, eid)
        assert e["description"] == skillservice.parse_skill_md(_text(slug))["description"]


def test_library_search_finds_each_skill_by_owner_words():
    for words, slug in (("forecast", "forecasting"), ("significant", "statistical-tests"),
                        ("unusual", "anomaly-detection"), ("seasonal", "trend-and-seasonality"),
                        ("drives", "driver-analysis")):
        assert f"intelligence-corner/{slug}" in {h["id"] for h in skillservice.search_library(words)}


def test_relevant_slugs_list_all():
    src = (ROOT / "backend" / "scripts" / "fetch_skills_library.py").read_text(encoding="utf-8")
    for slug in SLUGS:
        assert f'"intelligence-corner/{slug}"' in src


@pytest.mark.parametrize("slug", SLUGS)
def test_seed_rows_disabled_idempotent_and_equal_to_files(slug):
    s = _block()
    tail = f"'library', 'intelligence-corner/{slug}', 'Proprietary', 'general', FALSE)"
    assert tail in s
    assert s[s.index(tail) + len(tail):].lstrip().startswith("ON CONFLICT (name) DO NOTHING;")
    assert _between(SCHEMA, TAGS[slug]).strip() == _text(slug).strip()
    d = skillservice.parse_skill_md(_text(slug))["description"]
    assert f"VALUES ('{slug}', '{d}', {TAGS[slug]}" in s


def test_block_never_enables_and_refresh_is_guarded():
    s = _block()
    assert "DO UPDATE" not in s and "enabled = TRUE" not in s and ", TRUE)" not in s
    assert "SET enabled" not in s
    assert s.count("UPDATE skills SET ") == len(REFRESH)
    for slug, tag in REFRESH.items():
        head = f"UPDATE skills SET body = {tag}"
        b = s.index(tag, s.index(head) + len(head)) + len(tag)
        stmt = s[b:s.index(");", b) + 2]
        assert stmt.startswith(f", updated_at = now()\n WHERE name = '{slug}' AND ")
        assert "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN (" in stmt
        hashes = re.findall(r"'([0-9a-f]{32})'", stmt)
        assert hashes, "refresh needs the md5 of at least one earlier shipped body"
        import hashlib
        cur = hashlib.md5(_text(slug).replace("\r", "").strip(" \n\t").encode()).hexdigest()
        assert cur not in hashes, "the current body must not be in the earlier-versions list"
        assert _between(SCHEMA, tag).strip() == _text(slug).strip(), f"regenerate the {slug} refresh"


def test_block_comes_after_the_skills_table_and_scope():
    assert SCHEMA.index(HEADER) > SCHEMA.index("ALTER TABLE skills ADD COLUMN IF NOT EXISTS scope")


# ── sandbox image and tool text ─────────────────────────────────────────────

def test_sandbox_image_pins_the_analytics_libraries():
    # Quick 042: the package list moved to sandbox/requirements.txt, shared by
    # both images (Dockerfile and Dockerfile.service), so the pins live there.
    df = (ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    df += (ROOT / "sandbox" / "requirements.txt").read_text(encoding="utf-8")
    assert "statsmodels==0.15.0" in df and "scikit-learn==1.9.1" in df
    for lib in ("pandas", "numpy", "scipy", "matplotlib"):
        assert lib in df
    assert "FROM python:3.12-slim" in df and "USER runner" in df
    assert "OMP_NUM_THREADS=1" in df


def test_tool_text_lists_the_real_libraries():
    doc = graph.run_python.description
    for lib in ("scipy", "statsmodels", "scikit-learn"):
        assert lib in doc
    assert "NOT installed" not in doc
    p = graph.DEFAULT_PROMPTS["analysis"]
    assert "statsmodels" in p and "scikit-learn" in p and "not available" not in p


def test_sandbox_image_env_override(monkeypatch):
    try:
        monkeypatch.setenv("SANDBOX_IMAGE", "agentic-sandbox:ic-test")
        assert importlib.reload(sandbox).SANDBOX_IMAGE == "agentic-sandbox:ic-test"
        monkeypatch.delenv("SANDBOX_IMAGE")
        assert importlib.reload(sandbox).SANDBOX_IMAGE == "agentic-sandbox:latest"
    finally:
        monkeypatch.delenv("SANDBOX_IMAGE", raising=False)
        importlib.reload(sandbox)


# ── the export cap ──────────────────────────────────────────────────────────

@pytest.fixture
def csv_env(monkeypatch, tmp_path):
    seen = {}

    async def fake_run(sql, limit=None):
        seen["limit"] = limit
        return [{"period": i, "value": i * 2} for i in range(min(limit, seen["n"]))]

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(graph, "run_readonly_sql", fake_run)
    monkeypatch.setattr(graph, "_log_tool", no_log)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(sql_row_limit=5, sql_csv_row_limit=10))
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    return seen


async def test_csv_export_uses_its_own_cap_and_says_when_cut(csv_env, tmp_path):
    csv_env["n"] = 50
    out = await graph.run_sql_to_csv.ainvoke({"sql": "SELECT 1", "filename": "s.csv"})
    assert csv_env["limit"] == 11
    assert out.startswith("wrote 10 rows to /work/s.csv") and "TRUNCATED at the 10-row export cap" in out
    written = list((tmp_path).rglob("s.csv"))
    assert written and len(written[0].read_text().strip().splitlines()) == 11  # header + 10


async def test_csv_export_under_the_cap_is_not_flagged(csv_env):
    csv_env["n"] = 8   # above sql_row_limit (5), below the export cap (10)
    out = await graph.run_sql_to_csv.ainvoke({"sql": "SELECT 1", "filename": "t.csv"})
    assert out.startswith("wrote 8 rows") and "TRUNCATED" not in out


async def test_run_readonly_sql_limit_overrides_the_row_limit(monkeypatch):
    calls = []

    class Conn:
        @asynccontextmanager
        async def transaction(self, readonly=False):
            yield

        async def fetch(self, sql):
            calls.append(sql)
            return [{"x": i} for i in range(100)]

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def pool():
        return Pool()

    monkeypatch.setattr(guardrails, "get_business_pool", pool)
    monkeypatch.setattr(guardrails, "get_settings", lambda: SimpleNamespace(sql_row_limit=5))
    assert len(await guardrails.run_readonly_sql("SELECT 1")) == 5
    assert calls[-1].endswith("LIMIT 6")
    assert len(await guardrails.run_readonly_sql("SELECT 1", limit=20)) == 20
    assert calls[-1].endswith("LIMIT 21")


def test_csv_cap_is_a_registered_live_setting():
    from app import configservice
    from app.config import Settings
    assert Settings(_env_file=None).sql_csv_row_limit == 5000
    meta = configservice.REGISTRY["sql_csv_row_limit"]
    assert meta["group"] == "tuning" and not meta["secret"]


# ── the agent sees and loads them (offline proof, no LLM) ───────────────────

def _seeded_rows():
    """(name, description) exactly as the Quick 004 block inserts them."""
    rows = []
    for slug in SLUGS:
        m = re.search(r"VALUES \('" + re.escape(slug) + r"', '((?:[^']|'')*)', ", _block())
        assert m, slug
        rows.append({"name": slug, "description": m.group(1).replace("''", "'")})
    return rows


async def test_enabled_catalog_lists_each_skill_with_owner_words(monkeypatch):
    rows = sorted(_seeded_rows(), key=lambda r: r["name"])

    class Pool:
        async def fetch(self, sql, *args):
            assert "enabled" in sql
            return rows

    async def pool():
        return Pool()

    monkeypatch.setattr(skillservice, "get_pool", pool)
    out = await skillservice.index_lines()
    lines = out.splitlines()
    assert lines[0] == skillservice.INDEX_PREAMBLE
    for r in rows:
        assert f"- {r['name']}: {r['description']}" in lines
    for words in ("forecast", "predict", "next month", "significant", "did it really change",
                  "unusual", "outlier", "seasonal", "when did it change", "what drives",
                  "why did it drop"):
        assert words in out, words


@pytest.mark.parametrize("slug", SLUGS)
async def test_use_skill_returns_the_framed_body(monkeypatch, slug):
    async def get_skill(name):
        return {"name": name, "enabled": True, "body": _between(SCHEMA, TAGS[name])}

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(skillservice, "get_skill", get_skill)
    monkeypatch.setattr(graph, "_log_tool", no_log)
    out = await graph.use_skill.ainvoke({"name": slug})
    assert out.startswith(skillservice.FRAMING)
    assert f"name: {slug}" in out and "## Who runs what" in out
    assert len(out) <= len(skillservice.FRAMING) + 2 + 20000, "use_skill caps bodies at 20000 chars"
    assert _text(slug).strip() in out.replace("\r\n", "\n")


def test_library_install_path_reads_every_skill():
    for slug in SLUGS:
        entry = skillservice.library_entry(f"intelligence-corner/{slug}")
        assert entry and skillservice.library_skill_body(entry).replace("\r\n", "\n").strip() == _text(slug).strip()


def test_scenario_script_is_stdlib_only_and_answers_help():
    import subprocess
    import sys
    script = ROOT / "backend" / "scripts" / "scenario_analytics.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in stdlib for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib
    out = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "--dashboard" in out.stdout
    src = script.read_text(encoding="utf-8")
    for slug in SLUGS:
        assert f'"{slug}"' in src
    assert "SKIPPED" in src
