"""Quick 042: sandbox.run_python through the sandbox-runner service.

The remote path is exercised against the REAL service container (docker run on a
loopback port, see svc_helper). The docker path (SANDBOX_URL empty) is pinned with a
fake subprocess: it must build exactly the `docker run` command it always did."""

import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest

import svc_helper as H
from app import sandbox

NEEDS_DOCKER = pytest.mark.skipif(not H.docker_ok(), reason="docker not available")

FORMAT = re.compile(r"^\[(OK|EXIT CODE -?\d+)\]\nSTDOUT:\n.*\nSTDERR:\n.*\nFILES IN /work: .*$", re.S)


@pytest.fixture(scope="module")
def svc():
    if not H.docker_ok():
        pytest.skip("docker not available")
    H.build("Dockerfile.service", H.SERVICE_IMAGE)
    s = H.Service()
    yield s
    s.stop()


def _wire(monkeypatch, tmp_path, url, token=H.TOKEN):
    import app.config as config
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    ns = SimpleNamespace(sandbox_url=url, sandbox_token=token, sandbox_memory="384m",
                         sandbox_cpus="0.5", sandbox_pids=256)
    monkeypatch.setattr(config, "get_settings", lambda: ns)


# ── docker path unchanged ────────────────────────────────────────────────────

async def test_empty_sandbox_url_keeps_the_docker_path(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, "")
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"hello\n", b""

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = list(cmd)
        return FakeProc()

    async def boom(*a, **k):
        raise AssertionError("remote path must not run without SANDBOX_URL")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox, "_run_remote", boom)
    out = await sandbox.run_python(5, "print('hello')")
    d = tmp_path / "run_5"
    assert seen["cmd"] == [
        "docker", "run", "--rm", "--network", "none", "--memory", "384m", "--cpus", "0.5",
        "--pids-limit", "256", "-v", f"{d}:/work", sandbox.SANDBOX_IMAGE, "python", "/work/.runner.py"]
    assert out == "[OK]\nSTDOUT:\nhello\n\nSTDERR:\n\nFILES IN /work: (none)"
    assert not (d / ".runner.py").exists() and (d / "script.py").read_text() == "print('hello')"


async def test_settings_without_the_field_still_use_docker(monkeypatch, tmp_path):
    import app.config as config
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        sandbox_memory="1g", sandbox_cpus="1", sandbox_pids=10))
    called = {}

    async def fake_exec(*cmd, **kw):
        called["docker"] = cmd[0]
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await sandbox.run_python(1, "1") == "SANDBOX ERROR: docker CLI not found on host"
    assert called["docker"] == "docker"


# ── remote path ──────────────────────────────────────────────────────────────

