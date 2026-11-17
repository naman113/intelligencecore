"""Phase 15 (SRC-05): syncrunner against a fake docker CLI."""
import asyncio
import json

import pytest

from app import syncrunner
from app.config import get_settings


class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, hang=False):
        self.out, self.err, self.returncode, self.hang = out, err, rc, hang
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self.out, self.err

    def kill(self):
        self.killed = True


class Fake:
    def __init__(self):
        self.calls = []      # (argv, env, stdin)
        self.script = []     # callables (argv) -> FakeProc | None, first match wins

    async def __call__(self, *argv, env=None, stdin=None, stdout=None, stderr=None):
        self.calls.append((list(argv), env, stdin))
        for fn in self.script:
            p = fn(list(argv))
            if p is not None:
                return p
        return FakeProc()

    def with_prefix(self, *pre):
        return [c for c in self.calls if c[0][:len(pre)] == list(pre)]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", f)
    monkeypatch.setattr(syncrunner, "_image_cache", None)
    monkeypatch.setenv("IC_STACK", "tst")
    monkeypatch.setattr(syncrunner, "_interactive", asyncio.Semaphore(1))
    return f


def _result(status="ok"):
    return FakeProc(out=("noise\n" + syncrunner.RESULT_PREFIX + json.dumps({"status": status}) + "\n").encode())


def _is_run(a):
    return a[:2] == ["docker", "run"]


def test_argv_contract_and_no_secret_in_argv(fake):
    fake.script.append(lambda a: _result() if _is_run(a) else None)
    res = asyncio.run(syncrunner.run("check", {"a": 1}, {"IC_SRC_PASSWORD": "s3cretvalue"},
                                     name="chk-1", network="ic-net", timeout=30))
    assert res["status"] == "ok" and res["exit_code"] == 0
    argv, env, _ = fake.with_prefix("docker", "run")[0]
    for tok in ("--name", "ic-sync-chk-1", "--label", "ic.sync=1", "ic.stack=tst", "--cap-drop", "ALL",
                "no-new-privileges", "--pids-limit", "256", "--memory", "--memory-swap", "--cpus",
                "IC_SRC_PASSWORD", "IC_SYNC_JOB", "icsync", "check"):
        assert tok in argv
    assert argv[-4:] == ["python", "-m", "icsync", "check"]
    assert all("s3cretvalue" not in a for a in argv)
    assert env["IC_SRC_PASSWORD"] == "s3cretvalue" and "IC_SYNC_JOB" in env


_ALL = {k: "x" for k in syncrunner.BASE_ENV_KEYS + syncrunner.WIN_ENV_KEYS}


@pytest.mark.parametrize("plat,expected", [
    ("linux", set(syncrunner.BASE_ENV_KEYS)),
    ("win32", set(syncrunner.BASE_ENV_KEYS) | set(syncrunner.WIN_ENV_KEYS)),
])
def test_base_env_allowlist(monkeypatch, plat, expected):
    monkeypatch.setattr(syncrunner.sys, "platform", plat)
    monkeypatch.setattr(syncrunner.os, "environ", {**_ALL, "OPS_KEY": "k"})
    got = syncrunner._base_env()
    assert set(got) == expected and "OPS_KEY" not in got


def test_env_has_only_allowlist_plus_job_and_secrets(fake, monkeypatch):
    monkeypatch.setenv("OPS_KEY", "opsvalue123")
    fake.script.append(lambda a: _result() if _is_run(a) else None)
    asyncio.run(syncrunner.run("check", {}, {"IC_SRC_PASSWORD": "pw"}, name="e", network="n", timeout=5))
    env = fake.with_prefix("docker", "run")[0][1]
    assert "OPS_KEY" not in env
    assert set(env) - set(syncrunner._base_env()) == {"IC_SYNC_JOB", "IC_SRC_PASSWORD"}


def test_no_result_line_and_stderr_clipped_redacted(fake):
    fake.script.append(lambda a: FakeProc(out=b"hello\n", err=b"postgres://u:hunter2pw@h/db " + b"x" * 500, rc=1)
                       if _is_run(a) else None)
    res = asyncio.run(syncrunner.run("sync", {}, {}, name="n", network="n", timeout=5))
    assert res["status"] == "failed" and res["error"] == "no result from sync container"
    assert "hunter2pw" not in res["stderr"] and len(res["stderr"]) <= 300


def test_timeout_removes_container(fake):
    fake.script.append(lambda a: FakeProc(hang=True) if _is_run(a) else None)
    res = asyncio.run(syncrunner.run("sync", {}, {}, name="slow", network="n", timeout=0.05))
    assert res == {"status": "failed", "exit_code": None, "error": "sync container timed out"}
    assert fake.with_prefix("docker", "rm", "-f", "ic-sync-slow")


