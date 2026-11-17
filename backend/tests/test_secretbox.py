"""Phase 11 (GOLIVE-02): secretbox AES-GCM, redaction and the log filter."""

import logging
import re

import pytest

from app import secretbox
from app.config import get_settings

KEY_A = "a1" * 32
KEY_B = "b2" * 32


def _setkey(monkeypatch, key):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    yield
    get_settings.cache_clear()
    secretbox._known.clear()


def test_roundtrip_and_format(monkeypatch):
    _setkey(monkeypatch, KEY_A)
    stored = secretbox.encrypt("wa_api_key", "s3cret-value")
    assert re.match(r"^enc:v1:[0-9a-f]{8}:[A-Za-z0-9_-]+=*$", stored)
    assert "s3cret-value" not in stored
    assert secretbox.decrypt("wa_api_key", stored) == "s3cret-value"


def test_nonce_is_random(monkeypatch):
    _setkey(monkeypatch, KEY_A)
    assert secretbox.encrypt("wa_api_key", "x" * 12) != secretbox.encrypt("wa_api_key", "x" * 12)


def test_aad_binds_name(monkeypatch):
    _setkey(monkeypatch, KEY_A)
    stored = secretbox.encrypt("wa_api_key", "s3cret-value")
    with pytest.raises(secretbox.Undecryptable):
        secretbox.decrypt("openai_api_key", stored)


def test_wrong_key_message_has_ids_only(monkeypatch):
    _setkey(monkeypatch, KEY_A)
    stored = secretbox.encrypt("wa_api_key", "s3cret-value")
    kid_a = secretbox.key_id()
    _setkey(monkeypatch, KEY_B)
    kid_b = secretbox.key_id()
    with pytest.raises(secretbox.Undecryptable) as ei:
        secretbox.decrypt("wa_api_key", stored)
    msg = str(ei.value)
    assert kid_a in msg and kid_b in msg
    assert "s3cret-value" not in msg
    assert stored.split(":", 3)[3] not in msg


def test_tampered_and_plain_rejected(monkeypatch):
    _setkey(monkeypatch, KEY_A)
    stored = secretbox.encrypt("wa_api_key", "s3cret-value")
    head, body = stored.rsplit(":", 1)
    flipped = ("B" if body[10] != "B" else "C")
    bad = head + ":" + body[:10] + flipped + body[11:]
    with pytest.raises(secretbox.Undecryptable):
        secretbox.decrypt("wa_api_key", bad)
    with pytest.raises(secretbox.Undecryptable):
        secretbox.decrypt("wa_api_key", "plain-value")


@pytest.mark.parametrize("bad", ["", "a1" * 31 + "a", "zz" * 32])
def test_bad_key_unavailable(monkeypatch, bad):
    _setkey(monkeypatch, bad)
    assert secretbox.key_available() is False
    with pytest.raises(secretbox.KeyMissing):
        secretbox.encrypt("wa_api_key", "value-12345")


def test_key_with_trailing_newline(monkeypatch):
    _setkey(monkeypatch, KEY_A + "\r\n")
    assert secretbox.key_available() is True


def test_is_encrypted():
    assert secretbox.is_encrypted("enc:v1:abcd1234:xx")
    assert not secretbox.is_encrypted("plain")
    assert not secretbox.is_encrypted("")
    assert not secretbox.is_encrypted(None)


def test_redact_known_values(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "ops-key-123456")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-9999")
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql://ro:pw-123456789@h:5432/db")
    monkeypatch.setenv("WA_API_KEY", "short77")
    get_settings.cache_clear()
    secretbox.refresh_known_secrets()
    out = secretbox.redact(
        "a ops-key-123456 b lf-secret-9999 c pw-123456789 d short77 e")
    assert "ops-key-123456" not in out
    assert "lf-secret-9999" not in out
    assert "pw-123456789" not in out
    assert "short77" in out  # under 8 chars: left alone
    assert out.count("[redacted]") == 3


def test_redact_generic_url_password():
    out = secretbox.redact("postgresql://u:hunter2-long@db/x")
    assert "hunter2-long" not in out
    assert out == "postgresql://u:[redacted]@db/x"


def test_redact_filter_message_and_traceback(monkeypatch):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-9999")
    get_settings.cache_clear()
    secretbox.refresh_known_secrets()

    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    h = H()
    h.addFilter(secretbox.RedactFilter())
    lg = logging.getLogger("secretbox-test")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(h)
    try:
        lg.info("key=%s", "lf-secret-9999")
        try:
            raise RuntimeError("boom lf-secret-9999")
        except RuntimeError:
            lg.exception("failed")
    finally:
        lg.removeHandler(h)
    assert len(seen) == 2
    assert all("lf-secret-9999" not in s for s in seen)
    assert "[redacted]" in seen[0]
    assert "[redacted]" in seen[1] and "RuntimeError" in seen[1]
