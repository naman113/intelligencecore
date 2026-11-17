"""Secret settings at rest (Phase 11, GOLIVE-02).

The only module that encrypts or decrypts settings. A secret setting is stored
in ``system_configs.value`` as::

    enc:v1:<kid>:<urlsafe-base64(nonce || ciphertext+tag)>

AES-256-GCM, random 96-bit nonce, AAD = ``system_configs:<setting name>`` so a
ciphertext copied onto another row fails to decrypt. ``kid`` is the first 8 hex
chars of sha256(key); it lets a wrong-key error name both keys without leaking
either. The key (``SETTINGS_ENCRYPTION_KEY``, 64 hex chars) lives in ``.env``
only and is read through ``get_settings()``, never from the process environment directly
(on a VM, ``.env`` values are not exported to the environment).

This module also owns log/route redaction: ``redact()`` replaces known secret
values, and ``RedactFilter`` applies it to log records and tracebacks.
The settings service is imported lazily (it imports this module).
"""

import base64
import binascii
import hashlib
import logging
import os
import re
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

PREFIX = "enc:v1:"
AAD_PREFIX = b"system_configs:"
_REDACTED = "[redacted]"
_URL_PW = re.compile(r"(\w+://[^:/@\s]+:)([^@\s]+)(@)")

# Known secret values (8+ chars), refreshed by refresh_known_secrets().
_known: set[str] = set()


class KeyMissing(Exception):
    """No usable SETTINGS_ENCRYPTION_KEY."""


class Undecryptable(Exception):
    """Ciphertext cannot be decrypted (wrong key, tampered, wrong row)."""


def _key() -> bytes | None:
    raw = (get_settings().settings_encryption_key or "").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def key_available() -> bool:
    return _key() is not None


def key_id() -> str | None:
    key = _key()
    return hashlib.sha256(key).hexdigest()[:8] if key else None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(name: str, plaintext: str) -> str:
    key = _key()
    if key is None:
        raise KeyMissing("SETTINGS_ENCRYPTION_KEY is not set or is not 64 hex "
                         "characters (install.sh creates it)")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), AAD_PREFIX + name.encode())
    kid = hashlib.sha256(key).hexdigest()[:8]
    return PREFIX + kid + ":" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(name: str, stored: str) -> str:
    if not is_encrypted(stored):
        raise Undecryptable(f"setting {name} is not in enc:v1 format")
    parts = stored[len(PREFIX):].split(":", 1)
    kid = parts[0]
    have = key_id() or "none"
    why = f"setting {name} was encrypted with key id {kid}, this server has key id {have}"
    key = _key()
    if key is None or len(parts) != 2:
        raise Undecryptable(why)
    try:
        blob = base64.urlsafe_b64decode(parts[1].encode())
        if len(blob) < 13:
            raise ValueError("short")
        pt = AESGCM(key).decrypt(blob[:12], blob[12:], AAD_PREFIX + name.encode())
        return pt.decode()
    except (InvalidTag, binascii.Error, ValueError):
        raise Undecryptable(why) from None


def redact(text):
    """Replace known secret values and URL passwords. Never raises."""
    try:
        if not isinstance(text, str) or not text:
            return text
        out = text
        for v in sorted(_known, key=len, reverse=True):
            if v in out:
                out = out.replace(v, _REDACTED)
        return _URL_PW.sub(lambda m: m.group(1) + _REDACTED + m.group(3), out)
    except Exception:
        return text


def refresh_known_secrets() -> None:
    """Rebuild the known-values set from current settings."""
    try:
        from .configservice import REGISTRY  # lazy: configservice imports us

        s = get_settings()
        names = [k for k, m in REGISTRY.items() if m.get("secret")]
        names += ["ops_key", "webhook_secret", "settings_encryption_key"]
        names += ["warehouse_admin_password", "wh_owner_password", "wh_loader_password", "wh_reader_password"]  # Phase 14 (WH-06)
        found: set[str] = set()
        for n in names:
            v = str(getattr(s, n, "") or "")
            if not v:
                continue
            found.add(v)
            try:
                pw = urlsplit(v).password
            except ValueError:
                pw = None
            if pw:
                found.add(pw)
        _known.clear()
        _known.update(v for v in found if len(v) >= 8)
    except Exception:
        pass


