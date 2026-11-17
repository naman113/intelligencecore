"""Scrub secrets from every text the container prints."""
import json
import re

_URL_PW = re.compile(r"(://[^:/@\s]*:)[^@\s]+(@)")
_KV_PW = re.compile(r"(?i)\b(password|pwd)\s*=\s*[^;\s]+")
MIN_SECRET = 4


def env_secrets(environ) -> list[str]:
    out = []
    for k in ("IC_SRC_PASSWORD", "IC_WH_PASSWORD"):
        v = environ.get(k)
        if v:
            out.append(v)
    opts = environ.get("IC_SRC_OPTIONS")
    if opts:
        try:
            o = json.loads(opts)
            if isinstance(o, dict):
                for k in ("password", "pwd"):
                    if isinstance(o.get(k), str) and o[k]:
                        out.append(o[k])
        except ValueError:
            pass
    return out


def redact(text, secrets) -> str:
    s = str(text)
    for sec in sorted({x for x in secrets if x and len(x) >= MIN_SECRET}, key=len, reverse=True):
        s = s.replace(sec, "[redacted]")
    s = _URL_PW.sub(r"\1[redacted]\2", s)
    s = _KV_PW.sub(lambda m: m.group(1) + "=[redacted]", s)
    return s


def clip(text, n: int = 300) -> str:
    s = str(text)
    return s if len(s) <= n else s[:n]


def error_text(exc: BaseException, secrets) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return clip(f"{type(exc).__name__}: {redact(first, secrets)}")
