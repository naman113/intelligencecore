"""IC sandbox-runner service (Quick 042).

Runs the model's Python for a gateway that has no Docker socket (Railway). The
gateway POSTs a script and the run's files; this service runs the script as the
unprivileged `runner` user in /work and returns stdout, stderr and the files the
script created or changed.

Standard library only. Endpoints:
  GET  /health   no auth, {"ok": true, "busy": bool}
  POST /run      Authorization: Bearer <SANDBOX_TOKEN>; JSON body, see handle_run

Settings (environment): SANDBOX_TOKEN (required, the server refuses to start
without it), PORT (8080), SANDBOX_MEMORY (384m), SANDBOX_PIDS (256),
SANDBOX_TIMEOUT (120 s wall clock), SANDBOX_CPU_SECONDS (120), SANDBOX_LOCK_WAIT
(90 s), SANDBOX_MAX_BYTES (52428800, per direction).

One run at a time: scripts assume the path /work, so a run wipes /work, unpacks
its files, runs, collects and wipes again, all under one lock. Isolation is the
uid boundary: the server is root, scripts are `runner`, so a script cannot read
the server's environment (the token) or signal it. There is NO network
isolation (Railway offers none); see PLAN.md of quick 042 for the accepted risk.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORK = "/work"
RUNNER_USER = "runner"
RUNNER_HOME_PRISTINE = "/opt/runner-home-pristine"
RESERVED = ("script.py", ".runner.py")
# Directories a script can write outside /work; owned-by-runner entries are removed
# after every run so nothing survives into the next one.
SWEEP_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/var/lock")
MAX_FILES = 5000
MAX_DEPTH = 12
MAX_NAME = 240

# stdout/stderr are kept as tails; the gateway shows the last 8000 / 4000 chars
# and a char is at most 4 bytes, so these byte tails always contain them.
STDOUT_TAIL_BYTES = 8000 * 4
STDERR_TAIL_BYTES = 4000 * 4


def parse_size(text: str) -> int:
    """'384m' -> bytes. Accepts k, m, g suffixes (case-insensitive) or plain bytes."""
    t = str(text).strip().lower()
    mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    if t and t[-1] in mult:
        return int(float(t[:-1]) * mult[t[-1]])
    return int(t)


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.token = env.get("SANDBOX_TOKEN", "")
        self.port = int(env.get("PORT") or 8080)
        self.memory = parse_size(env.get("SANDBOX_MEMORY") or "384m")
        self.pids = int(env.get("SANDBOX_PIDS") or 256)
        self.timeout = float(env.get("SANDBOX_TIMEOUT") or 120)
        self.cpu_seconds = int(env.get("SANDBOX_CPU_SECONDS") or 120)
        self.lock_wait = float(env.get("SANDBOX_LOCK_WAIT") or 90)
        self.max_bytes = int(env.get("SANDBOX_MAX_BYTES") or 50 * 1024 * 1024)


CFG: Config | None = None
RUN_LOCK = threading.Lock()
_PW = None


def runner_pw():
    global _PW
    if _PW is None:
        _PW = pwd.getpwnam(RUNNER_USER)
    return _PW


# ── names ────────────────────────────────────────────────────────────────────

def valid_relpath(name) -> bool:
    """A relative POSIX path made of plain components: no absolute path, no '..',
    no backslash, no NUL, no empty parts, bounded length and depth."""
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        return False
    if "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    if len(parts) > MAX_DEPTH:
        return False
    return all(p not in ("", ".", "..") for p in parts)


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── workspace ────────────────────────────────────────────────────────────────

def _real_uids(pid: str):
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            state = uids = None
            for line in f:
                if line.startswith(b"State:"):
                    state = line.split()[1]
                elif line.startswith(b"Uid:"):
                    uids = [int(x) for x in line.split()[1:3]]
                    break
        return state, uids
    except (OSError, ValueError):
        return None, None


def kill_runner_processes() -> bool:
    """SIGKILL every process owned (real or effective uid) by the runner user, until
    a scan finds none. A script can setsid() out of the process group we kill, so
    the uid is the only boundary that holds. Zombies are skipped (tini reaps them)."""
    uid = runner_pw().pw_uid
    for _ in range(100):
        victims = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            state, uids = _real_uids(d)
            if uids and uid in uids and state != b"Z":
                victims.append(int(d))
        if not victims:
            return True
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.02)
    return False


def _remove_owned(path: str, uid: int) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    if st.st_uid != uid:
        return
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _chown_tree(path: str, uid: int, gid: int) -> None:
    os.lchown(path, uid, gid)
    for root, dirs, files in os.walk(path, followlinks=False):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(root, n), uid, gid)
            except OSError:
                pass


def reset_environment() -> None:
    """Kill leftovers, empty /work, restore the runner's home, clear the runner's
    files from the shared scratch directories. Runs before AND after every run."""
    pw = runner_pw()
    if not kill_runner_processes():
        raise RuntimeError("could not stop all runner processes")
    for name in os.listdir(WORK):
        p = os.path.join(WORK, name)
        try:
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        except OSError:
            subprocess.run(["rm", "-rf", "--one-file-system", p], check=False)
    os.chown(WORK, pw.pw_uid, pw.pw_gid)
    os.chmod(WORK, 0o755)
    for d in SWEEP_DIRS:
        if os.path.isdir(d):
            for n in os.listdir(d):
                _remove_owned(os.path.join(d, n), pw.pw_uid)
    home = pw.pw_dir
    shutil.rmtree(home, ignore_errors=True)
    if os.path.isdir(RUNNER_HOME_PRISTINE):
        shutil.copytree(RUNNER_HOME_PRISTINE, home, symlinks=True)
    else:
        os.makedirs(home)
    _chown_tree(home, pw.pw_uid, pw.pw_gid)
    os.chmod(home, 0o755)


def _write_input(rel: str, data: bytes, mtime_ns, uid: int, gid: int) -> None:
    """Create one uploaded file under /work. The directory was just emptied; O_EXCL and
    O_NOFOLLOW still make a symlink or an existing name impossible to follow."""
    target = os.path.join(WORK, rel)
    parent = os.path.dirname(target)
    # makedirs only through plain directories we create ourselves
    rel_parent = os.path.relpath(parent, WORK)
    cur = WORK
    if rel_parent != ".":
        for part in rel_parent.split(os.sep):
            cur = os.path.join(cur, part)
            if not os.path.isdir(cur):
                os.mkdir(cur, 0o755)
                os.chown(cur, uid, gid)
            elif os.path.islink(cur):
                raise RequestError(400, f"bad path: {rel}")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        _write_all(fd, data)
        os.fchown(fd, uid, gid)
    finally:
        os.close(fd)
    if isinstance(mtime_ns, int) and 0 < mtime_ns < 2 ** 62:
        os.utime(target, ns=(mtime_ns, mtime_ns), follow_symlinks=False)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view[:1 << 20])
        view = view[n:]


# ── running ──────────────────────────────────────────────────────────────────

# Launcher: runs AS the runner user (Popen switches uid first, so no preexec_fn,
# which is unsafe in a threaded server), sets the limits and execs the script. The
# limits survive exec. RLIMIT_DATA (heap + private anonymous mappings) bounds memory;
# RLIMIT_AS is not used because BLAS/OpenMP reserve far more address space than they
# touch (see PLAN.md).
_LAUNCH = """\
import os, resource, sys
mem, nproc, cpu, fsize = (int(x) for x in sys.argv[1:5])
def lim(r, soft, hard=None):
    resource.setrlimit(r, (soft, soft if hard is None else hard))
