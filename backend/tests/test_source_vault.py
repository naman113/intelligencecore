"""Phase 15 (SRC-02): credential vault with a fake pool."""
import json
import logging

import pytest

from app import secretbox, sourcevault, syncrunner
from app.config import get_settings

KEY = "c3" * 32
PW = "hunter2-longsecret"
FIELDS = {"host": "db.example", "port": 3306, "database": "erp", "user": "ro",
          "password": PW, "options": {"ssl": True}}


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Conn:
    def __init__(self, store):
        self.s = store

    def transaction(self):
        return Tx()

    async def fetchrow(self, sql, *a):
        if "FROM people" in sql:
            pid = self.s["person"]
            return {"id": pid} if pid is not None else None
        if "FROM source_credentials" in sql:
            return self.s["rows"].get(a[0])

    async def fetchval(self, sql, *a):
        return self.s["used"]

    async def execute(self, sql, *a):
        self.s["exec"].append((sql, a))
        if sql.lstrip().startswith("INSERT INTO source_credentials"):
            name, kind, secret, display = a
            self.s["rows"][name] = {"name": name, "kind": kind, "secret": secret,
                                    "display": display, "read_only": None,
                                    "verify_status": None, "verify_detail": None,
                                    "verified_at": None}
        elif sql.lstrip().startswith("UPDATE source_credentials"):
            r = self.s["rows"][a[0]]
            r.update(verify_status=a[1], verify_detail=a[2], read_only=a[3])
        elif sql.lstrip().startswith("DELETE FROM source_credentials"):
            self.s["rows"].pop(a[0], None)

    def audits(self):
        return [a for s, a in self.s["exec"] if "INSERT INTO actions" in s]


class Acq:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


class Pool(Conn):
    def acquire(self):
        return Acq(Conn(self.s))

    async def fetch(self, sql, *a):
        return list(self.s["rows"].values())


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", KEY)
    get_settings.cache_clear()
    store = {"rows": {}, "exec": [], "person": None, "used": 0}
    pool = Pool(store)

    async def gp():
        return pool

    monkeypatch.setattr(sourcevault, "get_pool", gp)
    calls = []

    async def ri(cmd, job, env_, *, name, timeout=None):
        calls.append((cmd, job, env_, name))
        return env.result

    monkeypatch.setattr(syncrunner, "run_interactive", ri)
    env.result = {"status": "ok", "read_only": True, "exit_code": 0}
    env.calls, env.store = calls, store
    yield env
    get_settings.cache_clear()
    secretbox._known.clear()
    secretbox._extra_known.clear()


def audits(env):
    return [json.loads(a[0]) for s, a in env.store["exec"] if "INSERT INTO actions" in s]


