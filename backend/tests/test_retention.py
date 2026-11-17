"""Artifact retention — pure parts.

The sweep deletes files, so the tests that matter are the ones asserting what
it refuses to delete. A retention bug that over-deletes is silent: the feature
it breaks (carry_over across messages, an approval's evidence) fails later and
somewhere else.
"""

import time
from pathlib import Path

import pytest

from app import retention
from app.config import Settings


def test_run_id_parsing_handles_the_adhoc_dir():
    assert retention._run_id(Path("run_42")) == 42
    # graph.py falls back to scratch_dir("adhoc") when there is no run in
    # context, so this directory exists and has no owning row.
    assert retention._run_id(Path("run_adhoc")) is None
    assert retention._run_id(Path("notarun")) is None


def test_carry_over_window_is_respected_with_margin():
    # sandbox.carry_over links from the previous 3 runs on a thread. Keeping
    # exactly 3 would race a run that starts mid-sweep.
    assert retention.KEEP_RECENT_PER_THREAD > 3


def test_defaults_bound_both_age_and_size():
    s = Settings()
    # Age alone cannot stop forty drawings landing in one day; size alone
    # keeps stale artifacts forever on a quiet instance. Both are required.
    assert s.artifact_retention_days > 0
    assert s.artifact_max_gb > 0
    assert s.artifact_sweep_seconds >= 300


def test_dir_bytes_survives_a_file_vanishing_mid_walk(tmp_path):
    # The sweep races live runs writing into these directories.
    (tmp_path / "a.txt").write_bytes(b"x" * 10)
    assert retention._dir_bytes(tmp_path) == 10
    assert retention._dir_bytes(tmp_path / "missing") == 0


@pytest.mark.asyncio
async def test_sweep_is_a_noop_when_there_is_nothing_to_sweep(monkeypatch, tmp_path):
    monkeypatch.setattr(retention, "ARTIFACTS_ROOT", tmp_path / "nope")
    assert (await retention.sweep())["deleted"] == 0


@pytest.mark.asyncio
async def test_sweep_deletes_only_what_age_requires(monkeypatch, tmp_path):
    monkeypatch.setattr(retention, "ARTIFACTS_ROOT", tmp_path)

    async def _none() -> set:
        return set()
    monkeypatch.setattr(retention, "_protected_run_ids", _none)

    old, fresh = tmp_path / "run_1", tmp_path / "run_2"
    for d in (old, fresh):
        d.mkdir()
        (d / "out.csv").write_bytes(b"x" * 100)
    ancient = time.time() - 90 * 86400
    import os
    os.utime(old, (ancient, ancient))

    result = await retention.sweep()
    assert result["deleted"] == 1
    assert not old.exists()
    assert fresh.exists(), "a recent run must survive an age sweep"


@pytest.mark.asyncio
async def test_protected_runs_survive_even_when_ancient(monkeypatch, tmp_path):
    # An un-approved action's evidence outranks the retention window: a human
    # is still being asked to approve a number that came from this file.
    monkeypatch.setattr(retention, "ARTIFACTS_ROOT", tmp_path)

    async def _protect() -> set:
        return {7}
    monkeypatch.setattr(retention, "_protected_run_ids", _protect)

    d = tmp_path / "run_7"
    d.mkdir()
    (d / "invoice.pdf").write_bytes(b"x" * 100)
    import os
    ancient = time.time() - 365 * 86400
    os.utime(d, (ancient, ancient))

    assert (await retention.sweep())["deleted"] == 0
    assert d.exists()
