"""Sandboxed code execution: one Docker container per run, no network.

Each task run gets a scratch directory (artifacts/run_<id>/) mounted at /work
inside the container. Agents write scripts and data files there; produced
files (xlsx, pdf, png, csv) are the artifacts we can send over WhatsApp.

Cloud migration: replace DockerRunner with an E2B-backed implementation of
the same interface.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import shutil
import stat
from pathlib import Path

log = logging.getLogger("sandbox")

# Quick 004: overridable so hosts that run several installs side by side (the
# test stacks next to a client install) can give each its own image build;
# every real install keeps the default tag that agentctl / setup.sh rebuild.
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE") or "agentic-sandbox:latest"
TIMEOUT_SECONDS = 120
# Quick 043: IC_ARTIFACTS_DIR moves the root onto a mounted volume (Railway gives one
# volume per service); unset keeps <backend>/artifacts.
ARTIFACTS_ROOT = Path(os.getenv("IC_ARTIFACTS_DIR") or (Path(__file__).resolve().parents[2] / "artifacts"))

# Set ONLY when the gateway itself runs in a container (docker-out-of-docker).
#
# `-v {host_path}:/work` is resolved by the daemon on the HOST, so from inside a
# container "/app/artifacts/run_7" names a path that does not exist out there —
# docker silently creates an empty directory and the agent sees no files.
# Mounting the same named volume BY NAME avoids host-path translation entirely
# (which is otherwise a Windows/WSL2 minefield). Unset — the host-run dev
# layout — keeps the original bind mount.
ARTIFACTS_VOLUME = os.getenv("SANDBOX_ARTIFACTS_VOLUME", "")


def scratch_dir(run_id: int | str) -> Path:
    d = ARTIFACTS_ROOT / f"run_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    # The sandbox runs as an unprivileged 'runner' user while the gateway may
    # run as root (containerized stack) — a default 755 root-owned dir makes
    # EVERY sandbox file write fail ("Can write to /work: False"), which
    # silently broke chart generation. World-writable is acceptable: the dir
    # holds one run's scratch files and the volume is private to the stack.
    # Best-effort: chmod is a no-op on Windows bind mounts.
    try:
        d.chmod(0o777)
    except OSError:
        pass
    return d


# Carried-file provenance: which filenames carry_over copied into a run's
# /work, and the mtime_ns each copy landed with. send_document's stale-file
# note (graph._stale_file_note) fires only for a file that is IN this record
# AND whose mtime_ns is still exactly the recorded value — a same-file
# equality check, so clock skew between the host, the gateway container and
# the sandbox container (Docker Desktop drifts after host sleep) can never
# misfire it, and files that arrived WITH the current message are never in
# the record at all. In-memory and bounded: after a gateway restart the note
# simply doesn't fire for runs carried before the restart (fail-soft, same
# direction as every other failure in this path).
_carried: dict[str, dict[str, int]] = {}
_CARRIED_MAX_RUNS = 256


def _record_carried(run_id: int | str, name: str, mtime_ns: int) -> None:
    _carried.setdefault(str(run_id), {})[name] = mtime_ns
    while len(_carried) > _CARRIED_MAX_RUNS:     # drop oldest runs (insertion order)
        _carried.pop(next(iter(_carried)))


def carried_mtime_ns(run_id: int | str, name: str) -> int | None:
    """mtime_ns recorded when carry_over copied `name` into run `run_id`'s
    /work, or None when the file was not carried over (or the record is gone,
    e.g. after a restart — callers must treat None as 'no provenance claim')."""
    return _carried.get(str(run_id), {}).get(name)


def carry_over(run_id: int | str, previous_run_ids: list[int]) -> int:
    """Copy files from recent runs on the same thread into this run's /work.

    Each inbound message opens a NEW task_run, and /work is keyed on the run —
    so "here's revision A" (run 101), "here's revision B" (run 102), "what
    changed?" (run 103) left run 103 with an empty workspace, making
    diff_drawings unreachable over the only channel that ships. Follow-ups like
    "now send me just the power layers" hit the same wall.

    Always shutil.copy2, never hardlinks: separate inodes mean regenerating a
    carried file can no longer corrupt the older run's artifact. Each copy is
    recorded (filename → mtime_ns) so send_document's provenance note can flag
    a carried file this run re-sent without rebuilding; rebuilding rewrites the
    mtime, which breaks the equality and clears the flag. Never overwrites a
    file the current run already produced — which also keeps files that arrived
    WITH the current message (media/upload intake writes them before the worker
    claims the run) out of the carried record.
    """
    dest = scratch_dir(run_id)
    linked = 0
    for prev in previous_run_ids:
        src = ARTIFACTS_ROOT / f"run_{prev}"
        if not src.is_dir():
            continue
        for p in src.iterdir():
            if not p.is_file() or p.name in {"script.py", ".runner.py"}:
                continue
            target = dest / p.name
            if target.exists():
                continue
            try:
                shutil.copy2(p, target)
                _record_carried(run_id, p.name, target.stat().st_mtime_ns)
            except OSError:
                continue
            linked += 1
    if linked:
        log.info("run %s: carried over %d file(s) from %s", run_id, linked, previous_run_ids)
    return linked


def safe_name(filename: str | None, default: str = "document.pdf") -> str:
    """Reduce an externally-supplied filename to a flat, inert basename.

    Used on every intake path (WhatsApp media, dashboard upload). Note that
    stripping separators alone is not enough: '..' survives a naive character
    filter and resolves to the artifacts root.
    """
    import re

    base = Path(filename or "").name                     # drop any directory part
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not base or set(base) <= {"."}:                   # "", ".", ".." -> default
        return default
    return base[:120]


def list_files(run_id: int | str) -> list[str]:
    d = scratch_dir(run_id)
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.name != "script.py")


# Notebook-like runner: executes the script but auto-prints the value of a
# trailing bare expression (models constantly write `df.head()` as the last
# line expecting to see it — without this they debug blind).
_RUNNER = """\
import ast, sys
src = open('/work/script.py').read()
tree = ast.parse(src)
if tree.body and isinstance(tree.body[-1], ast.Expr):
    last = ast.Expression(tree.body[-1].value)
    tree.body = tree.body[:-1]
    g = {'__name__': '__main__'}
    exec(compile(tree, '/work/script.py', 'exec'), g)
    _v = eval(compile(last, '/work/script.py', 'eval'), g)
    if _v is not None:
        print(repr(_v))
