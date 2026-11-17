"""Phase 15 (SRC-08): durable storage and stdlib checks for CSV sources. CSV bytes go to ic-sync on
stdin; the gateway never parses whole files into memory."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import secrets as _secrets
from pathlib import Path

from .config import get_settings

ALLOWED_DELIMITERS = (",", ";", "\t", "|")
SNIFF_BYTES = 65536
BLOCK = 1 << 20
MAX_COLUMNS = 1000
MAX_NAME_BYTES = 63
SAMPLE_ROWS = 3
SAMPLE_CHARS = 60
_HEX_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

NOT_CSV = "only .csv files can be used as a CSV source"
TOO_BIG = "the CSV file is larger than the allowed size ({mb} MB)"
NOT_UTF8 = "CSV files must be UTF-8"
BAD_DELIMITER = "unsupported delimiter; use comma, semicolon, tab or pipe"
BAD_PREFIX = "unknown or ambiguous CSV target; use the target shown by probe_source (at least 12 hex characters)"


def sources_root() -> Path:
    d = Path(get_settings().document_store_dir)
    if not d.is_absolute():
        d = Path(__file__).resolve().parents[1] / d
    return d / "sources"


def _check_name(filename: str) -> str:
    base = os.path.basename(str(filename or ""))
    if not base.lower().endswith(".csv"):
        raise ValueError(NOT_CSV)
    return base


def _store(src_path, dest_dir: Path, filename: str) -> dict:
    src = Path(src_path)
    cap = int(get_settings().csv_source_max_mb) * 1024 * 1024
    if src.stat().st_size > cap:
        raise ValueError(TOO_BIG.format(mb=get_settings().csv_source_max_mb))
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".tmp-{_secrets.token_hex(6)}"
    h, total = hashlib.sha256(), 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(BLOCK)
                if not b:
                    break
                total += len(b)
                if total > cap:
                    raise ValueError(TOO_BIG.format(mb=get_settings().csv_source_max_mb))
                h.update(b)
                fo.write(b)
        digest = h.hexdigest()
        final = dest_dir / f"{digest}.csv"
        if final.exists():
            tmp.unlink()
        else:
            os.replace(tmp, final)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {"sha256": digest, "path": final, "bytes": total, "filename": filename}


def store_incoming(path) -> dict:
    """Copy an uploaded CSV to sources/_incoming/<sha256>.csv (outside artifact retention)."""
    return _store(path, sources_root() / "_incoming", _check_name(Path(path).name))


def store_for_source(name: str, src_path) -> dict:
    """Copy a CSV to sources/<name>/<sha256>.csv for an approved source."""
    from . import sourcesvc
    if err := sourcesvc.validate_name(name):
        raise ValueError(err)
    return _store(src_path, sources_root() / name, _check_name(Path(src_path).name))


def incoming_path(sha256: str) -> Path:
    if not _SHA_RE.match(sha256 or ""):
        raise ValueError(BAD_PREFIX)
    return sources_root() / "_incoming" / f"{sha256}.csv"


def source_file(name: str, sha256: str) -> Path:
    if not _SHA_RE.match(sha256 or ""):
        raise ValueError("invalid CSV hash")
    return sources_root() / name / f"{sha256}.csv"


def _sniff(head: bytes) -> str:
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if e.start < len(head) - 4:
            raise ValueError(NOT_UTF8)
        text = head[:e.start].decode("utf-8-sig")
    line = text.splitlines()[0] if text.strip() else ""
    counts = {d: line.count(d) for d in ALLOWED_DELIMITERS}
    best = max(ALLOWED_DELIMITERS, key=lambda d: (counts[d], d == ","))
    if counts[best] == 0:
        if any(line.count(c) >= 2 for c in (":", "^", "~", "#")):
            raise ValueError(BAD_DELIMITER)
        return ","
    return best


def _header_check(names: list[str]) -> None:
    if not names or all(n == "" for n in names):
        raise ValueError("the CSV header is empty")
    if len(names) > MAX_COLUMNS:
        raise ValueError("too many columns (at most 1000)")
    seen = set()
    for n in names:
        if not n:
            raise ValueError("empty column name in header")
        if "\x00" in n or len(n.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError("column name too long or invalid")
        if n in seen:
            raise ValueError("duplicate column name in header")
        seen.add(n)


def _clip(v) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v))[:SAMPLE_CHARS]


def probe_csv(path) -> dict:
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(SNIFF_BYTES)
    delimiter = _sniff(head)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    rows, sample, names = 0, [], None
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for rec in csv.reader(f, delimiter=delimiter):
                if names is None:
                    names = rec
                    _header_check(names)
                    continue
                if not rec:
                    continue
                rows += 1
                if len(sample) < SAMPLE_ROWS:
                    sample.append([_clip(v) for v in rec])
    except UnicodeDecodeError:
        raise ValueError(NOT_UTF8)
    except csv.Error as e:
        raise ValueError(f"the CSV file could not be parsed: {str(e)[:100]}")
    if names is None:
        raise ValueError("the CSV file is empty")
    return {"filename": p.name, "sha256": h.hexdigest(), "bytes": p.stat().st_size, "rows": rows,
            "delimiter": delimiter, "encoding": "utf-8", "columns": names, "sample": sample}


def table_name_from_file(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0].lower()
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_") or "csv_data"
    if stem[0].isdigit():
        stem = "t_" + stem
    return stem[:63].rstrip("_") or "csv_data"


async def resolve_target(target: str) -> str:
    """csv:<hex prefix of 12+ chars> -> the unique stored probe target csv:<sha256>."""
    from .db import get_pool
    t = (target or "").strip().lower()
    prefix = t[4:] if t.startswith("csv:") else ""
    if not _HEX_RE.match(prefix):
        raise ValueError(BAD_PREFIX)
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT target FROM source_probes WHERE kind='probe' AND target LIKE $1 LIMIT 3",
        f"csv:{prefix}%")
    found = sorted({r["target"] for r in rows})
    if len(found) != 1:
        raise ValueError(BAD_PREFIX)
    return found[0]
