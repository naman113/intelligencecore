"""use_skill / skill_asset (agents/graph.py) — the skills tool surface.

Spec pins:
- use_skill returns FRAMING + body capped at 20000 chars; unknown/disabled/
  invalid names refuse WITH the enabled-slug list and never leak a body;
- skill_asset resolves ONLY via installed row → origin_ref → manifest entry →
  containment-checked path (never a raw join on model text); traversal via
  relative_path OR dest_filename OR a bad skill_name lands nothing in /work;
  oversize assets and custom skills refuse;
- both tools live in _tool_registry and in AGENT_TOOL_SUPERSET for the
  supervisor and all four delegates; playbooks gain use_skill (only).

Pure logic: skillservice DB helpers and _log_tool are faked, the library is a
tmp_path tree behind the REAL library_entry/asset_path chain; no DB/LLM/net."""

import json

from app import sandbox, skillservice
from app.agents import graph

RUN = 77

BODY = "Step 1: fetch the data.\nStep 2: build the sheet."

ROW = {"id": 1, "name": "pdf", "description": "Fill PDF forms", "body": BODY,
       "source": "library", "origin_ref": "anthropics/pdf", "license": "MIT",
       "compat": "general", "enabled": True}


def _capture_log(monkeypatch):
    calls = []

    async def fake_log(name, args, result, start, refused=False):
        calls.append({"name": name, "args": args, "result": result,
                      "refused": refused})

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    return calls


def _wire_skill(monkeypatch, row=ROW, slugs=("alpha", "beta")):
    """Fake the skillservice DB helpers the tools call."""
    seen = {"get_skill": [], "enabled_slugs": 0}

    async def fake_get_skill(name):
        seen["get_skill"].append(name)
        return dict(row) if row and name == row["name"] else None

    async def fake_slugs():
        seen["enabled_slugs"] += 1
        return list(slugs)

    monkeypatch.setattr(skillservice, "get_skill", fake_get_skill)
    monkeypatch.setattr(skillservice, "enabled_slugs", fake_slugs)
    return seen


async def _use(name):
    fn = graph.use_skill.coroutine or graph.use_skill.func
    tok = graph.ctx_run_id.set(RUN)
    try:
        return await fn(name=name)
    finally:
        graph.ctx_run_id.reset(tok)


async def _asset(skill_name, relative_path, dest_filename):
    fn = graph.skill_asset.coroutine or graph.skill_asset.func
    tok = graph.ctx_run_id.set(RUN)
    try:
        return await fn(skill_name=skill_name, relative_path=relative_path,
                        dest_filename=dest_filename)
    finally:
        graph.ctx_run_id.reset(tok)


# ── use_skill ──────────────────────────────────────────────────────────────

async def test_use_skill_happy_path_framing_plus_body(monkeypatch):
    calls = _capture_log(monkeypatch)
    _wire_skill(monkeypatch)

    result = await _use("pdf")

    assert result.startswith(skillservice.FRAMING), \
        "the generic framing header must come FIRST — skill text is procedure, not policy"
    assert BODY in result
    assert calls and calls[-1]["name"] == "use_skill"
    assert calls[-1]["refused"] is False


async def test_use_skill_body_capped_at_20000(monkeypatch):
    _capture_log(monkeypatch)
    big = dict(ROW, body="x" * 30000)
    _wire_skill(monkeypatch, row=big)

    result = await _use("pdf")

    assert result.startswith(skillservice.FRAMING)
    tail = result[len(skillservice.FRAMING):]
    assert tail.count("x") == 20000, "body rides capped at 20000 chars"


async def test_use_skill_unknown_name_refuses_listing_enabled_slugs(monkeypatch):
    calls = _capture_log(monkeypatch)
    _wire_skill(monkeypatch, row=None, slugs=("alpha", "beta"))

    result = await _use("nope")

    assert "alpha" in result and "beta" in result, \
        "the refusal must list the enabled slugs so the model can recover"
    assert skillservice.FRAMING not in result, "no body leaks on a refusal"
    assert calls[-1]["refused"] is True, "refusals are logged as refusals"