def test_aad_roundtrip_and_swap(monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", KEY)
    get_settings.cache_clear()
    st = secretbox.encrypt_aad("source_credentials:erp", "abc")
    assert st.startswith("enc:v1:")
    assert secretbox.decrypt_aad("source_credentials:erp", st) == "abc"
    with pytest.raises(secretbox.Undecryptable):
        secretbox.decrypt_aad("source_credentials:other", st)
    with pytest.raises(secretbox.Undecryptable):
        secretbox.decrypt("erp", st)
    get_settings.cache_clear()


def test_add_known_survives_refresh(monkeypatch):
    secretbox.add_known(["longsecretvalue1"])
    assert "[redacted]" in secretbox.redact("x longsecretvalue1 y")
    secretbox.refresh_known_secrets()
    assert "longsecretvalue1" not in secretbox.redact("longsecretvalue1")
    secretbox._known.clear()
    secretbox._extra_known.clear()


async def test_save_encrypts_and_verifies(env):
    view = await sourcevault.save("erp_my", "mysql", FIELDS)
    row = env.store["rows"]["erp_my"]
    assert PW not in row["secret"]
    assert json.loads(secretbox.decrypt_aad("source_credentials:erp_my", row["secret"]))["password"] == PW
    disp = json.loads(row["display"])
    assert "password" not in disp and disp["host"] == "db.example"
    assert view["verify_status"] == "ok" and view["read_only"] is True
    assert env.calls[0][0] == "check" and env.calls[0][2]["IC_SRC_PASSWORD"] == PW
    assert len(audits(env)) == 1


async def test_resave_replaces(env):
    await sourcevault.save("erp_my", "mysql", FIELDS)
    await sourcevault.save("erp_my", "mysql", dict(FIELDS, database="other"))
    assert json.loads(env.store["rows"]["erp_my"]["display"])["database"] == "other"
    assert len(audits(env)) == 2


@pytest.mark.parametrize("bad", [
    {"host": "a/b"}, {"host": "u@h"}, {"host": "h?x"}, {"host": "a b"}, {"host": "x://h"},
    {"host": "h" * 254}, {"port": 0}, {"port": 70000}, {"port": "x"}, {"database": ""},
    {"user": ""}, {"password": "p" * 1025}, {"options": {"bogus": 1}}, {"options": {"ssl": "yes"}},
])
async def test_validation(env, bad):
    with pytest.raises(ValueError) as e:
        await sourcevault.save("erp_my", "mysql", dict(FIELDS, **bad))
    assert PW not in str(e.value)
    assert audits(env) == [] and env.store["rows"] == {}


async def test_bad_name_and_kind(env):
    with pytest.raises(ValueError):
        await sourcevault.save("Bad Name", "mysql", FIELDS)
    with pytest.raises(ValueError):
        await sourcevault.save("erp_my", "oracle", FIELDS)


async def test_option_allowlists():
    for kind, opts in (("postgres", {"sslmode": "require"}),
                       ("mssql", {"encrypt": "strict", "trust_server_certificate": True})):
        out = sourcevault.validate("erp_x", kind, dict(FIELDS, options=opts))
        assert out["options"] == opts
    with pytest.raises(ValueError):
        sourcevault.validate("erp_x", "postgres", dict(FIELDS, options={"sslmode": "bogus"}))


async def test_failed_check_redacted(env):
    env.result = {"status": "error", "error": f"login failed for {PW} " + "x" * 400}
    view = await sourcevault.save("erp_my", "mysql", FIELDS)
    assert view["verify_status"] == "failed"
    assert PW not in view["verify_detail"] and len(view["verify_detail"]) <= 300


async def test_image_missing(env, monkeypatch):
    async def boom(*a, **k):
        raise syncrunner.SyncImageMissing()

    monkeypatch.setattr(syncrunner, "run_interactive", boom)
    view = await sourcevault.save("erp_my", "mysql", FIELDS)
    assert view["verify_status"] == "failed" and view["verify_detail"] == "sync runner not installed"


async def test_list_handles_no_secret(env):
    await sourcevault.save("erp_my", "mysql", FIELDS)
    out = await sourcevault.list_handles()
    assert out[0]["handle"] == "cred:erp_my"
    assert "secret" not in out[0] and "password" not in out[0]
    assert PW not in json.dumps(out, default=str)


async def test_load_decrypts(env):
    await sourcevault.save("erp_my", "mysql", FIELDS)
    assert (await sourcevault.load("erp_my"))["password"] == PW


async def test_delete_in_use_and_ok(env):
    await sourcevault.save("erp_my", "mysql", FIELDS)
    env.store["used"] = 1
    with pytest.raises(ValueError):
        await sourcevault.delete("erp_my")
    assert len(audits(env)) == 1
    env.store["used"] = 0
    await sourcevault.delete("erp_my")
    assert "erp_my" not in env.store["rows"] and len(audits(env)) == 2
    with pytest.raises(LookupError):
        await sourcevault.delete("erp_my")


async def test_no_key_raises(env, monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(secretbox.KeyMissing):
        await sourcevault.save("erp_my", "mysql", FIELDS)
    assert audits(env) == []


async def test_audit_decider_and_payload(env):
    await sourcevault.save("erp_my", "mysql", FIELDS)
    a = [a for s, a in env.store["exec"] if "INSERT INTO actions" in s][0]
    assert a[1] is None
    env.store["person"] = 7
    await sourcevault.verify("erp_my")
    a2 = [a for s, a in env.store["exec"] if "INSERT INTO actions" in s][1]
    assert a2[1] == 7 and isinstance(a2[1], int)
    p = json.loads(a2[0])
    assert p["op"] == "verify" and p["handle"] == "cred:erp_my" and p["origin"] == "owner"
    assert not {"password", "secret", "host", "display"} & set(p)
    assert PW not in a2[0] and "db.example" not in a2[0]


async def test_no_password_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.result = {"status": "error", "error": PW}
    await sourcevault.save("erp_my", "mysql", FIELDS)
    await sourcevault.verify("erp_my")
    assert PW not in caplog.text
