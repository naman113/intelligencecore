"""Job spec validation. Error messages are fixed text and never echo input values."""
import json

MAX_TABLES = 500
TEST_ROW_CAP = 1000
IDENT_MAX_BYTES = 63
HARD_MAX_ROWS = 50_000_000
HARD_MAX_SECONDS = 21_600
HARD_MAX_CHUNK = 500_000
KINDS = ("postgres", "mysql", "mssql", "csv")


class SpecError(ValueError):
    pass


def _ident(v, what: str) -> None:
    if v is None:
        return
    if not isinstance(v, str) or "\x00" in v or len(v.encode("utf-8")) > IDENT_MAX_BYTES:
        raise SpecError(f"invalid identifier in {what}")


def parse_job(cmd: str, raw: str | None) -> dict:
    try:
        job = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        raise SpecError("job is not valid JSON") from None
    if not isinstance(job, dict):
        raise SpecError("job must be an object")
    if job.get("v") != 1:
        raise SpecError("unsupported job version")
    if job.get("kind") not in KINDS:
        raise SpecError("unsupported source kind")
    out = {
        "v": 1,
        "source": job.get("source") or "probe",
        "kind": job["kind"],
        "remote_schema": job.get("remote_schema"),
        "dataset": job.get("dataset"),
        "pipeline": job.get("pipeline"),
        "tables": [],
        "caps": {"max_rows": 1_000_000, "max_seconds": 3600, "chunk_size": 50_000},
        "row_limit": job.get("row_limit"),
        "csv": job.get("csv"),
        "reconcile": bool(job.get("reconcile", False)),
        "sample_rows": job.get("sample_rows", 3),
    }
    for k in ("remote_schema", "dataset", "pipeline"):
        _ident(out[k], k)
    if not isinstance(out["source"], str):
        raise SpecError("invalid source name")
    tables = job.get("tables") or []
    if not isinstance(tables, list) or len(tables) > MAX_TABLES:
        raise SpecError("too many tables")
    for t in tables:
        if not isinstance(t, dict):
            raise SpecError("invalid table entry")
        for k in ("remote_table", "warehouse_table", "cursor"):
            _ident(t.get(k), "table entry")
        mk = t.get("merge_key") or []
        if not isinstance(mk, list):
            raise SpecError("invalid merge key")
        for c in mk:
            _ident(c, "merge key")
        if "reset" in t and not isinstance(t["reset"], bool):
            raise SpecError("invalid reset flag")
        out["tables"].append({
            "remote_table": t.get("remote_table"), "warehouse_table": t.get("warehouse_table"),
            "cursor": t.get("cursor"), "merge_key": list(mk),
            "reconcile": bool(t.get("reconcile", False)), "initial_value": t.get("initial_value"),
            "reset": bool(t.get("reset", False)),
        })
    caps = job.get("caps") or {}
    if not isinstance(caps, dict):
        raise SpecError("invalid caps")
    hard = {"max_rows": HARD_MAX_ROWS, "max_seconds": HARD_MAX_SECONDS, "chunk_size": HARD_MAX_CHUNK}
    for k, hv in hard.items():
        if k in caps:
            v = caps[k]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > hv:
                raise SpecError("cap out of range")
            out["caps"][k] = v
    rl = out["row_limit"]
    if rl is not None and (isinstance(rl, bool) or not isinstance(rl, int) or rl <= 0 or rl > TEST_ROW_CAP):
        raise SpecError("row_limit out of range")
    sr = out["sample_rows"]
    if isinstance(sr, bool) or not isinstance(sr, int) or sr < 0 or sr > 3:
        raise SpecError("sample_rows out of range")
    return out
