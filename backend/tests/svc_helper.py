"""Quick 042: helpers that run the REAL sandbox-runner service container.

The service is built from sandbox/Dockerfile.service and started with `docker run`
on a free loopback port. Everything is tagged ic-quick042-* so it is easy to find
and remove. Tests that use it skip when Docker is not available, or when
IC_SKIP_SANDBOX_SERVICE_TESTS=1 (the image build takes minutes the first time)."""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parents[2] / "sandbox"
SERVICE_IMAGE = "ic-quick042-service"
DOCKER_IMAGE = "ic-quick042-docker"
TOKEN = "test-token-" + uuid.uuid4().hex

# The same notebook-like runner the gateway sends (sandbox._RUNNER).
from app import sandbox as _sandbox  # noqa: E402
RUNNER = _sandbox._RUNNER


def docker_ok() -> bool:
    if os.getenv("IC_SKIP_SANDBOX_SERVICE_TESTS") == "1" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build(dockerfile: str, tag: str) -> None:
    r = subprocess.run(["docker", "build", "-q", "-f", str(SANDBOX_DIR / dockerfile), "-t", tag,
                        str(SANDBOX_DIR)], capture_output=True, text=True, timeout=1500)
    if r.returncode != 0:
        raise RuntimeError(f"docker build {tag} failed: {r.stderr[-1500:]}")


class Service:
    def __init__(self, env: dict | None = None, token: str = TOKEN):
        self.port = free_port()
        self.name = f"ic-quick042-svc-{uuid.uuid4().hex[:8]}"
        self.token = token
        env = {"SANDBOX_TOKEN": token, "SANDBOX_TIMEOUT": "8", "SANDBOX_LOCK_WAIT": "4", **(env or {})}
        cmd = ["docker", "run", "-d", "--name", self.name, "--memory", "1g", "--pids-limit", "2048",
               "-p", f"127.0.0.1:{self.port}:8080"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(SERVICE_IMAGE)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        self.url = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self.get("/health")[0] == 200:
                    return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("service did not come up: " + self.logs())

    def logs(self) -> str:
        return subprocess.run(["docker", "logs", self.name], capture_output=True, text=True).stdout

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post_raw(self, body: bytes, token: str | None = "default", timeout: float = 120):
        headers = {"Content-Type": "application/json"}
        tok = self.token if token == "default" else token
        if tok is not None:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(self.url + "/run", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def run(self, script: str, files: dict | None = None, token: str | None = "default", **kw):
        """files: {name: bytes}. Returns (status, json)."""
        import base64
        ent = {n: {"b64": base64.b64encode(b).decode()} for n, b in (files or {}).items()}
        body = json.dumps({"script": script, "runner": RUNNER, "files": ent}).encode()
        return self.post_raw(body, token, **kw)