async def test_use_skill_disabled_skill_refuses(monkeypatch):
    _capture_log(monkeypatch)
    disabled = dict(ROW, enabled=False)
    _wire_skill(monkeypatch, row=disabled, slugs=("alpha",))

    result = await _use("pdf")

    assert BODY not in result, "a disabled skill's body must never reach the model"
    assert "alpha" in result


async def test_use_skill_invalid_slug_refuses_without_body(monkeypatch):
    calls = _capture_log(monkeypatch)
    seen = _wire_skill(monkeypatch)

    for bad in ("../etc", "PDF", "a" * 65, "", "library", "install"):
        result = await _use(bad)
        assert skillservice.FRAMING not in result
        assert BODY not in result
    assert seen["get_skill"] == [], \
        "an invalid slug must be refused before any lookup runs"
    assert all(c["refused"] for c in calls)


async def test_use_skill_db_error_is_tool_error_not_crash(monkeypatch):
    _capture_log(monkeypatch)

    async def boom(name):
        raise ConnectionError("db down")

    monkeypatch.setattr(skillservice, "get_skill", boom)

    result = await _use("pdf")
    assert result.startswith("TOOL ERROR:")


# ── skill_asset ────────────────────────────────────────────────────────────

ENTRY = {"id": "anthropics/pdf", "name": "pdf", "description": "Fill PDF forms",
         "source": "anthropics", "license": "MIT", "compat": "general",
         "path": "anthropics/pdf"}


def _wire_asset(monkeypatch, tmp_path, row=ROW):
    """Real library_entry/asset_path against a tmp library; real _work_path
    against a tmp artifacts root; only the DB row is faked."""
    _capture_log(monkeypatch)
    lib = tmp_path / "skills_library"
    skill_dir = lib / "anthropics" / "pdf"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "template.md").write_text("# T", encoding="utf-8")
    (skill_dir / "scripts" / "helper.py").write_text("print('help')",
                                                    encoding="utf-8")
    (lib / "manifest.json").write_text(json.dumps([ENTRY]), encoding="utf-8")
    (lib / "SECRET-outside-skill.txt").write_text("not yours", encoding="utf-8")
    monkeypatch.setattr(skillservice, "library_root", lambda: lib)

    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", artifacts)

    async def fake_get_skill(name):
        return dict(row) if row and name == row["name"] else None

    async def fake_slugs():
        return ["pdf"]

    monkeypatch.setattr(skillservice, "get_skill", fake_get_skill)
    monkeypatch.setattr(skillservice, "enabled_slugs", fake_slugs)
    return lib, artifacts / f"run_{RUN}"


async def test_skill_asset_happy_path_copies_into_work(monkeypatch, tmp_path):
    lib, scratch = _wire_asset(monkeypatch, tmp_path)

    result = await _asset("pdf", "scripts/helper.py", "helper.py")

    dest = scratch / "helper.py"
    assert dest.is_file(), "the asset must land in THIS run's /work"
    assert dest.read_text(encoding="utf-8") == "print('help')"
    assert "helper.py" in result
    assert not result.startswith("TOOL ERROR")


async def test_skill_asset_flat_file_too(monkeypatch, tmp_path):
    _, scratch = _wire_asset(monkeypatch, tmp_path)
    await _asset("pdf", "template.md", "template.md")
    assert (scratch / "template.md").is_file()


async def test_skill_asset_traversal_via_relative_path_refused(monkeypatch, tmp_path):
    _, scratch = _wire_asset(monkeypatch, tmp_path)

    for rel in ("../SECRET-outside-skill.txt", "../../manifest.json",
                "/etc/passwd", "..\\SECRET-outside-skill.txt",
                "scripts/../../pdf2/x"):
        result = await _asset("pdf", rel, "loot.txt")
        assert not (scratch / "loot.txt").exists(), \
            f"traversal {rel!r} must never land a file in /work"
        assert "copied" not in result.lower()


