"""Quick 042: the sandbox-runner service, tested against the REAL container built
from sandbox/Dockerfile.service (docker run on a loopback port). Skipped when Docker
is unavailable. The service runs with SANDBOX_TIMEOUT=8 and SANDBOX_LOCK_WAIT=4 so the
timeout and busy cases take seconds, not minutes."""

import base64
import json
import subprocess
import threading
import time

import pytest

import svc_helper as H

pytestmark = pytest.mark.skipif(not H.docker_ok(), reason="docker not available")

NORMAL = """
import pandas as pd, numpy as np, matplotlib.pyplot as plt, openpyxl, statsmodels.api as sm, scipy.linalg
from sklearn.linear_model import LinearRegression
x = np.random.randn(500, 3); y = x @ [1, 2, 3] + np.random.randn(500)
print(round(float(sm.OLS(y, sm.add_constant(x)).fit().params[1]), 0))
pd.DataFrame(x).to_excel('o.xlsx'); plt.plot(y); plt.savefig('p.png')
a = np.random.randn(1500, 1500); print((a @ a).sum() > -1e12)
"""


@pytest.fixture(scope="module")
def svc():
    H.build("Dockerfile.service", H.SERVICE_IMAGE)
    s = H.Service()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def small():
    """A service with a 1 MB per-direction cap."""
    H.build("Dockerfile.service", H.SERVICE_IMAGE)
    s = H.Service(env={"SANDBOX_MAX_BYTES": str(1024 * 1024)})
    yield s
    s.stop()


def ok(res):
    st, j = res
    assert st == 200, j
    return j


# ── auth ─────────────────────────────────────────────────────────────────────

def test_health_needs_no_token(svc):
    assert svc.get("/health") == (200, {"ok": True, "busy": False})


def test_missing_and_wrong_token_are_401(svc):
    assert svc.run("print(1)", token=None)[0] == 401
    assert svc.run("print(1)", token="wrong")[0] == 401
    assert svc.run("print(1)", token="")[0] == 401
    assert svc.run("print(1)", token=H.TOKEN + "x")[0] == 401
    assert svc.post_raw(b"{}", token=None)[0] == 401      # before the body is parsed
    assert svc.run("print(1)")[0] == 200


def test_refuses_to_start_without_a_token():
    r = subprocess.run(["docker", "run", "--rm", H.SERVICE_IMAGE], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "SANDBOX_TOKEN" in (r.stderr + r.stdout)


def test_refuses_to_start_as_non_root():
    r = subprocess.run(["docker", "run", "--rm", "--user", "1000", "-e", "SANDBOX_TOKEN=x", H.SERVICE_IMAGE],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "root" in (r.stderr + r.stdout)


# ── names ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["../x", "/etc/x", "a/../../b", "a\\b", "script.py", ".runner.py",
                                  "a//b", "./a", "x\x00y", ""])
def test_bad_upload_names_rejected(svc, name):
    st, j = svc.run("print(1)", files={name: b"x"})
    assert st == 400 and "error" in j


def test_non_base64_and_bad_json_rejected(svc):
    body = json.dumps({"script": "print(1)", "runner": H.RUNNER, "files": {"a": {"b64": "!!notb64"}}}).encode()
    assert svc.post_raw(body)[0] == 400
    assert svc.post_raw(b"not json")[0] == 400


def test_symlinks_and_specials_are_never_returned(svc):
    j = ok(svc.run(
        "import os\n"
        "open('real.txt', 'w').write('hi')\n"
        "os.symlink('/etc/passwd', 'link.txt')\n"
        "os.symlink('/opt/ic/server.py', 'link2.py')\n"
        "os.mkfifo('pipe')\n"
        "os.mkdir('d'); os.symlink('/etc', 'd/etc')\n"))
    assert list(j["files"]) == ["real.txt"]
    assert "link.txt" in j["skipped"] and "pipe" in j["skipped"]


# ── size caps ────────────────────────────────────────────────────────────────

def test_upload_over_cap_is_413(small):
    st, j = small.run("print(1)", files={"big.bin": b"x" * (1024 * 1024 + 10)})
    assert st == 413 and "MB" in j["error"]
    st, j = small.post_raw(b" " * (10 * 1024 * 1024))          # Content-Length over the limit
    assert st == 413


def test_output_over_cap_is_413_and_service_recovers(small):
    st, j = small.run("for i in range(3):\n    open(f'f{i}.bin', 'wb').write(b'x' * 600000)\n")
    assert st == 413 and "limit" in j["error"]
    assert ok(small.run("print('still fine')"))["stdout"].strip() == "still fine"


def test_single_file_over_fsize_limit_is_stopped(small):
    j = ok(small.run("open('big.bin', 'wb').write(b'x' * 5_000_000)\n"))
    assert j["returncode"] != 0          # RLIMIT_FSIZE: SIGXFSZ
    assert "big.bin" not in j["files"] or len(base64.b64decode(j["files"]["big.bin"]["b64"])) <= 1024 * 1024


# ── limits ───────────────────────────────────────────────────────────────────

