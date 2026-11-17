"""Phase 11 GOLIVE-02: SETTINGS_ENCRYPTION_KEY provisioning scripts."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2]
SCRIPT = APP / "deploy" / "ensure-settings-key.sh"
HAVE = shutil.which("bash") and shutil.which("openssl")
needs = pytest.mark.skipif(not HAVE, reason="bash/openssl missing")


def _run(p):
    return subprocess.run(["bash", str(SCRIPT), str(p)], capture_output=True, text=True)


def _read(rel):
    return (APP / rel).read_text(encoding="utf-8")


@needs
def test_appends_when_missing(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\nOPS_KEY=abc\n")
    r = _run(f)
    assert r.returncode == 0 and "added" in r.stdout
    lines = f.read_text().splitlines()
    assert lines[:2] == ["A=1", "OPS_KEY=abc"]
    keys = [l for l in lines if l.startswith("SETTINGS_ENCRYPTION_KEY=")]
    assert len(keys) == 1 and re.fullmatch(r"SETTINGS_ENCRYPTION_KEY=[0-9a-f]{64}", keys[0])
    assert keys[0].split("=")[1] not in r.stdout


@needs
def test_idempotent(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\n")
    _run(f)
    before = f.read_bytes()
    r = _run(f)
    assert "kept" in r.stdout and f.read_bytes() == before


@needs
def test_fills_empty_in_place(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\nSETTINGS_ENCRYPTION_KEY=\nB=2\n")
    assert _run(f).returncode == 0
    lines = f.read_text().splitlines()
    assert lines[0] == "A=1" and lines[2] == "B=2"
    assert re.fullmatch(r"SETTINGS_ENCRYPTION_KEY=[0-9a-f]{64}", lines[1])


@needs
def test_never_overwrites_existing(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"SETTINGS_ENCRYPTION_KEY=not-hex\n")
    _run(f)
    assert f.read_bytes() == b"SETTINGS_ENCRYPTION_KEY=not-hex\n"


@needs
def test_missing_file(tmp_path):
    r = _run(tmp_path / "nope")
    assert r.returncode == 1 and "no such .env" in r.stderr


def test_install_calls_script_in_both_branches():
    s = _read("install.sh")
    assert s.count("ensure-settings-key.sh") >= 2
    assert "Settings key:" in s and "SETTINGS_ENCRYPTION_KEY" in s


def test_no_overwriting_paths():
    for rel in ("install.sh", "deploy/agentctl", "setup.sh"):
        s = _read(rel)
        assert "set_env SETTINGS_ENCRYPTION_KEY" not in s
        assert 's|^SETTINGS_ENCRYPTION_KEY' not in s


def test_update_and_setup_call_script():
    assert "ensure-settings-key.sh" in _read("deploy/agentctl")
    assert _read("setup.sh").count("ensure-settings-key.sh") == 1


def test_env_example_and_compose():
    assert _read(".env.example").count("\nSETTINGS_ENCRYPTION_KEY=") == 1
    assert "plaintext secrets" not in _read("docker-compose.yml")


@needs
@pytest.mark.parametrize("rel", ["install.sh", "setup.sh", "deploy/agentctl", "deploy/ensure-settings-key.sh"])
def test_bash_syntax(rel):
    r = subprocess.run(["bash", "-n", str(APP / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- Quick 029 F9: deploy/ensure-settings-key.ps1 (local.ps1 up) ----
import re as _re

PS1 = APP / "deploy" / "ensure-settings-key.ps1"
_PS = shutil.which("pwsh") or shutil.which("powershell")
needs_ps = pytest.mark.skipif(not _PS, reason="no pwsh/powershell")


def _ps(p):
    return subprocess.run([_PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1), str(p)],
                          capture_output=True, text=True)


def _key_of(data: bytes):
    m = _re.search(rb"^SETTINGS_ENCRYPTION_KEY=([0-9a-f]{64})\r?$", data, _re.M)
    return m.group(1).decode() if m else None


@needs_ps
def test_ps_appends_and_keeps_other_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\nB=2")                      # no trailing newline
    r = _ps(f)
    assert r.returncode == 0 and "added" in r.stdout
    data = f.read_bytes()
    assert data.startswith(b"A=1\nB=2\nSETTINGS_ENCRYPTION_KEY=")
    key = _key_of(data)
    assert key and key not in r.stdout + r.stderr
    assert not data.startswith(b"\xef\xbb\xbf")


@needs_ps
def test_ps_fills_empty_line_in_crlf_file(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\r\nSETTINGS_ENCRYPTION_KEY=\r\nB=2\r\n")
    assert _ps(f).returncode == 0
    data = f.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert data.startswith(b"A=1\r\nSETTINGS_ENCRYPTION_KEY=") and data.endswith(b"\r\nB=2\r\n")
    assert _key_of(data)


@needs_ps
def test_ps_keeps_existing_key_and_second_run_is_a_noop(tmp_path):
    f = tmp_path / ".env"
    raw = b"A=1\nSETTINGS_ENCRYPTION_KEY=" + b"ab" * 32 + b"\nB=2\n"
    f.write_bytes(raw)
    r = _ps(f)
    assert r.returncode == 0 and "kept" in r.stdout
    assert f.read_bytes() == raw
    f.write_bytes(b"A=1\n")
    _ps(f)
    once = f.read_bytes()
    _ps(f)
    assert f.read_bytes() == once


@needs_ps
def test_ps_missing_file_fails(tmp_path):
    r = _ps(tmp_path / "nope.env")
    assert r.returncode != 0


def test_local_up_calls_ps_script_before_compose():
    s = _read("deploy/local.ps1")
    up = s[s.index("'up' {"):s.index("'down'")]
    assert up.index("ensure-settings-key.ps1") < up.index("Invoke-Compose up")
    assert "Convert]::ToHexString" not in _read("deploy/ensure-settings-key.ps1")
    assert "SETTINGS_ENCRYPTION_KEY" in _read(".env.example")
