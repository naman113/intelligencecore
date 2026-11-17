"""Phase 15 (SRC-05): the only code that launches ic-sync containers. Secrets travel in the
subprocess environment, never argv. Every container is named and removed with docker rm -f on
timeout, error or cancel."""

import asyncio
import json
import logging
import os
import sys
import time
from urllib.parse import urlsplit

from . import secretbox, warehouse, whlink
from .config import get_settings

log = logging.getLogger("syncrunner")

RESULT_PREFIX = "IC_SYNC_RESULT "
SYNC_RUNNER_MISSING = "sync runner not installed"
COMMANDS = ("check", "probe", "test", "sync", "csv")
BASE_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
WIN_ENV_KEYS = ("SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "PROGRAMDATA", "TEMP", "TMP")
_STDERR_CLIP = 300
_IMAGE_TTL = 60.0

_image_cache: tuple[float, bool] | None = None
_interactive = asyncio.Semaphore(1)


class SyncImageMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(SYNC_RUNNER_MISSING)


def _base_env() -> dict[str, str]:
    keys = BASE_ENV_KEYS + (WIN_ENV_KEYS if sys.platform == "win32" else ())
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def stack_label() -> str:
    return os.environ.get("IC_STACK") or "local"


async def _exec(*args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run a short docker CLI command and return (exit code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args, env=_base_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (proc.returncode if proc.returncode is not None else 1,
            (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"))


async def image_present() -> bool:
    global _image_cache
    now = time.monotonic()
    if _image_cache and now - _image_cache[0] < _IMAGE_TTL:
        return _image_cache[1]
    try:
        rc, _, _ = await _exec("docker", "image", "inspect", get_settings().sync_image, timeout=20)
        ok = rc == 0
    except (OSError, asyncio.TimeoutError):
        ok = False
    _image_cache = (now, ok)
    return ok


async def _networks_of(container: str) -> list[str]:
    rc, out, _ = await _exec("docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}",
                             container, timeout=20)
    if rc != 0:
        return []
    try:
        return sorted(json.loads(out.strip() or "{}"))
    except ValueError:
        return []


async def _is_internal(network: str) -> bool:
    rc, out, _ = await _exec("docker", "network", "inspect", "-f", "{{.Internal}}", network, timeout=20)
    return rc == 0 and out.strip().lower() == "true"


EGRESS_LABEL = "ic.sync.role"


async def _is_egress(network: str) -> bool:
    # Quick 028 (WR-11): the dedicated egress network carries this label (docker-compose.yml)
    rc, out, _ = await _exec("docker", "network", "inspect", "-f",
                             '{{index .Labels "ic.sync.role"}}', network, timeout=20)
    return rc == 0 and out.strip() == "egress"


async def network_for(cmd: str) -> str:
    s = get_settings()
    if cmd == "csv":
        if s.sync_csv_network:
            return s.sync_csv_network
        for n in await _networks_of(s.sync_warehouse_host):
            if await _is_internal(n):
                return n
        raise ValueError("CSV loads need an internal sync network (SYNC_CSV_NETWORK)")
    if s.sync_network:
        return s.sync_network
    # Quick 028 (WR-11): only the dedicated sync egress network, never the stack's main network
    # (it holds agent-memory Postgres, FalkorDB and Langfuse). SYNC_NETWORK is an override.
    for n in await _networks_of(s.sync_warehouse_host):
        if not await _is_internal(n) and await _is_egress(n):
            return n
    raise ValueError("database syncs need the sync egress network (the warehouse is not on one; "
                     "update the stack or set SYNC_NETWORK)")


def loader_env() -> dict[str, str]:
    s = get_settings()
    kw = warehouse.connect_kwargs("loader")
    db = urlsplit(kw["dsn"]).path.lstrip("/") or "warehouse"
    return {"IC_WH_HOST": s.sync_warehouse_host, "IC_WH_PORT": str(s.sync_warehouse_port),
            "IC_WH_DB": db, "IC_WH_USER": kw["user"], "IC_WH_PASSWORD": kw["password"] or ""}


def source_env(cred: dict) -> dict[str, str]:
    host, _ = whlink.rewrite_loopback(str(cred.get("host", "")))
    env = {"IC_SRC_HOST": host, "IC_SRC_PORT": str(cred.get("port", "")),
           "IC_SRC_DB": str(cred.get("database", "")), "IC_SRC_USER": str(cred.get("user", "")),
           "IC_SRC_PASSWORD": str(cred.get("password", ""))}
    if cred.get("options"):
        env["IC_SRC_OPTIONS"] = cred["options"] if isinstance(cred["options"], str) \
            else json.dumps(cred["options"])
    return env


def _clip(text: str) -> str:
    return (secretbox.redact(text) or "").strip()[:_STDERR_CLIP]


def _parse(out: str, err: str, rc: int) -> dict:
    for line in reversed(out.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                res = json.loads(line[len(RESULT_PREFIX):])
                if isinstance(res, dict):
                    res.setdefault("exit_code", rc)
                    if rc != 0 or err.strip():
                        res.setdefault("stderr", _clip(err))
                    return res
            except ValueError:
                break
    return {"status": "failed", "exit_code": rc, "error": "no result from sync container",
            "stderr": _clip(err)}


def _kill(proc) -> None:
    try:
        if proc.returncode is None:
            proc.kill()
    except Exception:
        pass


async def _rm(cname: str) -> None:
    try:
        await _exec("docker", "rm", "-f", cname, timeout=30)
    except Exception:
        log.warning("could not remove container %s", cname)


async def run(cmd: str, job: dict, secret_env: dict[str, str], *, name: str,
              network: str | None = None, timeout: float, stdin_path=None) -> dict:
    if cmd not in COMMANDS:
        raise ValueError(f"unknown sync command: {cmd}")
    if not await image_present():
        raise SyncImageMissing()
    s = get_settings()
    net = network or await network_for(cmd)
    cname = f"ic-sync-{name}"
    add = getattr(secretbox, "add_known", None)
    if add:
        # Only real secrets: host, db and user names are not secret and would
        # poison log redaction (WR-01).
        for k in ("IC_SRC_PASSWORD", "IC_WH_PASSWORD"):
            if secret_env.get(k):
                add(secret_env[k])
    env = _base_env()
    env["IC_SYNC_JOB"] = json.dumps(job)
    env.update(secret_env)
    argv = ["docker", "run", "--rm", "--name", cname, "--label", "ic.sync=1",
            "--label", f"ic.stack={stack_label()}", "--network", net,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", s.sync_memory, "--memory-swap", s.sync_memory,
            "--cpus", s.sync_cpus, "--pids-limit", "256"]
    if stdin_path is not None:
        argv.append("-i")
    if cmd != "csv" and "IC_SRC_HOST" in secret_env:
        argv += ["--add-host", "host.docker.internal:host-gateway"]
    for k in ("IC_SYNC_JOB", *secret_env):
        argv += ["-e", k]
    argv += [s.sync_image, "python", "-m", "icsync", cmd]
    log.info("sync %s container=%s network=%s", cmd, cname, net)
    fh = open(stdin_path, "rb") if stdin_path is not None else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=fh if fh is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _rm(cname)
            _kill(proc)
            return {"status": "failed", "exit_code": None, "error": "sync container timed out"}
        except asyncio.CancelledError:
            await _rm(cname)
            _kill(proc)
            raise
        return _parse((out or b"").decode("utf-8", "replace"),
                      (err or b"").decode("utf-8", "replace"), proc.returncode or 0)
    finally:
        if fh is not None:
            fh.close()


async def run_interactive(cmd: str, job: dict, secret_env: dict[str, str], *, name: str,
                          timeout: float | None = None, network: str | None = None,
                          stdin_path=None) -> dict:
    t = timeout if timeout is not None else get_settings().sync_probe_timeout_seconds
    extra = {}
    if network is not None:
        extra["network"] = network
    if stdin_path is not None:
        extra["stdin_path"] = stdin_path
    async with _interactive:
        return await run(cmd, job, secret_env, name=name, timeout=t, **extra)


async def cleanup_orphans() -> int:
    try:
        rc, out, _ = await _exec("docker", "ps", "-aq", "--filter", "label=ic.sync=1",
                                 "--filter", f"label=ic.stack={stack_label()}", timeout=30)
    except Exception:
        return 0
    if rc != 0:
        return 0
    ids = out.split()
    for i in ids:
        await _rm(i)
    return len(ids)
