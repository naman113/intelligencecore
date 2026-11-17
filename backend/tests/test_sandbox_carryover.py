"""sandbox.carry_over — copies, never hardlinks.

The hardlink 'optimization' had two failure modes: regenerating a carried
file wrote through the shared inode and CORRUPTED the older run's artifact,
and a fresh link's metadata could muddy provenance. carry_over now always
shutil.copy2s: separate inodes (edits stay local to the new run) with the
ORIGINAL mtime preserved — send_document's stale-file note depends on a
carried file keeping its source timestamp. Pure filesystem logic on tmp_path
with ARTIFACTS_ROOT monkeypatched; inode assertions are real in the Linux
testrunner."""

import os
import time

from app import sandbox


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)

    def no_link(*args, **kwargs):
        raise AssertionError("carry_over must copy, never hardlink")

    monkeypatch.setattr(os, "link", no_link)


def _prev_run(tmp_path, run_id, name, data, age_s=3600):
    src = tmp_path / f"run_{run_id}"
    src.mkdir(exist_ok=True)
    f = src / name
    f.write_bytes(data)
    past = time.time() - age_s
    os.utime(f, (past, past))
    return f, past


def test_carry_over_copies_with_mtime_preserved(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    src, past = _prev_run(tmp_path, 1, "invoice.pdf", b"ORIGINAL")

    assert sandbox.carry_over(2, [1]) == 1

    dest = tmp_path / "run_2" / "invoice.pdf"
    assert dest.is_file()
    assert dest.read_bytes() == b"ORIGINAL"
    assert dest.stat().st_ino != src.stat().st_ino, \
        "separate inodes — a hardlink would alias the older run's artifact"
    assert abs(dest.stat().st_mtime - past) < 1.0, \
        "copy2 preserves mtime — the stale-file provenance note relies on it"


def test_modifying_carried_copy_leaves_source_intact(monkeypatch, tmp_path):
    """THE corruption bug: with a hardlink, 'fix the document and re-send'
    rewrote the older run's artifact through the shared inode."""
    _wire(monkeypatch, tmp_path)
    src, _ = _prev_run(tmp_path, 1, "invoice.pdf", b"ORIGINAL")
    sandbox.carry_over(2, [1])

    dest = tmp_path / "run_2" / "invoice.pdf"
    dest.write_bytes(b"REGENERATED v2 -- corrected totals")

    assert src.read_bytes() == b"ORIGINAL", \
        "regenerating a carried file must never alter the older run's artifact"


def test_runner_scripts_are_not_carried(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    _prev_run(tmp_path, 1, "chart.png", b"PNG")
    _prev_run(tmp_path, 1, "script.py", b"print('x')")
    _prev_run(tmp_path, 1, ".runner.py", b"harness")

    assert sandbox.carry_over(2, [1]) == 1

    dest = tmp_path / "run_2"
    assert (dest / "chart.png").is_file()
    assert not (dest / "script.py").exists()
    assert not (dest / ".runner.py").exists()


def test_existing_dest_file_is_never_overwritten(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    _prev_run(tmp_path, 1, "report.pdf", b"OLD VERSION")
    cur = tmp_path / "run_2"
    cur.mkdir()
    (cur / "report.pdf").write_bytes(b"CURRENT RUN'S OWN FILE")

    assert sandbox.carry_over(2, [1]) == 0

    assert (cur / "report.pdf").read_bytes() == b"CURRENT RUN'S OWN FILE", \
        "a file the current run already produced always wins"


def test_missing_previous_run_is_a_noop(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    assert sandbox.carry_over(2, [99]) == 0
    assert list((tmp_path / "run_2").iterdir()) == []
