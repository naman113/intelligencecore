"""Quick 029 F7: redaction on every log handler (uvicorn's included) and in selftest."""

import copy
import io
import logging
import logging.config

import pytest
import uvicorn.config

from app import secretbox

SECRET = "sup3r-secret-token-value"
LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error")


@pytest.fixture
def uv_logging():
    """dictConfig uvicorn's logging onto StringIO streams, restore everything after."""
    saved = {}
    for n in LOGGERS:
        lg = logging.getLogger(n)
        saved[n] = (list(lg.handlers), list(lg.filters), lg.level, lg.propagate)
    last = logging.lastResort
    last_filters = list(last.filters)
    cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    out, err = io.StringIO(), io.StringIO()
    cfg["handlers"]["default"]["stream"] = err
    cfg["handlers"]["access"]["stream"] = out
    cfg["disable_existing_loggers"] = False
    logging.config.dictConfig(cfg)
    secretbox._known.clear()
    secretbox._known.add(SECRET)
    yield out, err
    secretbox._known.clear()
    for n, (h, f, lvl, prop) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = h
        lg.filters[:] = f
        lg.setLevel(lvl)
        lg.propagate = prop
    last.filters[:] = last_filters


def test_uvicorn_error_exception_redacted(uv_logging):
    out, err = uv_logging
    assert secretbox.install_log_redaction() >= 2
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("failed with %s", SECRET)
    text = err.getvalue()
    assert SECRET not in text
    assert "[redacted]" in text
    assert "RuntimeError" in text


def test_access_line_stays_well_formed(uv_logging, capsys):
    out, err = uv_logging
    secretbox.install_log_redaction()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000", "GET", f"/files/{SECRET}/a.csv", "1.1", 200)
    line = out.getvalue()
    assert SECRET not in line
    assert "[redacted]" in line
    assert "GET" in line and "200" in line and "127.0.0.1" in line
    assert "Logging error" not in capsys.readouterr().err
    assert "Logging error" not in err.getvalue()


def test_install_is_idempotent(uv_logging):
    secretbox.install_log_redaction()
    second = secretbox.install_log_redaction()
    assert second == 0
    for n in ("uvicorn", "uvicorn.access"):
        for h in logging.getLogger(n).handlers:
            assert sum(isinstance(f, secretbox.RedactFilter) for f in h.filters) == 1


def test_last_resort_redacted(uv_logging, capsys):
    secretbox.install_log_redaction()
    lg = logging.getLogger("q029.orphan")
    lg.handlers.clear()
    lg.propagate = False
    lg.warning("leaked %s", SECRET)
    err = capsys.readouterr().err
    assert SECRET not in err
    assert "[redacted]" in err


def test_filter_collapses_when_args_not_clean():
    secretbox._known.clear()
    secretbox._known.add(SECRET)
    try:
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "%s", (f"a-{SECRET}",), None)
        secretbox.RedactFilter().filter(rec)
        assert SECRET not in rec.getMessage()
        rec2 = logging.LogRecord("x", logging.INFO, "f", 1, "v=%(k)s", None, None)
        rec2.args = {"k": SECRET}
        secretbox.RedactFilter().filter(rec2)
        assert SECRET not in rec2.getMessage()
    finally:
        secretbox._known.clear()


async def test_selftest_prints_no_secret(monkeypatch, capsys):
    from app import configservice, selftest

    pw_url = "postgresql://u:hunter2-long@db/x"

    async def lo():
        return 1

    async def status():
        return {g: {"state": "configured"} for g in configservice.GROUPS}

    async def tg(group):
        return {"ok": group != "memory_db" and False or group.endswith("a"),
                "detail": f"{SECRET} and {pw_url}"}

    monkeypatch.setattr(configservice, "load_overrides", lo)
    monkeypatch.setattr(configservice, "status", status)
    monkeypatch.setattr(configservice, "test_group", tg)
    monkeypatch.setattr(configservice, "is_optional", lambda g: False)
    monkeypatch.setattr(secretbox, "refresh_known_secrets", lambda: None)
    monkeypatch.setattr(secretbox, "install_log_redaction", lambda: 0)
    secretbox._known.clear()
    secretbox._known.add(SECRET)
    try:
        await selftest.main()
    finally:
        secretbox._known.clear()
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "hunter2-long" not in out
    assert "[redacted]" in out
