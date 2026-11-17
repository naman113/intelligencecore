"""Phase 15 SRC-05: static guards on the ic-sync image files."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
SYNC = APP / "infra" / "ic-sync"
DOCKERFILE = SYNC / "Dockerfile"
REQS = SYNC / "requirements.txt"
MAIN = SYNC / "icsync" / "__main__.py"


def _docker() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_base_image_pinned_by_digest():
    froms = [ln for ln in _docker().splitlines() if ln.startswith("FROM ")]
    assert len(froms) == 1 and "@sha256:" in froms[0] and "python:3.12-slim-bookworm" in froms[0]


def test_pip_requires_hashes():
    assert "--require-hashes" in _docker()


def test_telemetry_off():
    assert "RUNTIME__DLTHUB_TELEMETRY=false" in _docker()


def test_runs_non_root():
    users = re.findall(r"^USER\s+(\S+)", _docker(), flags=re.M)
    assert users and users[-1] not in ("root", "0")


def test_requirements_pinned_and_hashed():
    text = REQS.read_text(encoding="utf-8")
    assert re.search(r"^dlt==1\.31\.0\b", text, flags=re.M)
    entries = re.findall(r"^([A-Za-z0-9_.\-]+)==\S+[^\n]*?(?:\\\n(?:    .*\n?)*)", text, flags=re.M)
    assert entries
    blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", text)
    reqs = [b for b in blocks if re.match(r"[A-Za-z0-9_.\-]+==", b)]
    assert reqs
    for b in reqs:
        assert "--hash=sha256:" in b, b.splitlines()[0]


def test_result_line_contract():
    assert "IC_SYNC_RESULT " in MAIN.read_text(encoding="utf-8")


def test_backend_dependencies_untouched():
    py = (APP / "backend" / "pyproject.toml").read_text(encoding="utf-8").lower()
    for name in ("dlt", "pymysql", "pyodbc"):
        assert not re.search(rf"[\"']{name}[\[=<>~ \"']", py), name


def test_reconcile_decide_match_blocks_mismatched_key_forms():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra" / "ic-sync"))
    from icsync.reconcile import NOTE_MISMATCH, decide_match
    assert decide_match(1000, 0) == (False, NOTE_MISMATCH)
    assert decide_match(1000, 990) == (True, None)
    assert decide_match(1000, 500)[0] is False
    assert decide_match(50, 0)[0] is False
    assert decide_match(0, 0) == (True, None)