class RedactFilter(logging.Filter):
    """Redact secrets from a record's message and traceback text.

    When the args are a tuple, they are redacted element by element and kept if the
    re-formatted message is clean: uvicorn's AccessFormatter unpacks exactly five
    args, so collapsing them would turn its access line into a logging error
    (Quick 029 F7). Otherwise the message is collapsed to the redacted text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                self._apply(record, red)
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:
            pass
        return True

    @staticmethod
    def _apply(record: logging.LogRecord, red: str) -> None:
        args = record.args
        if isinstance(args, tuple) and args:
            try:
                new_args = tuple(redact(a) if isinstance(a, str) else a for a in args)
                new_msg = redact(record.msg) if isinstance(record.msg, str) else record.msg
                if str(new_msg) % new_args == red:
                    record.msg, record.args = new_msg, new_args
                    return
            except Exception:
                pass
        record.msg = red
        record.args = ()


def install_log_redaction() -> int:
    """Attach one RedactFilter to every handler we can reach. Idempotent, never raises.

    Covers the root logger's handlers, the handlers of every named logger (uvicorn's
    own handlers sit on ``uvicorn`` and ``uvicorn.access`` with propagate off) and
    ``logging.lastResort`` (what a process without logging config, like the
    selftest, prints through). Returns how many filters it attached.
    """
    attached = 0
    try:
        handlers: list[logging.Handler] = list(logging.getLogger().handlers)
        for lg in list(logging.root.manager.loggerDict.values()):
            if isinstance(lg, logging.Logger):
                handlers.extend(lg.handlers)
        if logging.lastResort is not None:
            handlers.append(logging.lastResort)
        seen: set[int] = set()
        for h in handlers:
            if id(h) in seen:
                continue
            seen.add(id(h))
            if not any(isinstance(f, RedactFilter) for f in h.filters):
                h.addFilter(RedactFilter())
                attached += 1
    except Exception:
        pass
    return attached


# ── Phase 15 (SRC-02): caller-chosen AAD and extra known secrets ──
_extra_known: set[str] = set()


def encrypt_aad(aad: str, plaintext: str) -> str:
    """Same enc:v1 format and key as encrypt(), with a caller-chosen AAD string."""
    key = _key()
    if key is None:
        raise KeyMissing("SETTINGS_ENCRYPTION_KEY is not set or is not 64 hex "
                         "characters (install.sh creates it)")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad.encode())
    kid = hashlib.sha256(key).hexdigest()[:8]
    return PREFIX + kid + ":" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_aad(aad: str, stored: str) -> str:
    label = aad.split(":", 1)[0]
    if not is_encrypted(stored):
        raise Undecryptable(f"value for {label} is not in enc:v1 format")
    parts = stored[len(PREFIX):].split(":", 1)
    have = key_id() or "none"
    why = f"value for {label} was encrypted with key id {parts[0]}, this server has key id {have}"
    key = _key()
    if key is None or len(parts) != 2:
        raise Undecryptable(why)
    try:
        blob = base64.urlsafe_b64decode(parts[1].encode())
        if len(blob) < 13:
            raise ValueError("short")
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad.encode()).decode()
    except (InvalidTag, binascii.Error, ValueError):
        raise Undecryptable(why) from None


def add_known(values) -> None:
    """Add secret values (8+ chars) to the redaction set; they survive refreshes."""
    for v in values or []:
        if isinstance(v, str) and len(v) >= 8:
            _extra_known.add(v)
            _known.add(v)


_refresh_base = refresh_known_secrets


def refresh_known_secrets() -> None:  # noqa: F811
    _refresh_base()
    _known.update(_extra_known)
# ── End Phase 15 ──


# ── Phase 15 (SRC-02, gap 1): pasted secrets in inbound chat text ──
PASTED_SECRET_MARK = "[secret removed: enter it on the credentials form]"
_NOT_MARKED = r"(?!\[secret removed)"
# Quick 026: the userinfo may have an empty user, and the password runs to the LAST '@' of the
# whitespace-delimited token (RFC 3986 wants '@' percent-encoded, but tools disagree on a raw one,
# so the scrub takes the widest reading). A password that is only a port number followed by a
# path/query ("host:8080/p?e=a@b.c") is a URL without a password and stays unchanged.
_PASTED_URL_PW = re.compile(
    r"(\w[\w+.\-]*://[^:/@\s]*:)" + _NOT_MARKED + r"(?!\d+(?:[/?#;]|$))(\S+)(@)")
# key=value: ODBC/JDBC {...} values ('}}' is an escaped '}'), quotes, then a bare value that stops at
# whitespace, ';', '&', ',' or an encoded ';' (%3B). '%3D' is accepted as '=' (odbc_connect=...).
_PASTED_KV = re.compile(
    r"(\b\w*(?:password|passwd|pwd)\w*[ \t]*(?:[=:]|%3[dD])[ \t]*)" + _NOT_MARKED
    + r"(\{(?:[^}]|\}\})*\}|\"[^\"]*\"|'[^']*'|(?:(?!%3[bB])[^\s;&,])+)", re.I)


def scrub_pasted_secrets(text):
    """Replace URL passwords and password/passwd/pwd values with PASTED_SECRET_MARK.

    Returns (text, replacements). Pure, idempotent, never raises; anything that is not a
    non-empty str comes back unchanged with 0."""
    if not isinstance(text, str) or not text:
        return text, 0
    try:
        out, n1 = _PASTED_URL_PW.subn(lambda m: m.group(1) + PASTED_SECRET_MARK + m.group(3), text)
        out, n2 = _PASTED_KV.subn(lambda m: m.group(1) + PASTED_SECRET_MARK, out)
        return out, n1 + n2
    except Exception:
        return text, 0
# ── End Phase 15 gap 1 ──