COUNT_RUNNER_PROCS = (
    "import os\n"
    "n = [p for p in os.listdir('/proc') if p.isdigit() and int(p) != os.getpid() and "
    "int(open(f'/proc/{p}/status').read().split('Uid:')[1].split()[0]) == os.getuid()]\n"
    "print(len(n))")


def test_timeout_kills_script_and_its_children(svc):
    t0 = time.time()
    j = ok(svc.run("import subprocess, time\nsubprocess.Popen(['sleep', '300'])\n"
                   "subprocess.Popen(['sleep', '301'], start_new_session=True)\ntime.sleep(60)\n"))
    assert j["timeout"] is True and time.time() - t0 < 30
    assert ok(svc.run(COUNT_RUNNER_PROCS))["stdout"].strip() == "0"


def test_fork_bomb_is_contained(svc):
    j = ok(svc.run("import os, time\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        time.sleep(0.01)\n"))
    assert j["timeout"] is True
    assert ok(svc.run(COUNT_RUNNER_PROCS))["stdout"].strip() == "0"
    assert ok(svc.run("print('alive')"))["stdout"].strip() == "alive"


def test_memory_hog_is_contained_and_normal_script_works(svc):
    j = ok(svc.run("x = bytearray(2 * 1024**3)\nprint(len(x))\n"))
    assert j["returncode"] != 0 and "MemoryError" in j["stderr"]
    j = ok(svc.run("import numpy as np\nx = np.ones(700 * 2**20 // 8)\nprint(x.sum())\n"))
    assert j["returncode"] != 0 and "MemoryError" in j["stderr"]
    j = ok(svc.run(NORMAL))
    assert j["returncode"] == 0, j["stderr"]
    assert j["stdout"].split()[-1] == "True"
    assert set(j["files"]) == {"o.xlsx", "p.png"}


def test_huge_stdout_is_tailed_not_buffered(svc):
    j = ok(svc.run("for i in range(400000):\n    print('line', i, 'x' * 100)\n"))
    assert j["returncode"] == 0
    assert len(j["stdout"].encode()) <= 32000 and "line 399999" in j["stdout"]


def test_exit_code_and_stderr(svc):
    j = ok(svc.run("import sys\nprint('out')\nsys.stderr.write('err\\n')\nraise SystemExit(3)"))
    assert j["returncode"] == 3 and j["stdout"] == "out\n" and j["stderr"] == "err\n"
    j = ok(svc.run("1/0"))
    assert j["returncode"] == 1 and "ZeroDivisionError" in j["stderr"] and "/work/script.py" in j["stderr"]
    j = ok(svc.run("1+1"))
    assert j["stdout"].strip() == "2"                    # trailing bare expression is printed


# ── who the script is ────────────────────────────────────────────────────────

def test_script_is_not_root_and_cannot_see_the_server(svc):
    j = ok(svc.run(
        "import os, glob\n"
        "print('uids', os.getresuid(), os.getgid(), os.getgroups())\n"
        "print('env', sorted(os.environ))\n"
        "for f in ['/proc/1/environ', '/proc/1/mem']:\n"
        "    try:\n"
        "        open(f, 'rb').read(10); print('READ', f)\n"
        "    except OSError as e:\n"
        "        print('denied', f)\n"
        "tok = False\n"
        "for f in glob.glob('/proc/[0-9]*/environ') + glob.glob('/proc/[0-9]*/cmdline'):\n"
        "    try:\n"
        "        b = open(f, 'rb').read()\n"
        "        tok = tok or b'test-token' in b\n"
        "    except OSError:\n"
        "        pass\n"
        "print('token visible', tok)\n"
        "try:\n"
        "    os.kill(1, 9); print('KILLED 1')\n"
        "except OSError:\n"
        "    print('kill denied')\n"
        "try:\n"
        "    os.setuid(0); print('BECAME ROOT')\n"
        "except OSError:\n"
        "    print('setuid denied')\n"))
    out = j["stdout"]
    assert "uids (1000, 1000, 1000) 1000 []" in out, out
    assert "READ /proc" not in out and "KILLED" not in out and "BECAME ROOT" not in out
    assert "token visible False" in out and "denied /proc/1/environ" in out
    env_line = [ln for ln in out.splitlines() if ln.startswith("env ")][0]
    assert "TOKEN" not in env_line and "SANDBOX" not in env_line and "PORT" not in env_line
    for k in ("PATH", "HOME", "MPLBACKEND", "OMP_NUM_THREADS"):
        assert k in env_line


def test_script_cannot_write_server_files(svc):
    j = ok(svc.run(
        "for f in ['/opt/ic/server.py', '/etc/passwd', '/usr/local/bin/python3',\n"
        "          '/opt/runner-home-pristine/.bashrc', '/opt/runner-home-pristine/poison']:\n"
        "    try:\n"
        "        open(f, 'a').write('x'); print('WROTE', f)\n"
        "    except OSError:\n"
        "        print('denied', f)\n"))
    assert "WROTE" not in j["stdout"]