def test_cancel_removes_container(fake):
    fake.script.append(lambda a: FakeProc(hang=True) if _is_run(a) else None)

    async def go():
        t = asyncio.create_task(syncrunner.run("sync", {}, {}, name="cx", network="n", timeout=60))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert fake.with_prefix("docker", "rm", "-f", "ic-sync-cx")


def test_image_missing_and_cached(fake):
    fake.script.append(lambda a: FakeProc(rc=1) if a[:3] == ["docker", "image", "inspect"] else None)
    for _ in range(2):
        with pytest.raises(syncrunner.SyncImageMissing) as e:
            asyncio.run(syncrunner.run("check", {}, {}, name="m", network="n", timeout=5))
        assert str(e.value) == "sync runner not installed"
    assert len(fake.with_prefix("docker", "image", "inspect")) == 1


def test_cleanup_orphans(fake):
    fake.script.append(lambda a: FakeProc(out=b"aaa\nbbb\n") if a[:3] == ["docker", "ps", "-aq"] else None)
    assert asyncio.run(syncrunner.cleanup_orphans()) == 2
    ps = fake.with_prefix("docker", "ps")[0][0]
    assert "label=ic.sync=1" in ps and "label=ic.stack=tst" in ps
    assert fake.with_prefix("docker", "rm", "-f", "aaa") and fake.with_prefix("docker", "rm", "-f", "bbb")


def _net_script(fake, nets):
    """nets: {name: internal}"""
    def fn(a):
        if a[:2] == ["docker", "inspect"]:
            return FakeProc(out=json.dumps({n: {} for n in nets}).encode())
        if a[:3] == ["docker", "network", "inspect"]:
            return FakeProc(out=str(nets[a[-1]]).lower().encode())
        return None
    fake.script.append(fn)


def test_network_for_csv(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_csv_network", "explicit")
    assert asyncio.run(syncrunner.network_for("csv")) == "explicit"
    monkeypatch.setattr(get_settings(), "sync_csv_network", "")
    _net_script(fake, {"a_default": False, "b_sync": True})
    assert asyncio.run(syncrunner.network_for("csv")) == "b_sync"


def test_network_for_csv_none(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_csv_network", "")
    _net_script(fake, {"a_default": False})
    with pytest.raises(ValueError, match="CSV loads need an internal sync network"):
        asyncio.run(syncrunner.network_for("csv"))


@pytest.mark.parametrize("cmd", ["check", "probe", "test", "sync"])
def test_network_for_db_skips_internal(fake, monkeypatch, cmd):
    # Quick 028 (WR-11): only the labelled egress network counts (see test_quick028_source_security)
    monkeypatch.setattr(get_settings(), "sync_network", "")
    _net_script(fake, {"a_internal": True, "z_open": False})
    fake.script.insert(0, lambda a: FakeProc(out=b"egress") if a[:3] == ["docker", "network", "inspect"]
                       and "Labels" in " ".join(a) and a[-1] == "z_open" else (
        FakeProc(out=b"") if a[:3] == ["docker", "network", "inspect"] and "Labels" in " ".join(a) else None))
    assert asyncio.run(syncrunner.network_for(cmd)) == "z_open"


def test_network_for_db_none(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "")
    _net_script(fake, {"a_internal": True})
    with pytest.raises(ValueError, match="database syncs need the sync egress network"):
        asyncio.run(syncrunner.network_for("sync"))


def test_network_for_db_explicit(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "ic-net")
    assert asyncio.run(syncrunner.network_for("check")) == "ic-net"


def test_stdin_streamed(fake, tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("a,b\n1,2\n")
    fake.script.append(lambda a: _result() if _is_run(a) else None)
    asyncio.run(syncrunner.run("csv", {}, {}, name="c", network="n", timeout=5, stdin_path=f))
    argv, _, stdin = fake.with_prefix("docker", "run")[0]
    assert "-i" in argv and hasattr(stdin, "read")


def test_interactive_serialised(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_network", "ic-net")
    active = {"n": 0, "max": 0}

    class Slow(FakeProc):
        async def communicate(self):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.05)
            active["n"] -= 1
            return _result().out, b""

    fake.script.append(lambda a: Slow() if _is_run(a) else None)

    async def go():
        await asyncio.gather(*[syncrunner.run_interactive("probe", {}, {}, name=f"p{i}", timeout=5)
                               for i in range(3)])
    asyncio.run(go())
    assert active["max"] == 1


def test_loader_and_source_env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "warehouse_url", "postgresql://wh:5432/warehouse")
    monkeypatch.setattr(s, "wh_loader_password", "ldr")
    e = syncrunner.loader_env()
    assert e["IC_WH_USER"] == "wh_loader" and e["IC_WH_DB"] == "warehouse" and e["IC_WH_PASSWORD"] == "ldr"
    src = syncrunner.source_env({"host": "localhost", "port": 5432, "database": "d", "user": "u", "password": "p"})
    assert src["IC_SRC_HOST"] == "host.docker.internal"