lim(resource.RLIMIT_DATA, mem)
lim(resource.RLIMIT_NPROC, nproc)
lim(resource.RLIMIT_CPU, cpu, cpu + 5)
lim(resource.RLIMIT_FSIZE, fsize)
lim(resource.RLIMIT_CORE, 0)
os.execv(sys.executable, [sys.executable, '/work/.runner.py'])
"""


def run_env() -> dict:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": runner_pw().pw_dir,
        "LANG": "C.UTF-8",
        "MPLBACKEND": "Agg",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


class Tail:
    """Keeps only the last `cap` bytes of a stream, so a script that prints
    gigabytes cannot grow the server's memory."""

    def __init__(self, cap: int):
        self.cap = cap
        self.buf = b""
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.buf = (self.buf + chunk)[-self.cap:]


def _pump(stream, tail: Tail) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            tail.feed(chunk)
    except (OSError, ValueError):
        return


def dir_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def execute(cfg: Config) -> dict:
    pw = runner_pw()
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _LAUNCH,
         str(cfg.memory), str(cfg.pids), str(cfg.cpu_seconds), str(cfg.max_bytes)],
        cwd=WORK, env=run_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        user=pw.pw_uid, group=pw.pw_gid, extra_groups=[],
        start_new_session=True, close_fds=True,
    )
    out, err = Tail(STDOUT_TAIL_BYTES), Tail(STDERR_TAIL_BYTES)
    threads = [threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + cfg.timeout
    timed_out = False
    note = ""
    last_size_check = 0.0
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        if now - last_size_check >= 1.0:
            last_size_check = now
            # per-file limit is RLIMIT_FSIZE; this bounds the total a script can
            # write into /work (4x the per-direction cap)
            if dir_size(WORK) > cfg.max_bytes * 4:
                note = f"\n[sandbox: run stopped, /work grew past {cfg.max_bytes * 4 // (1024 * 1024)} MB]\n"
                break
    # Whatever happened: kill the whole group, then every runner process (a script
    # can leave a background child behind or escape the group with setsid).
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    kill_runner_processes()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    for t in threads:
        t.join(timeout=5)
    for s in (proc.stdout, proc.stderr):
        try:
            s.close()
        except OSError:
            pass
    rc = proc.returncode
    if rc is None:
        rc = -9
    if rc < 0:
        rc = 128 + (-rc)            # killed by signal: report like `docker run` does
    if note:
        rc = rc or 1
    return {
        "returncode": rc,
        "timeout": timed_out,
        "stdout": out.buf.decode("utf-8", errors="replace"),
        "stderr": err.buf.decode("utf-8", errors="replace") + note,
    }


def _sha256_file(fd: int) -> tuple[str, bytes]:
    h = hashlib.sha256()
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        h.update(b)
        chunks.append(b)
    return h.hexdigest(), b"".join(chunks)


def collect(before: dict, cap: int) -> dict:
    """New or changed regular files under /work (hash differs from what was uploaded),
    names of uploaded files that are gone, and what was skipped. Symlinks, devices,
    FIFOs and hardlinked files are never read. The runner's processes are already dead,
    so nothing changes under the walk."""
    changed: dict[str, dict] = {}
    skipped: list[str] = []
    present: set[str] = set()
    total = 0
    count = 0
    for root, dirs, files in os.walk(WORK, followlinks=False):
        rel_root = os.path.relpath(root, WORK)
        for n in files:
            rel = n if rel_root == "." else f"{rel_root}/{n}"
            if rel in RESERVED:
                continue
            path = os.path.join(root, n)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or not valid_relpath(rel):
                skipped.append(rel)
                continue
            present.add(rel)
            if rel not in before and st.st_size > cap:
                raise RequestError(413, f"output file {rel} is larger than the {cap // (1024 * 1024)} MB limit")
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                skipped.append(rel)
                continue
            try:
                st2 = os.fstat(fd)
                if not stat.S_ISREG(st2.st_mode) or st2.st_nlink != 1:
                    skipped.append(rel)
                    continue
                digest, data = _sha256_file(fd)
            finally:
                os.close(fd)
            if before.get(rel) == digest:
                continue
            total += len(data)
            count += 1
            if total > cap or count > MAX_FILES:
                raise RequestError(413, f"output files are larger than the {cap // (1024 * 1024)} MB limit "
                                        "(or more than %d files); write fewer or smaller files" % MAX_FILES)
            changed[rel] = {"b64": base64.b64encode(data).decode("ascii")}
    deleted = sorted(n for n in before if n not in present and not os.path.lexists(os.path.join(WORK, n)))
    return {"files": changed, "deleted": deleted, "skipped": skipped[:50]}


def parse_request(body: bytes, cfg: Config) -> dict:
    """Validate everything BEFORE taking the lock or touching /work."""
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise RequestError(400, "body is not valid JSON")
    if not isinstance(req, dict):
        raise RequestError(400, "body must be a JSON object")
    script, runner = req.get("script"), req.get("runner")
    if not isinstance(script, str) or not isinstance(runner, str):
        raise RequestError(400, "script and runner must be strings")
    files_in = req.get("files", {})
    if not isinstance(files_in, dict):
        raise RequestError(400, "files must be an object")
    if len(files_in) > MAX_FILES:
        raise RequestError(413, f"more than {MAX_FILES} files")
    files = {}
    total = 0
    for name, ent in files_in.items():
        if not valid_relpath(name) or name in RESERVED:
            raise RequestError(400, f"bad file name: {name[:80]!r}")
        if not isinstance(ent, dict) or not isinstance(ent.get("b64"), str):
            raise RequestError(400, f"bad entry for {name[:80]!r}")
        try:
            data = base64.b64decode(ent["b64"], validate=True)
        except (binascii.Error, ValueError):
            raise RequestError(400, f"{name[:80]!r} is not valid base64")
        total += len(data)
        if total > cfg.max_bytes:
            raise RequestError(413, f"files are larger than the {cfg.max_bytes // (1024 * 1024)} MB limit")
        files[name] = (data, ent.get("mtime_ns"))
    return {"script": script, "runner": runner, "files": files}


def handle_run(body: bytes, cfg: Config) -> dict:
    req = parse_request(body, cfg)
    if not RUN_LOCK.acquire(timeout=cfg.lock_wait):
        raise RequestError(503, f"busy: another run held the sandbox for {cfg.lock_wait:g}s; try again")
    try:
        pw = runner_pw()
        reset_environment()
        before = {}
        for name, (data, mtime_ns) in req["files"].items():
            _write_input(name, data, mtime_ns, pw.pw_uid, pw.pw_gid)
            before[name] = hashlib.sha256(data).hexdigest()
        for name, text in (("script.py", req["script"]), (".runner.py", req["runner"])):
            p = os.path.join(WORK, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(p, 0o644)          # root-owned, readable by the runner
        result = execute(cfg)
        result.update(collect(before, cfg.max_bytes))
        return result
    finally:
        try:
            reset_environment()
        except Exception as e:           # never leave the lock held, whatever happens
            print(f"cleanup failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        RUN_LOCK.release()


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ic-sandbox"
    timeout = 60                       # per socket operation; a stalled upload is dropped

    def log_message(self, fmt, *args):  # request lines only, never headers or bodies
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, obj: dict, extra=None) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        given = hashlib.sha256(header[7:].encode("utf-8", "replace")).digest()
        return hmac.compare_digest(given, TOKEN_DIGEST)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "busy": RUN_LOCK.locked()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            return
        cfg = CFG
        try:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise RequestError(411, "Content-Length is required")
            limit = cfg.max_bytes * 4 // 3 + (2 << 20)
            if length < 0 or length > limit:
                raise RequestError(413, f"request is larger than the {cfg.max_bytes // (1024 * 1024)} MB limit")
            body = self._read_body(length)
            result = handle_run(body, cfg)
            self._send(200, result)
        except RequestError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:
            print(f"run failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            self._send(500, {"error": "internal error in the sandbox service"})

    def _read_body(self, length: int) -> bytes:
        chunks, got = [], 0
        deadline = time.monotonic() + 120      # overall upload deadline (slow-loris guard)
        while got < length:
            if time.monotonic() > deadline:
                raise RequestError(408, "upload too slow")
            chunk = self.rfile.read(min(1 << 20, length - got))
            if not chunk:
                raise RequestError(400, "connection closed before the body was complete")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)


class DualStackServer(ThreadingHTTPServer):
    """Binds '::' with IPV6_V6ONLY off so Railway's private network (IPv6; IPv4 and
    IPv6 in newer environments) and local IPv4 both reach it."""
    daemon_threads = True
    address_family = socket.AF_INET6
    request_queue_size = 32

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class V4Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def make_server(port: int):
    try:
        return DualStackServer(("::", port), Handler)
    except OSError:                     # no IPv6 in this container
        return V4Server(("0.0.0.0", port), Handler)


def self_test(cfg: Config) -> None:
    """Fail closed at startup: a script must actually run as the runner user, with
    the limits applied, and must not see the token."""
    probe = ("import os, json\n"
             "print(json.dumps({'uid': os.getresuid(), 'env': sorted(os.environ)}))\n")
    runner = "exec(open('/work/script.py').read())\n"
    with RUN_LOCK:
        try:
            reset_environment()
            for name, text in (("script.py", probe), (".runner.py", runner)):
                with open(os.path.join(WORK, name), "w") as f:
                    f.write(text)
                os.chmod(os.path.join(WORK, name), 0o644)
            res = execute(cfg)
        finally:
            reset_environment()
    if res["returncode"] != 0:
        raise SystemExit(f"self-test failed to run a script as {RUNNER_USER}: {res['stderr'][-300:]}")
    info = json.loads(res["stdout"])
    uid = runner_pw().pw_uid
    if info["uid"] != [uid, uid, uid]:
        raise SystemExit(f"self-test: scripts do not run as {RUNNER_USER} (uids {info['uid']})")
    if any("TOKEN" in k for k in info["env"]):
        raise SystemExit("self-test: the token is visible to scripts")


def main() -> None:
    global CFG, TOKEN_DIGEST
    cfg = Config()
    if not cfg.token:
        raise SystemExit("SANDBOX_TOKEN is not set: refusing to start without authentication")
    if os.geteuid() != 0:
        raise SystemExit("the sandbox service must start as root (it runs scripts as the "
                         f"{RUNNER_USER} user); on Railway set RAILWAY_RUN_UID=0")
    runner_pw()
    TOKEN_DIGEST = hashlib.sha256(cfg.token.encode("utf-8")).digest()
    CFG = cfg
    # The token is held only as a digest from here on.
    os.environ.pop("SANDBOX_TOKEN", None)
    cfg.token = ""
    os.makedirs(WORK, exist_ok=True)
    self_test(cfg)
    srv = make_server(cfg.port)
    print(f"sandbox service listening on port {cfg.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


TOKEN_DIGEST = b""

if __name__ == "__main__":
    main()