# ── what comes back ──────────────────────────────────────────────────────────

def test_only_new_or_changed_files_come_back(svc):
    files = {"a.csv": b"1,2\n", "b.csv": b"3,4\n", "gone.csv": b"x\n", "sub/n.csv": b"5\n"}
    j = ok(svc.run(
        "import os\n"
        "open('b.csv', 'a').write('5,6\\n')\n"          # changed
        "open('c.csv', 'w').write('new\\n')\n"           # new
        "open('a.csv', 'w').write('1,2\\n')\n"           # rewritten, same content: not returned
        "os.remove('gone.csv')\n"
        "os.makedirs('out', exist_ok=True); open('out/r.txt', 'w').write('deep')\n", files))
    assert sorted(j["files"]) == ["b.csv", "c.csv", "out/r.txt"]
    assert base64.b64decode(j["files"]["b.csv"]["b64"]) == b"3,4\n5,6\n"
    assert j["deleted"] == ["gone.csv"]
    assert "script.py" not in j["files"] and ".runner.py" not in j["files"]


def test_inputs_are_present_with_their_content(svc):
    j = ok(svc.run("print(open('in.txt').read(), open('d/e.txt').read())", {"in.txt": b"A", "d/e.txt": b"B"}))
    assert j["stdout"].strip() == "A B"


# ── runs are isolated from each other ────────────────────────────────────────

def test_second_run_starts_with_an_empty_work_dir(svc):
    ok(svc.run("import os\nopen('leftover.txt', 'w').write('x')\nos.makedirs('dd'); open('dd/z', 'w').write('y')\n"))
    j = ok(svc.run("import os\nprint(sorted(os.listdir('/work')))"))
    assert j["stdout"].strip() == "['.runner.py', 'script.py']"


def test_background_process_from_run_one_is_gone_before_run_two(svc):
    j = ok(svc.run("import subprocess\np = subprocess.Popen(['sleep', '1000'], start_new_session=True)\n"
                   "q = subprocess.Popen(['sh', '-c', 'sleep 1000 & wait'])\nprint(p.pid)"))
    pid = j["stdout"].strip()
    j2 = ok(svc.run(f"import os\nprint(os.path.exists('/proc/{pid}'))\n"
                    "print(sum(1 for p in os.listdir('/proc') if p.isdigit() and 'sleep' in open(f'/proc/{p}/cmdline').read()))"))
    assert j2["stdout"].split() == ["False", "0"]


def test_tmp_and_home_do_not_carry_over(svc):
    ok(svc.run("import os\nopen('/tmp/leak', 'w').write('x')\nopen(os.path.expanduser('~/leak'), 'w').write('x')\n"
               "os.makedirs('/tmp/dirleak'); open('/dev/shm/leak', 'w').write('x')\n"))
    j = ok(svc.run("import os\nprint(os.path.exists('/tmp/leak'), os.path.exists('/tmp/dirleak'), "
                   "os.path.exists(os.path.expanduser('~/leak')), os.path.exists('/dev/shm/leak'))"))
    assert j["stdout"].strip() == "False False False False"


def test_concurrent_run_gets_a_clear_busy_error_and_lock_is_released(svc):
    results = {}

    def first():
        results["first"] = svc.run("import time\ntime.sleep(60)")

    t = threading.Thread(target=first)
    t.start()
    time.sleep(1.5)
    assert svc.get("/health")[1]["busy"] is True
    st, j = svc.run("print('second')")
    assert st == 503 and j["error"].startswith("busy")
    t.join(30)
    assert results["first"][1]["timeout"] is True
    assert svc.get("/health")[1]["busy"] is False
    assert ok(svc.run("print('after')"))["stdout"].strip() == "after"


def test_server_survives_a_script_that_breaks_its_workspace(svc):
    ok(svc.run("import os\nos.makedirs('/tmp/a/b/c'); os.chmod('/tmp/a/b', 0)\nos.makedirs('locked/x'); os.chmod('locked', 0)\n"))
    assert ok(svc.run("print('recovered')"))["stdout"].strip() == "recovered"


# ── images ───────────────────────────────────────────────────────────────────

def test_plain_sandbox_build_is_still_the_runner_user_image():
    H.build("Dockerfile", H.DOCKER_IMAGE)
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", H.DOCKER_IMAGE,
                        "python", "-c", "import os,pwd,pandas,statsmodels;print(pwd.getpwuid(os.getuid()).pw_name, os.getcwd())"],
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.split() == ["runner", "/work"], r.stderr


def test_listens_on_ipv6_and_ipv4_for_private_networking(svc):
    """Railway's private network is IPv6 (dual-stack in newer environments): the
    service binds '::' with IPV6_V6ONLY off, so both families reach it."""
    for host in ("[::1]", "127.0.0.1"):
        r = subprocess.run(["docker", "exec", svc.name, "python", "-c",
                            f"import urllib.request;print(urllib.request.urlopen('http://{host}:8080/health').read())"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "ok" in r.stdout, (host, r.stderr[-300:])
