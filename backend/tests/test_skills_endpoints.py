"""Skills CRUD ops endpoints (main.py). Spec pins:

- /api/skills/library resolves as the static route, never swallowed by the
  /api/skills/{name} slug route ('library'/'install' are also reserved slugs);
- name conflicts answer 409 with the existing row's source in the detail;
- bodies over 64KB answer 422 at write, the 41st enable answers 422 (never
  silent truncation), PATCH {} keeps `enabled` unchanged;
- the list endpoint carries NO body column (dashboard polls it);
- EVERY successful mutation drops the cached agent graph (graph.reset_agents —
  the configservice._invalidate pattern; the agent-config compare in run()
  cannot see a skills change).

Direct handler calls with a recording fake pool; no DB/LLM/network."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main
from app import skillservice
from app.agents import graph

SKILL = {"id": 1, "name": "pdf", "description": "Fill PDF forms",
         "body": "the full 64KB body text", "source": "library",
         "origin_ref": "anthropics/pdf", "license": "MIT", "compat": "general",
         "enabled": True, "created_at": None, "updated_at": None}


def _cols(sql: str):
    """Selected column names, or None for SELECT * (fake stays column-aware so
    a `SELECT *` in the list route would genuinely leak `body`)."""
    import re
    m = re.search(r"SELECT\s+(.*?)\s+FROM\s+skills", sql,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    cols = m.group(1).strip()
    if cols == "*" or "(" in cols:  # count(*)/aggregates handled in fetchval
        return None
    return [c.strip() for c in cols.split(",")]


class SkillsPool:
    def __init__(self, skills=None, enabled_count=0):
        self.skills = {s["name"]: dict(s) for s in (skills or [])}
        self.enabled_count = enabled_count
        self.executed = []

    def _shape(self, row, sql):
        cols = _cols(sql)
        return dict(row) if cols is None else {k: row.get(k) for k in cols}

    async def fetch(self, sql, *args):
        return [self._shape(s, sql)
                for s in sorted(self.skills.values(), key=lambda s: s["name"])]

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        if sql.strip().upper().startswith("INSERT"):
            import asyncpg
            name = args[0]
            if name in self.skills:
                raise asyncpg.UniqueViolationError("duplicate key")
            row = dict(SKILL, id=len(self.skills) + 10, name=name,
                       enabled=False)
            self.skills[name] = row
            return self._shape(row, "SELECT id, name, source, enabled FROM skills")
        for a in args:
            if isinstance(a, str) and a in self.skills:
                return self._shape(self.skills[a], sql)
        return None

    async def fetchval(self, sql, *args):
        if "count" in sql.lower():
            return self.enabled_count
        if sql.strip().upper().startswith("DELETE"):
            row = self.skills.pop(args[0], None)
            return row["id"] if row else None
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "UPDATE skills" in sql and "enabled" in sql:
            name = next((a for a in args if isinstance(a, str)
                         and a in self.skills), None)
            val = next((a for a in args if isinstance(a, bool)), None)
            if name is not None and val is not None:
                self.skills[name]["enabled"] = val
        return "UPDATE 1"


def _wire(monkeypatch, pool):
    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    return pool


def _count_resets(monkeypatch):
    counter = {"n": 0}

    def fake_reset():
        counter["n"] += 1

    monkeypatch.setattr(graph, "reset_agents", fake_reset)
    return counter


def _mk_library(tmp_path, monkeypatch, body="---\nname: pdf\n---\nBody."):
    import json
    lib = tmp_path / "skills_library"
    (lib / "anthropics" / "pdf").mkdir(parents=True)
    (lib / "anthropics" / "pdf" / "SKILL.md").write_text(body, encoding="utf-8")
    entry = {"id": "anthropics/pdf", "name": "pdf", "description": "PDFs",
             "source": "anthropics", "license": "MIT", "compat": "general",
             "path": "anthropics/pdf"}
    (lib / "manifest.json").write_text(json.dumps([entry]), encoding="utf-8")
    monkeypatch.setattr(skillservice, "library_root", lambda: lib)
    return entry


# ── routing: /library must not be swallowed by /{name} ─────────────────────

def test_library_route_wins_over_slug_route():
    from starlette.routing import Match
    scope = {"type": "http", "method": "GET", "path": "/api/skills/library",
             "root_path": "", "headers": []}
    for route in main.app.router.routes:
        m, _ = route.matches(scope)
        if m == Match.FULL:
            assert getattr(route, "path", "") == "/api/skills/library", \
                "GET /api/skills/library resolved into the /{name} slug route"
            return
    pytest.fail("no route matched /api/skills/library")


async def test_library_and_detail_are_distinct_handlers(monkeypatch, tmp_path):
    """Belt and braces at the handler level: the library listing answers even
    when NO skill row named 'library' exists (and none ever can — reserved)."""
    _mk_library(tmp_path, monkeypatch)
    _wire(monkeypatch, SkillsPool())
    out = await main.api_skills_library("")
    assert out["library_available"] is True
    assert [e["id"] for e in out["skills"]] == ["anthropics/pdf"]

    with pytest.raises(HTTPException) as ei:
        await main.api_skill_detail("some-unknown-skill")
    assert ei.value.status_code == 404


async def test_library_absent_is_failsoft_not_error(monkeypatch, tmp_path):
    monkeypatch.setattr(skillservice, "library_root",
                        lambda: tmp_path / "gone")
    _wire(monkeypatch, SkillsPool())
    out = await main.api_skills_library("")
    assert out == {"library_available": False, "skills": []}


# ── list omits body; detail carries it ─────────────────────────────────────

async def test_list_skills_omits_body(monkeypatch):
    _wire(monkeypatch, SkillsPool([SKILL]))
    out = await main.api_list_skills()
    assert len(out) == 1
    assert "body" not in out[0], \
        "the 5s-poll list must never ship 64KB bodies"
    assert out[0]["name"] == "pdf"
    assert out[0]["enabled"] is True


async def test_skill_detail_carries_body(monkeypatch):
    _wire(monkeypatch, SkillsPool([SKILL]))
    out = await main.api_skill_detail("pdf")
    assert out["body"] == SKILL["body"]


# ── create (custom) ────────────────────────────────────────────────────────

async def test_create_custom_skill_born_disabled_and_resets_agents(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)

    out = await main.api_create_skill(
        {"name": "my-skill", "description": "invoice follow-up tone",
         "body": "Remind the customer about the pending invoice politely."})

    assert out["enabled"] is False, \
        "creating is not consent — a human enables separately"
    assert pool.skills["my-skill"]["enabled"] is False
    assert resets["n"] == 1, "a skills mutation must drop the cached agents"
    # compat is NOT NULL with a DEFAULT — but an INSERT that names the column
    # and passes NULL bypasses the default (this 500'd live). Both INSERT
    # sites must supply a real value, never None/NULL.
    inserts = [(sql, args) for sql, args in pool.executed
               if "INSERT INTO skills" in sql]
    assert inserts, "custom create must INSERT"
    for sql, args in inserts:
        assert "'general'" in sql or "general" in args, \
            "custom INSERT must write compat='general', not NULL"


async def test_create_conflict_409_mentions_existing_source(monkeypatch):
    existing = dict(SKILL, source="custom")
    _wire(monkeypatch, SkillsPool([existing]))
    resets = _count_resets(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await main.api_create_skill(
            {"name": "pdf", "body": "How we file customer invoice documents."})

    assert ei.value.status_code == 409
    assert "custom" in str(ei.value.detail), \
        "the conflict detail must say WHICH source holds the name"
    assert resets["n"] == 0, "a failed mutation must not thrash the agent cache"


async def test_create_body_over_64kb_is_422(monkeypatch):
    _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await main.api_create_skill(
            {"name": "big-skill", "body": "x" * (skillservice.MAX_BODY + 1)})
    assert ei.value.status_code == 422
    assert resets["n"] == 0


async def test_create_body_at_cap_is_accepted(monkeypatch):
    _wire(monkeypatch, SkillsPool())
    _count_resets(monkeypatch)
    stem = "invoice reminder procedure "
    body = (stem + "x" * skillservice.MAX_BODY)[: skillservice.MAX_BODY]
    out = await main.api_create_skill({"name": "edge-skill", "body": body})
    assert out["name"] == "edge-skill"


@pytest.mark.parametrize("bad", ["library", "install", "PDF", "a" * 65, "", "a/b"])
async def test_create_rejects_invalid_and_reserved_slugs(monkeypatch, bad):
    pool = _wire(monkeypatch, SkillsPool())
    with pytest.raises(HTTPException) as ei:
        await main.api_create_skill({"name": bad, "body": "x"})
    assert ei.value.status_code == 422
    assert not pool.skills, "nothing may be written for a rejected slug"


# ── install (from the vendored library) ────────────────────────────────────

async def test_install_copies_library_body_born_disabled(monkeypatch, tmp_path):
    _mk_library(tmp_path, monkeypatch)
    pool = _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)

    out = await main.api_install_skill({"id": "anthropics/pdf"})

    assert out["enabled"] is False
    assert "pdf" in pool.skills
    assert resets["n"] == 1


async def test_install_unknown_entry_404(monkeypatch, tmp_path):
    _mk_library(tmp_path, monkeypatch)
    _wire(monkeypatch, SkillsPool())
    with pytest.raises(HTTPException) as ei:
        await main.api_install_skill({"id": "anthropics/nope"})
    assert ei.value.status_code == 404


async def test_install_empty_skill_md_is_404_not_installed(monkeypatch, tmp_path):
    """An empty/whitespace SKILL.md must not install — use_skill would return
    the framing header alone (the custom-create path already requires body)."""
    _mk_library(tmp_path, monkeypatch, body="   \n\n  ")
    pool = _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await main.api_install_skill({"id": "anthropics/pdf"})

    assert ei.value.status_code == 404
    assert not pool.skills and resets["n"] == 0


async def test_install_conflict_409_mentions_source(monkeypatch, tmp_path):
    _mk_library(tmp_path, monkeypatch)
    _wire(monkeypatch, SkillsPool([SKILL]))  # 'pdf' already installed (library)

    with pytest.raises(HTTPException) as ei:
        await main.api_install_skill({"id": "anthropics/pdf"})
    assert ei.value.status_code == 409
    assert "library" in str(ei.value.detail)


# ── PATCH: enable/disable, None-keeps, 40-cap ──────────────────────────────

async def test_patch_empty_body_keeps_enabled(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool([SKILL]))  # enabled=True

    out = await main.api_update_skill("pdf", {})

    assert out["enabled"] is True
    assert pool.skills["pdf"]["enabled"] is True, \
        "a partial update must never silently revoke what a human turned on"


async def test_patch_enabled_none_keeps_too(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool([SKILL]))
    out = await main.api_update_skill("pdf", {"enabled": None})
    assert out["enabled"] is True
    assert pool.skills["pdf"]["enabled"] is True


async def test_patch_enable_and_disable_reset_agents(monkeypatch):
    disabled = dict(SKILL, enabled=False)
    pool = _wire(monkeypatch, SkillsPool([disabled], enabled_count=3))
    resets = _count_resets(monkeypatch)

    out = await main.api_update_skill("pdf", {"enabled": True})
    assert out["enabled"] is True
    assert pool.skills["pdf"]["enabled"] is True
    assert resets["n"] == 1

    pool.enabled_count = 4
    out = await main.api_update_skill("pdf", {"enabled": False})
    assert out["enabled"] is False
    assert pool.skills["pdf"]["enabled"] is False
    assert resets["n"] == 2


async def test_41st_enable_is_422_never_silent_truncation(monkeypatch):
    disabled = dict(SKILL, enabled=False)
    pool = _wire(monkeypatch,
                 SkillsPool([disabled], enabled_count=skillservice.MAX_ENABLED))
    resets = _count_resets(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await main.api_update_skill("pdf", {"enabled": True})

    assert ei.value.status_code == 422
    assert pool.skills["pdf"]["enabled"] is False, \
        "the over-cap enable must not land"
    assert resets["n"] == 0


async def test_disable_always_allowed_even_at_cap(monkeypatch):
    pool = _wire(monkeypatch,
                 SkillsPool([SKILL], enabled_count=skillservice.MAX_ENABLED))
    out = await main.api_update_skill("pdf", {"enabled": False})
    assert out["enabled"] is False
    assert pool.skills["pdf"]["enabled"] is False


@pytest.mark.parametrize("bad", ["false", "true", "no", 0.5, [], {}])
async def test_patch_non_boolean_enabled_is_422(monkeypatch, bad):
    """bool('false') is True — a string here would ENABLE a skill the caller
    meant to disable. Anything but a JSON boolean (or None) must 422."""
    disabled = dict(SKILL, enabled=False)
    pool = _wire(monkeypatch, SkillsPool([disabled], enabled_count=0))
    resets = _count_resets(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await main.api_update_skill("pdf", {"enabled": bad})

    assert ei.value.status_code == 422
    assert pool.skills["pdf"]["enabled"] is False, \
        "a junk-typed 'enabled' must not flip the row"
    assert resets["n"] == 0


async def test_patch_unknown_skill_404(monkeypatch):
    _wire(monkeypatch, SkillsPool())
    with pytest.raises(HTTPException) as ei:
        await main.api_update_skill("ghost", {"enabled": True})
    assert ei.value.status_code == 404


# ── DELETE ─────────────────────────────────────────────────────────────────

async def test_delete_resets_agents(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool([SKILL]))
    resets = _count_resets(monkeypatch)

    out = await main.api_delete_skill("pdf")

    assert out == {"deleted": "pdf"}
    assert "pdf" not in pool.skills
    assert resets["n"] == 1


async def test_delete_unknown_404_no_reset(monkeypatch):
    _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await main.api_delete_skill("ghost")
    assert ei.value.status_code == 404
    assert resets["n"] == 0


# ── relevance blocker + draft endpoint ─────────────────────────────────────

async def test_create_blocks_coding_agent_skill(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await main.api_create_skill(
            {"name": "k8s-deployer",
             "body": "Deploy kubernetes manifests with docker from the "
                     "terminal, then refactor the codebase."})
    assert ei.value.status_code == 422
    assert "blocked" in str(ei.value.detail)
    assert resets["n"] == 0, "a blocked create must not thrash the agent cache"
    assert not any("INSERT INTO skills" in sql for sql, _ in pool.executed), \
        "a blocked create must never reach the INSERT"


async def test_draft_requires_goal(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await main.api_draft_skill({})
    assert ei.value.status_code == 422


async def test_draft_blocks_irrelevant_goal_without_llm_call(monkeypatch):
    async def boom(system, goal):  # pragma: no cover - must never run
        raise AssertionError("LLM called for a goal the gate should block")
    monkeypatch.setattr(main, "_draft_call", boom)
    with pytest.raises(HTTPException) as ei:
        await main.api_draft_skill(
            {"goal": "write terraform modules and bash scripts in the terminal"})
    assert ei.value.status_code == 422
    assert "blocked" in str(ei.value.detail)


async def test_draft_without_llm_key_is_503(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        main, "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="", supervisor_model="m"))
    with pytest.raises(HTTPException) as ei:
        await main.api_draft_skill({"goal": "invoice reminder tone for customers"})
    assert ei.value.status_code == 503


async def test_draft_model_refusal_becomes_422(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        main, "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="k", supervisor_model="m"))

    async def fake(system, goal):
        return "NOT RELEVANT: this is a coding workflow"
    monkeypatch.setattr(main, "_draft_call", fake)
    with pytest.raises(HTTPException) as ei:
        await main.api_draft_skill({"goal": "invoice reminder tone for customers"})
    assert ei.value.status_code == 422
    assert "NOT RELEVANT" in str(ei.value.detail)


async def test_draft_returns_parsed_skill_and_strips_fences(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        main, "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="k", supervisor_model="m"))

    async def fake(system, goal):
        # skill-creator guidance must ride along in the system prompt
        assert "SKILL.md" in system
        return ("```markdown\n---\nname: invoice-chase\n"
                "description: How we chase overdue invoices on WhatsApp\n---\n\n"
                "Steps for the reminder message and escalation.\n```")
    monkeypatch.setattr(main, "_draft_call", fake)

    out = await main.api_draft_skill({"goal": "chasing overdue invoice payments"})
    assert out["name"] == "invoice-chase"
    assert "overdue invoices" in out["description"]
    assert out["body"].startswith("---"), "markdown fence must be stripped"
    assert not out["body"].rstrip().endswith("```")


# ── scope (triage policies) ────────────────────────────────────────────────

async def test_create_accepts_triage_scope(monkeypatch):
    pool = _wire(monkeypatch, SkillsPool())
    _count_resets(monkeypatch)
    out = await main.api_create_skill(
        {"name": "discussion-cases", "scope": "triage",
         "description": "owner discussion demands become cases",
         "body": "When the owner demands a discussion in a group, open a case."})
    assert out["name"] == "discussion-cases"
    insert = next(( (sql, args) for sql, args in pool.executed
                    if "INSERT INTO skills" in sql))
    assert "scope" in insert[0] and "triage" in insert[1], \
        "the chosen scope must reach the INSERT"


async def test_create_rejects_unknown_scope(monkeypatch):
    _wire(monkeypatch, SkillsPool())
    resets = _count_resets(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await main.api_create_skill(
            {"name": "weird", "scope": "everything",
             "body": "invoice reminder procedure"})
    assert ei.value.status_code == 422
    assert resets["n"] == 0
