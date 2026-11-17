"""Vendored skills_library/ integrity + fetch script sanity.

The library is committed, read-only content and may legitimately be ABSENT
(client deployments that never vendored it) — those checks skip, they never
fail. The fetch script checks always run: it must parse, be stdlib-only (plus
an optional guarded yaml), and answer --help without touching the network."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import skillservice

LIB = skillservice.library_root()   # the SAME resolution the runtime uses
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_skills_library.py"

ALLOWED_SOURCES = {"anthropics", "hermes", "opencode"}
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                    "CC-BY-4.0"}
# Phase 2: first-party skills (source intelligence-corner). "Proprietary" is accepted ONLY for first-party sources.
FIRST_PARTY_SOURCES = {"intelligence-corner"}
FIRST_PARTY_LICENSES = {"Proprietary"}
ALLOWED_SOURCES = ALLOWED_SOURCES | FIRST_PARTY_SOURCES
ENTRY_KEYS = {"id", "name", "description", "source", "license", "compat",
              "relevant", "path"}
FILE_MAX = 204800


def _entries():
    if not LIB.is_dir() or not (LIB / "manifest.json").is_file():
        pytest.skip("library not vendored")
    data = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("skills")
    assert isinstance(data, list) and data, "manifest must be a non-empty list"
    return data


# ── manifest shape ─────────────────────────────────────────────────────────

def test_every_entry_has_the_full_key_set():
    for e in _entries():
        missing = ENTRY_KEYS - set(e)
        assert not missing, f"entry {e.get('id')!r} missing keys {missing}"


def test_relevance_is_a_curated_boolean_and_nonempty():
    """`relevant` drives the UIs' default shelf; it must be a real bool on
    every entry, and the curated shelf must not silently shrink to nothing."""
    entries = _entries()
    for e in entries:
        assert isinstance(e["relevant"], bool), e["id"]
    assert any(e["relevant"] for e in entries), \
        "no relevant skills — the default library view would be empty"


def test_sources_and_licenses_are_allowlisted():
    for e in _entries():
        assert e["source"] in ALLOWED_SOURCES, e["id"]
        assert e["license"] in ALLOWED_LICENSES or (e["source"] in FIRST_PARTY_SOURCES and e["license"] in FIRST_PARTY_LICENSES), (  # Phase 2
            f"{e['id']}: license {e['license']!r} not in the vendoring allowlist")


def test_ids_compose_source_slash_name_and_are_unique():
    entries = _entries()
    ids = [e["id"] for e in entries]
    assert len(set(ids)) == len(ids), "duplicate manifest ids"
    names = [e["name"] for e in entries]
    assert len(set(names)) == len(names), \
        "slug collisions must be deduped at vendor time (first-in-wins)"
    for e in entries:
        assert e["id"] == f"{e['source']}/{e['name']}"
        assert skillservice.validate_slug(e["name"]), \
            f"{e['id']}: name is not a valid (non-reserved) slug"


def test_paths_are_relative_forward_slash_and_contained():
    """The manifest is authored on Windows but read inside a Linux container —
    backslashes or absolute paths would silently break every install."""
    root = LIB.resolve()
    for e in _entries():
        p = str(e["path"])
        assert "\\" not in p, f"{e['id']}: backslash in path {p!r}"
        assert not p.startswith("/"), f"{e['id']}: absolute path {p!r}"
        assert ":" not in p, f"{e['id']}: drive letter in path {p!r}"
        resolved = (root / p).resolve()
        assert root in resolved.parents, \
            f"{e['id']}: path {p!r} escapes skills_library/"


def test_every_entry_has_a_skill_md_on_disk():
    for e in _entries():
        f = LIB / e["path"] / "SKILL.md"
        assert f.is_file(), f"{e['id']}: no SKILL.md at {e['path']}"


def test_no_vendored_file_exceeds_the_asset_limit():
    _entries()  # skip when not vendored
    for f in LIB.rglob("*"):
        if f.is_file():
            assert f.stat().st_size <= FILE_MAX, \
                f"{f.relative_to(LIB)} is {f.stat().st_size}B > {FILE_MAX}B — " \
                "skill_asset could never serve it and the repo bloats"


def test_library_total_stays_within_budget():
    _entries()
    total = sum(f.stat().st_size for f in LIB.rglob("*") if f.is_file())
    assert total <= 11 * 1024 * 1024, \
        f"library is {total}B — the vendor script's ~10MB budget has failed"


def test_bodies_parse_and_search_finds_them():
    """End-to-end through the real seam: every vendored SKILL.md parses and is
    reachable via search_library on its own name."""
    for e in _entries()[:50]:
        body = skillservice.library_skill_body(e)
        assert body, f"{e['id']}: SKILL.md unreadable through the seam"
        parsed = skillservice.parse_skill_md(body)
        assert parsed["body"], f"{e['id']}: empty body after parse"
        hits = {h["id"] for h in skillservice.search_library(e["name"])}
        assert e["id"] in hits


# ── fetch script (always runs — no library, no network needed) ─────────────

def test_fetch_script_parses():
    assert SCRIPT.is_file(), "backend/scripts/fetch_skills_library.py missing"
    ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))


def test_fetch_script_is_stdlib_only():
    """Dev-time script: stdlib + git CLI only (yaml allowed but must be an
    optional import — the regex fallback covers machines without it). It must
    never import app code: it runs OUTSIDE the backend environment."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [(node.module or "").split(".")[0]]
        for m in mods:
            assert m in stdlib | {"yaml"}, \
                f"non-stdlib import {m!r} — the fetch script must be self-contained"
            assert m != "app", "the fetch script must not import app code"


def test_fetch_script_help_runs_without_network():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.lower()
    assert "--dry-run" in out and "--only" in out