else:
    exec(compile(tree, '/work/script.py', 'exec'), {'__name__': '__main__'})
"""


async def run_python(run_id: int | str, code: str) -> str:
    """Execute python code in the sandbox. Returns stdout/stderr + file listing."""
    from .config import get_settings
    if getattr(get_settings(), "sandbox_url", ""):
        return await _run_remote(run_id, code)      # Quick 042: no Docker socket
    d = scratch_dir(run_id)
    (d / "script.py").write_text(code)
    (d / ".runner.py").write_text(_RUNNER)

    # volume-subpath needs Docker Engine >= 27 (Docker Desktop ships well past
    # that); the failure is explicit rather than silent, and handled below.
    mount = (
        ["--mount",
         f"type=volume,source={ARTIFACTS_VOLUME},target=/work,volume-subpath={d.name}"]
        if ARTIFACTS_VOLUME else
        ["-v", f"{d}:/work"]
    )
    from .config import get_settings
    s = get_settings()
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", s.sandbox_memory, "--cpus", s.sandbox_cpus,
        # --pids-limit closes the one gap memory and CPU caps leave open: a
        # fork bomb exhausts host PIDs without ever touching either ceiling.
        "--pids-limit", str(s.sandbox_pids),
        *mount,
        SANDBOX_IMAGE,
        "python", "/work/.runner.py",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            return f"TIMEOUT: execution exceeded {TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        return "SANDBOX ERROR: docker CLI not found on host"

    # Pure scaffolding — written fresh on every call and read by nothing else.
    # Removing it here keeps `list_artifacts` and the dashboard's file list
    # honest about what the run actually produced.
    (d / ".runner.py").unlink(missing_ok=True)

    out = stdout.decode(errors="replace")[-8000:]
    err = stderr.decode(errors="replace")[-4000:]
    if proc.returncode != 0 and "volume-subpath" in err:
        return ("SANDBOX ERROR: this Docker engine does not support "
                "--mount volume-subpath (needs Engine >= 27). Upgrade Docker, or "
                "unset SANDBOX_ARTIFACTS_VOLUME and run the gateway on the host.")
    files = ", ".join(list_files(run_id)) or "(none)"
    status = "OK" if proc.returncode == 0 else f"EXIT CODE {proc.returncode}"
    return f"[{status}]\nSTDOUT:\n{out}\nSTDERR:\n{err}\nFILES IN /work: {files}"


# ── Quick 042: the sandbox-runner service ───────────────────────────────────────
#
# With SANDBOX_URL set, the script and the run's files go to the service
# (sandbox/server.py) and only the files the script created or changed come back.
# Unchanged files are never rewritten here: carry_over's provenance record compares
# exact mtime_ns, so touching a carried file would clear it.

REMOTE_MAX_BYTES = int(os.getenv("SANDBOX_MAX_BYTES") or 50 * 1024 * 1024)   # each way
REMOTE_LOCK_WAIT = 90          # the service's wait for its one-run-at-a-time lock
_RESERVED = ("script.py", ".runner.py")


class _RemoteError(Exception):
    """Carries the SANDBOX ERROR string the model will read."""


def _valid_relpath(name) -> bool:
    if not isinstance(name, str) or not name or len(name) > 240:
        return False
    if "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    return len(parts) <= 12 and all(p not in ("", ".", "..") for p in parts)


def _pack_files(d: Path) -> dict:
    """Regular files under the scratch dir (symlinks and special files are never
    sent), as {relative name: {b64, mtime_ns}}. Raises _RemoteError over the cap."""
    files: dict = {}
    total = 0
    for root, dirs, names in os.walk(d, followlinks=False):
        rel_root = os.path.relpath(root, d)
        for n in names:
            rel = n if rel_root == "." else f"{rel_root}/{n}".replace(os.sep, "/")
            if rel in _RESERVED or not _valid_relpath(rel):
                continue
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                total += st.st_size
                if total > REMOTE_MAX_BYTES:
                    raise _RemoteError(
                        f"SANDBOX ERROR: the files in /work are larger than the "
                        f"{REMOTE_MAX_BYTES // (1024 * 1024)} MB the sandbox service accepts; "
                        "remove or shrink input files")
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            files[rel] = {"b64": base64.b64encode(data).decode("ascii"), "mtime_ns": st.st_mtime_ns}
    return files


def _write_result_file(d: Path, rel: str, b64: str) -> None:
    """Write one returned file into the scratch dir: flat or nested relative names
    only, never through a symlink, atomically (temp file + replace)."""
    unsafe = "SANDBOX ERROR: the sandbox service returned an unsafe file name"
    if not _valid_relpath(rel) or rel in _RESERVED:
        raise _RemoteError(unsafe)
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise _RemoteError("SANDBOX ERROR: the sandbox service returned a corrupt file")
    root = os.path.realpath(d)
    cur = root
    for part in rel.split("/")[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise _RemoteError(unsafe)
        os.makedirs(cur, exist_ok=True)
        try:
            os.chmod(cur, 0o777)
        except OSError:
            pass
    target = os.path.join(root, *rel.split("/"))
    if os.path.realpath(os.path.dirname(target)) != os.path.dirname(target):
        raise _RemoteError(unsafe)
    tmp = f"{target}.ic-tmp-{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, target)       # replaces a symlink at the target, never follows it


def _apply_result(d: Path, files: dict, deleted: list) -> None:
    for rel, ent in files.items():
        _write_result_file(d, rel, ent.get("b64", "") if isinstance(ent, dict) else "")
    root = os.path.realpath(d)
    for rel in deleted:
        if _valid_relpath(rel) and rel not in _RESERVED:
            p = os.path.join(root, *rel.split("/"))
            if os.path.isfile(p) and not os.path.islink(p) and os.path.realpath(os.path.dirname(p)) == os.path.dirname(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def _run_remote(run_id: int | str, code: str) -> str:
    import httpx
    from .config import get_settings
    s = get_settings()
    d = scratch_dir(run_id)
    (d / "script.py").write_text(code)
    (d / ".runner.py").unlink(missing_ok=True)       # scaffolding of the docker path only
    try:
        files = await asyncio.to_thread(_pack_files, d)
    except _RemoteError as e:
        return str(e)
    body = {"script": code, "runner": _RUNNER, "files": files}
    payload = await asyncio.to_thread(lambda: json.dumps(body).encode("utf-8"))
    del body, files
    # Read timeout = the service's lock wait + the run timeout + margin. The service
    # answers only when the run is over, so this is effectively the whole call.
    wait = REMOTE_LOCK_WAIT + TIMEOUT_SECONDS + 30
    timeout = httpx.Timeout(connect=10.0, read=wait, write=60.0, pool=10.0)
    url = s.sandbox_url.rstrip("/") + "/run"
    headers = {"Authorization": f"Bearer {s.sandbox_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await asyncio.wait_for(
                client.post(url, content=payload, headers=headers), timeout=wait + 60)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        log.warning("sandbox service unreachable: %s", type(e).__name__)
        return "SANDBOX ERROR: cannot reach the sandbox service (is it running, and is SANDBOX_URL right?)"
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        log.warning("sandbox service timed out: %s", type(e).__name__)
        return "SANDBOX ERROR: the sandbox service did not answer in time"
    except httpx.HTTPError as e:
        log.warning("sandbox service call failed: %s", type(e).__name__)
        return f"SANDBOX ERROR: the sandbox service call failed ({type(e).__name__})"
    if resp.status_code in (401, 403):
        return "SANDBOX ERROR: the sandbox service rejected the token (check SANDBOX_TOKEN)"
    try:
        out_json = resp.json()
    except ValueError:
        out_json = {}
    if not isinstance(out_json, dict):
        out_json = {}
    if resp.status_code != 200:
        msg = " ".join(str(out_json.get("error", "")).split())[:200]
        if resp.status_code == 503 and msg.startswith("busy"):
            return f"SANDBOX ERROR: the sandbox service is busy ({msg}); retry in a moment"
        return f"SANDBOX ERROR: the sandbox service answered HTTP {resp.status_code}" + (f": {msg}" if msg else "")
    try:
        await asyncio.to_thread(_apply_result, d, out_json.get("files") or {}, out_json.get("deleted") or [])
    except _RemoteError as e:
        return str(e)
    except OSError as e:
        return f"SANDBOX ERROR: could not store the files from the sandbox service ({type(e).__name__})"
    if out_json.get("timeout"):
        return f"TIMEOUT: execution exceeded {TIMEOUT_SECONDS}s"
    out = str(out_json.get("stdout", ""))[-8000:]
    err = str(out_json.get("stderr", ""))[-4000:]
    rc = out_json.get("returncode", 1)
    files_txt = ", ".join(list_files(run_id)) or "(none)"
    status = "OK" if rc == 0 else f"EXIT CODE {rc}"
    return f"[{status}]\nSTDOUT:\n{out}\nSTDERR:\n{err}\nFILES IN /work: {files_txt}"
