"""python -m icsync <command>: one IC_SYNC_RESULT line on stdout, exit 0 ok / 2 partial / 1 failed."""
import json
import os
import sys
import time
from collections.abc import Callable, Mapping

from .redact import clip, env_secrets, error_text, redact
from .spec import SpecError, parse_job

PREFIX = "IC_SYNC_RESULT "
EXIT = {"ok": 0, "partial": 2, "failed": 1}


def log(msg: str) -> None:
    """Progress to stderr. Callers pass fixed text only, never values."""
    print(msg, file=sys.stderr, flush=True)


def peak_memory_mb():
    for p in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            with open(p) as f:
                return int(f.read().strip()) // (1024 * 1024)
        except (OSError, ValueError):
            continue
    return None


def _version(job: dict, env: Mapping[str, str]) -> dict:
    out = {"status": "ok", "error": None}
    try:
        import dlt
        out["dlt"] = dlt.__version__
    except Exception:  # noqa: BLE001
        out["dlt"] = None
    return out


HANDLERS: dict[str, Callable[[dict, Mapping[str, str]], dict]] = {"version": _version}


def _register() -> None:
    try:
        from .probe import check, probe
        HANDLERS.setdefault("check", check)
        HANDLERS.setdefault("probe", probe)
    except Exception:  # noqa: BLE001 - probe imports its heavy libraries lazily; never block the core
        pass
    # Phase 15 (15-07): data-moving commands; dlt and pyarrow load lazily inside the handlers.
    try:
        from .csvload import run_csv
        from .jobs import run_sync, run_test
        HANDLERS.setdefault("test", run_test)
        HANDLERS.setdefault("sync", run_sync)
        HANDLERS.setdefault("csv", run_csv)
    except Exception:  # noqa: BLE001
        pass


def _scrub(v, secrets):
    if isinstance(v, str):
        return redact(v, secrets)
    if isinstance(v, list):
        return [_scrub(x, secrets) for x in v]
    if isinstance(v, dict):
        return {k: _scrub(x, secrets) for k, x in v.items()}
    return v


def main(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    _register()
    secrets = env_secrets(env)
    t0 = time.monotonic()
    cmd = argv[0] if argv else ""
    try:
        handler = HANDLERS.get(cmd)
        if handler is None:
            result = {"status": "failed", "error": "unknown command"}
        else:
            if cmd == "version" and not env.get("IC_SYNC_JOB"):
                job = {"v": 1, "kind": "postgres"}
            else:
                job = parse_job(cmd, env.get("IC_SYNC_JOB"))
            result = handler(job, env)
            result.setdefault("status", "ok")
            result.setdefault("error", None)
    except SpecError as e:
        result = {"status": "failed", "error": clip(f"SpecError: {e}")}
    except Exception as e:  # noqa: BLE001
        result = {"status": "failed", "error": error_text(e, secrets)}
    if result.get("status") not in EXIT:
        result["status"] = "failed"
    result["duration_ms"] = int((time.monotonic() - t0) * 1000)
    result["peak_memory_mb"] = peak_memory_mb()
    result = _scrub(result, secrets)
    if isinstance(result.get("error"), str):
        result["error"] = clip(result["error"])
    print(PREFIX + json.dumps(result, default=str), flush=True)
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