@NEEDS_DOCKER
async def test_output_format_matches_the_docker_path(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    out = await sandbox.run_python(7, "import sys\nsys.stderr.write('warn\\n')\nprint('hello')")
    assert out == "[OK]\nSTDOUT:\nhello\n\nSTDERR:\nwarn\n\nFILES IN /work: (none)"
    out = await sandbox.run_python(7, "raise SystemExit(4)")
    assert out.startswith("[EXIT CODE 4]\nSTDOUT:\n\nSTDERR:\n") and FORMAT.match(out)
    out = await sandbox.run_python(7, "print(1/0)")
    assert out.startswith("[EXIT CODE 1]") and "ZeroDivisionError" in out and "/work/script.py" in out
    out = await sandbox.run_python(7, "print('x' * 20000)\nimport sys\nsys.stderr.write('e' * 9000)")
    stdout_part = out.split("STDOUT:\n", 1)[1].split("\nSTDERR:\n", 1)[0]
    stderr_part = out.split("\nSTDERR:\n", 1)[1].split("\nFILES IN /work:", 1)[0]
    assert len(stdout_part) == 8000 and len(stderr_part) == 4000
    assert (tmp_path / "run_7" / "script.py").read_text().startswith("print('x'")


@NEEDS_DOCKER
async def test_files_round_trip_xlsx_png_csv(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    d = sandbox.scratch_dir(9)
    (d / "in.csv").write_text("a,b\n1,2\n3,4\n")
    code = (
        "import pandas as pd, matplotlib.pyplot as plt\n"
        "df = pd.read_csv('in.csv')\n"
        "df.to_excel('out.xlsx', index=False)\n"
        "df.to_csv('copy.csv', index=False)\n"
        "plt.plot(df.a, df.b); plt.savefig('chart.png')\n"
        "print(df.a.sum())\n")
    out = await sandbox.run_python(9, code)
    assert out.startswith("[OK]\nSTDOUT:\n4\n"), out
    assert out.endswith("FILES IN /work: chart.png, copy.csv, in.csv, out.xlsx")
    assert (d / "copy.csv").read_text().replace("\r\n", "\n") == "a,b\n1,2\n3,4\n"
    assert (d / "chart.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    import openpyxl
    assert openpyxl.load_workbook(d / "out.xlsx").active["A2"].value == 1
    assert not (d / ".runner.py").exists()


@NEEDS_DOCKER
async def test_carried_files_are_left_untouched(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    prev = tmp_path / "run_1"
    prev.mkdir()
    (prev / "report.pdf").write_bytes(b"%PDF-old")
    (prev / "data.csv").write_text("x\n1\n")
    past = time.time() - 7200
    os.utime(prev / "report.pdf", (past, past))
    os.utime(prev / "data.csv", (past, past))
    assert sandbox.carry_over(2, [1]) == 2
    d = tmp_path / "run_2"
    before = {n: (d / n).stat().st_mtime_ns for n in ("report.pdf", "data.csv")}
    assert all(sandbox.carried_mtime_ns(2, n) == before[n] for n in before)
    out = await sandbox.run_python(2, "print(open('report.pdf','rb').read()[:4])\n"
                                      "open('data.csv','a').write('2\\n')\nopen('new.txt','w').write('n')")
    assert out.startswith("[OK]") and "b'%PDF'" in out
    # untouched by the script: same inode content, same mtime_ns, provenance intact
    assert (d / "report.pdf").stat().st_mtime_ns == before["report.pdf"]
    assert sandbox.carried_mtime_ns(2, "report.pdf") == (d / "report.pdf").stat().st_mtime_ns
    # changed by the script: new content, new mtime, so the stale-file note clears
    assert (d / "data.csv").read_text() == "x\n1\n2\n"
    assert (d / "data.csv").stat().st_mtime_ns != sandbox.carried_mtime_ns(2, "data.csv")
    assert (d / "new.txt").read_text() == "n"
    assert (prev / "data.csv").read_text() == "x\n1\n"          # the older run's file is intact


@NEEDS_DOCKER
async def test_nested_files_and_deletes_round_trip(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    d = sandbox.scratch_dir(11)
    (d / "old.txt").write_text("old")
    out = await sandbox.run_python(11, "import os\nos.makedirs('sub', exist_ok=True)\n"
                                       "open('sub/a.txt','w').write('A')\nos.remove('old.txt')\n")
    assert out.startswith("[OK]")
    assert (d / "sub" / "a.txt").read_text() == "A" and not (d / "old.txt").exists()
    out = await sandbox.run_python(11, "print(open('sub/a.txt').read())")
    assert "STDOUT:\nA\n" in out


@NEEDS_DOCKER
async def test_timeout_string(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)          # the test service times out at 8 s
    out = await sandbox.run_python(12, "open('partial.txt','w').write('p')\nimport time\ntime.sleep(60)")
    assert out == f"TIMEOUT: execution exceeded {sandbox.TIMEOUT_SECONDS}s"
    assert (tmp_path / "run_12" / "partial.txt").read_text() == "p"


@NEEDS_DOCKER
async def test_shielded_cancellation_pattern_of_forecasts(svc, monkeypatch, tmp_path):
    """forecasts._sandbox_shielded: cancelling the caller must not cancel the call."""
    _wire(monkeypatch, tmp_path, svc.url)
    from app import forecasts
    outer = asyncio.ensure_future(forecasts._sandbox_shielded("13", "import time\ntime.sleep(2)\nopen('done.txt','w').write('1')"))
    await asyncio.sleep(0.5)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.gather(*list(forecasts._SANDBOX_TASKS))
    assert (tmp_path / "run_13" / "done.txt").read_text() == "1"
    # and the service is free again (lock released)
    assert (await sandbox.run_python(13, "print('ok')")).startswith("[OK]")


# ── error strings ────────────────────────────────────────────────────────────

async def test_service_down_is_a_sandbox_error(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, f"http://127.0.0.1:{H.free_port()}")
    out = await sandbox.run_python(20, "print(1)")
    assert out.startswith("SANDBOX ERROR: cannot reach the sandbox service")
    assert "127.0.0.1" not in out


@NEEDS_DOCKER
async def test_wrong_token_is_a_sandbox_error(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url, token="nope")
    out = await sandbox.run_python(21, "print(1)")
    assert out == "SANDBOX ERROR: the sandbox service rejected the token (check SANDBOX_TOKEN)"
    assert "nope" not in out


@NEEDS_DOCKER
async def test_oversize_input_is_a_sandbox_error_without_calling(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    monkeypatch.setattr(sandbox, "REMOTE_MAX_BYTES", 1000)
    (sandbox.scratch_dir(22) / "big.bin").write_bytes(b"x" * 2000)
    out = await sandbox.run_python(22, "print(1)")
    assert out.startswith("SANDBOX ERROR: the files in /work are larger than")


@NEEDS_DOCKER
async def test_busy_service_is_a_sandbox_error(svc, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, svc.url)
    first = asyncio.ensure_future(sandbox.run_python(23, "import time\ntime.sleep(60)"))
    await asyncio.sleep(1.5)
    out = await sandbox.run_python(24, "print(1)")
    assert out.startswith("SANDBOX ERROR: the sandbox service is busy")
    assert (await first).startswith("TIMEOUT")


# ── unsafe answers from a misbehaving service ────────────────────────────────

def test_unsafe_returned_names_are_refused(tmp_path):
    for bad in ("../x", "/etc/x", "a/../b", "a\\b", "script.py", ".runner.py", ""):
        with pytest.raises(sandbox._RemoteError):
            sandbox._write_result_file(tmp_path, bad, "eA==")
    try:
        (tmp_path / "evil").symlink_to(tmp_path.parent)
    except OSError:                       # no symlink privilege on this host
        pass
    if (tmp_path / "evil").is_symlink():
        with pytest.raises(sandbox._RemoteError):
            sandbox._write_result_file(tmp_path, "evil/x.txt", "eA==")
    sandbox._write_result_file(tmp_path, "ok/x.txt", "eA==")
    assert (tmp_path / "ok" / "x.txt").read_text() == "x"
