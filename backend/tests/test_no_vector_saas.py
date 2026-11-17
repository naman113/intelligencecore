"""Guard: the external vector store must never come back (pgvector only)."""

import types
from pathlib import Path

from app import configservice

# str.join runs at runtime; "a" + "b" or adjacent literals are folded by the
# compiler into one constant that lands in the .pyc (which the SC1 grep reads).
NEEDLE = "".join(("q", "drant"))
BACKEND = Path(__file__).resolve().parent.parent
FRONTEND_SRC = BACKEND.parent / "frontend" / "src"


def _files():
    for sub in ("app", "tests", "scripts"):
        root = BACKEND / sub
        if root.is_dir():
            for p in root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    yield p
    yield BACKEND / "pyproject.toml"
    yield BACKEND / "uv.lock"
    if FRONTEND_SRC.is_dir():
        for p in FRONTEND_SRC.rglob("*"):
            if p.is_file() and "node_modules" not in p.parts:
                yield p


def test_no_file_mentions_removed_store():
    bad = []
    for p in _files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if NEEDLE in text.lower():
            bad.append(str(p))
    assert not bad, bad


def test_config_registry_clean():
    assert not [k for k in configservice.REGISTRY if NEEDLE in k]
    assert "knowledge" not in configservice.GROUPS
    assert "knowledge" not in configservice.OPTIONAL_GROUPS
    assert configservice.REGISTRY["knowledge_score_floor"]["group"] == "tuning"


def _py_sources():
    for sub in ("app", "tests", "scripts"):
        root = BACKEND / sub
        if root.is_dir():
            for p in root.rglob("*.py"):
                if "__pycache__" not in p.parts:
                    yield p


def _string_constants(code):
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _string_constants(const)
        elif isinstance(const, (tuple, frozenset)):
            for item in const:
                if isinstance(item, (str, bytes)):
                    yield item
        elif isinstance(const, (str, bytes)):
            yield const


def test_compiled_constants_never_contain_needle():
    bad = []
    for p in _py_sources():
        text = p.read_text(encoding="utf-8")
        code = compile(text, str(p), "exec", dont_inherit=True)
        for c in _string_constants(code):
            if isinstance(c, str):
                hit = NEEDLE in c.lower()
            else:
                hit = NEEDLE.encode() in c.lower()
            if hit:
                bad.append(str(p.relative_to(BACKEND)))
                break
    assert not bad, bad


def test_bytecode_cache_clean():
    bad = []
    for sub in ("app", "tests", "scripts"):
        root = BACKEND / sub
        if root.is_dir():
            for p in root.rglob("*.pyc"):
                if NEEDLE.encode() in p.read_bytes().lower():
                    bad.append(str(p))
    assert not bad, (
        f"compiled cache holds the removed store's name: {bad}. Delete those "
        "__pycache__ directories; if a hit returns after the next test run, "
        "look for a string literal the compiler constant-folds."
    )