async def test_skill_asset_traversal_via_dest_filename_refused(monkeypatch, tmp_path):
    tmp_lib, scratch = _wire_asset(monkeypatch, tmp_path)
    artifacts_root = scratch.parent

    result = await _asset("pdf", "template.md", "../evil.md")

    assert not (artifacts_root / "evil.md").exists()
    assert not (scratch / "evil.md").exists()
    assert "copied" not in result.lower()


async def test_skill_asset_bad_skill_name_refused(monkeypatch, tmp_path):
    _, scratch = _wire_asset(monkeypatch, tmp_path)

    for bad in ("../pdf", "PDF", "", "library"):
        result = await _asset(bad, "template.md", "t.md")
        assert not (scratch / "t.md").exists()
        assert "copied" not in result.lower()


async def test_skill_asset_unknown_skill_refuses_with_slugs(monkeypatch, tmp_path):
    _, scratch = _wire_asset(monkeypatch, tmp_path)
    result = await _asset("unknown-skill", "template.md", "t.md")
    assert not (scratch / "t.md").exists()
    assert "pdf" in result, "refusal lists the enabled slugs"


async def test_skill_asset_over_size_limit_refused(monkeypatch, tmp_path):
    lib, scratch = _wire_asset(monkeypatch, tmp_path)
    big = lib / "anthropics" / "pdf" / "big.bin"
    big.write_bytes(b"x" * (skillservice.ASSET_MAX + 1))

    result = await _asset("pdf", "big.bin", "big.bin")

    assert not (scratch / "big.bin").exists(), \
        "an over-limit asset must not be copied"
    assert "copied" not in result.lower()


async def test_skill_asset_exactly_at_limit_is_allowed(monkeypatch, tmp_path):
    lib, scratch = _wire_asset(monkeypatch, tmp_path)
    edge = lib / "anthropics" / "pdf" / "edge.bin"
    edge.write_bytes(b"x" * skillservice.ASSET_MAX)

    await _asset("pdf", "edge.bin", "edge.bin")
    assert (scratch / "edge.bin").is_file()


async def test_skill_asset_custom_skill_refused(monkeypatch, tmp_path):
    custom = dict(ROW, source="custom", origin_ref=None)
    _, scratch = _wire_asset(monkeypatch, tmp_path, row=custom)

    result = await _asset("pdf", "template.md", "t.md")

    assert not (scratch / "t.md").exists(), \
        "custom skills bundle no assets — nothing may be copied"
    assert "copied" not in result.lower()


async def test_skill_asset_library_absent_is_error_not_crash(monkeypatch, tmp_path):
    _wire_asset(monkeypatch, tmp_path)
    monkeypatch.setattr(skillservice, "library_root",
                        lambda: tmp_path / "gone")

    result = await _asset("pdf", "template.md", "t.md")

    assert isinstance(result, str) and result, \
        "an absent library must produce a readable error, never a raise"
    assert "copied" not in result.lower()


# ── registration pins ──────────────────────────────────────────────────────

def test_both_tools_resolve_in_tool_registry():
    reg = graph._tool_registry()
    for name in ("use_skill", "skill_asset"):
        assert name in reg
        t = reg[name]
        assert getattr(t, "coroutine", None) or getattr(t, "func", None), \
            f"{name} must be an invocable @tool"


def test_superset_gains_both_tools_for_all_five_agents():
    for key in ("supervisor", "data", "analysis", "document", "drawing"):
        tools = graph.AGENT_TOOL_SUPERSET[key]
        assert "use_skill" in tools, f"{key} missing use_skill"
        assert "skill_asset" in tools, f"{key} missing skill_asset"


def test_reflection_agent_gets_no_skills_tools():
    assert "use_skill" not in graph.AGENT_TOOL_SUPERSET["reflection"]
    assert "skill_asset" not in graph.AGENT_TOOL_SUPERSET["reflection"]


def test_playbook_registry_gains_use_skill_only():
    from app import playbooks
    reg = playbooks._playbook_registry()
    assert "use_skill" in reg, "playbooks must be able to load skill procedures"
    assert "skill_asset" not in reg, \
        "skill_asset (file drops into /work) stays off the playbook surface"
