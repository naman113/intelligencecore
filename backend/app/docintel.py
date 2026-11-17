"""Document intelligence — attachments → structured JSON via the LLM.

Two intakes share one extraction core:

  reactive (WhatsApp attachment | dashboard upload | watched inbox folder)
    → extract_pdf(): Claude reads the file NATIVELY (document/image content
      block — text layer AND rendered pages, so scans work without an OCR
      stack; office/text formats are converted to plain text by docconvert)
      and returns freeform JSON: doc_type + fields, each with value/confidence/
      source quote so the human can audit every number before it enters
      anything.
    → a `documents` row + a PROPOSED action (existing approval flow):
        * record_extracted_data — on approve, writes extracted_records in the
          working-memory DB. The tenant business DB stays read-only, always.
        * agent_task — on approve, queues a normal task run for the agents.

  eager (every unique attachment in an active chat, no @mention needed)
    → intake_bytes(): sha256-dedupe, persist the bytes in the content-addressed
      store, queue a 'pending' documents row.
    → run_forever() drains the queue: extract → status 'extracted' + one
      follow-up event through connectors.ingest so triage can open a case from
      "document understood: …". The event is provenance='external' (the
      DOCUMENT came from outside; our reading of it may create obligations).

Fail-soft conventions match the rest of the app: extraction errors surface as
tool errors, intake never raises into the webhook, the loop never dies, and
nothing here can write to the business database.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from . import docconvert, jobruns, llmclient  # Quick 008: jobruns
from .config import get_settings
from .db import get_pool

log = logging.getLogger("docintel")

MAX_PDF_BYTES = 30 * 1024 * 1024   # anthropic document-block limit is 32MB
INBOX_POLL_SECONDS = 20
PENDING_DRAIN_PER_TICK = 3         # eager extractions per loop tick
MAX_ATTEMPTS = 3                   # poison-file cap before status='failed'

_EXTRACT_PROMPT = """You are a meticulous back-office analyst. Read the attached business
document (invoice, quotation, agreement, delivery note, or similar) and extract its data.

Answer with ONLY a JSON object, no prose, no code fences:
{
 "doc_type": "<invoice|quotation|agreement|other:...>",
 "title": "<short human title, e.g. 'Invoice INV-2041 from Acme LLC'>",
 "summary": "<2-3 sentence plain-language summary>",
 "fields": {
   "<field_name>": {"value": <string|number>, "confidence": <0.0-1.0>,
                    "quote": "<the exact text in the document this came from>"},
   ...
 },
 "line_items": [ {"description": ..., "qty": ..., "unit_price": ..., "amount": ...}, ... ]
}

Rules:
- Choose field names yourself (snake_case) — whatever this document actually contains:
  parties, numbers, dates, totals, taxes, payment terms, validity, obligations, penalties.
- Every field MUST carry the exact source quote; if you cannot quote it, do not invent it.
- Use ISO dates (YYYY-MM-DD) and plain numbers (no currency symbols; add a "currency" field).
- line_items may be [] when the document has none (e.g. agreements).
- If the document is unreadable or not a business document, return
  {"doc_type": "unreadable", "title": "", "summary": "<why>", "fields": {}, "line_items": []}.
"""


IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}

# Everything the eager path persists on arrival. `.doc` is included so the
# intake can answer it with a polite "unsupported" notice instead of silence.
EAGER_EXTS: set[str] = ({".pdf", ".jpg", ".jpeg", ".png", ".webp"}
                        | docconvert.SUPPORTED_CONVERT | {".doc"})


def _content_block(path: Path, data: bytes) -> dict:
    """PDF → document block; photo of a document (the common WhatsApp case) →
    image block — Claude reads both natively, no OCR stack. Office/text formats
    are converted to a plain-text block via docconvert."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        return {"type": "image",
                "source": {"type": "base64", "media_type": IMAGE_TYPES[suffix],
                           "data": base64.standard_b64encode(data).decode()}}
    if suffix == ".pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(data).decode()}}
    if suffix in docconvert.SUPPORTED_CONVERT:
        return {"type": "text",
                "text": f"--- converted content of {path.name} ---\n"
                        + docconvert.to_text(path, data)}
    raise ValueError(f"unsupported document type '{suffix}' (pdf/photo/office/text)")


async def extract_pdf(pdf_path: Path) -> dict:
    """LLM extraction of one document (pdf, photo, or convertible office/text
    file). Raises on failure — callers surface the error."""
    pdf_path = Path(pdf_path)
    data = pdf_path.read_bytes()
    if len(data) > MAX_PDF_BYTES:
        raise ValueError(f"file too large ({len(data) // 1048576}MB > 30MB)")
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("no anthropic_api_key configured — set it on the Setup page")

    # Conversion + base64 of a large office file is pure CPU — off the event
    # loop, or one big spreadsheet stalls the webhook and every ambient loop.
    block = await asyncio.to_thread(_content_block, pdf_path, data)
    client = llmclient.anthropic_async(s.anthropic_api_key, role="document")  # Quick 005
    msg = await client.messages.create(
        **llmclient.messages_kwargs(  # Phase 13 (GOLIVE-01)
            s.document_model,         # Quick 022: Haiku 5.5 by default, at parity with Sonnet 5.5
            role="document", max_tokens=8192, settings=s),
        messages=[{
            "role": "user",
            "content": [
                block,
                {"type": "text", "text": _EXTRACT_PROMPT},
            ],
        }])
    # Phase 12 (CTX-09e): a refusal is a failed extraction, not empty text
    if llmclient.is_refusal(msg):
        raise ValueError("model declined")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    out = json.loads(text)
    if not isinstance(out, dict) or "doc_type" not in out:
        raise ValueError("extraction returned no doc_type")
    out.setdefault("fields", {})
    out.setdefault("line_items", [])
    out.setdefault("summary", "")
    out.setdefault("title", pdf_path.name)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def store_document(pdf_path: Path, extracted: dict, *, source: str,
                         thread_id: str | None, task_run_id: int | None) -> int:
    """Persist the extraction as a documents row. Returns document id."""
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO documents (thread_id, task_run_id, filename, sha256, source,
                                  doc_type, summary, extracted)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb) RETURNING id""",
        thread_id, task_run_id, pdf_path.name, _sha256(pdf_path), source,
        str(extracted.get("doc_type", "other"))[:60],
        str(extracted.get("summary", ""))[:2000], json.dumps(extracted, default=str))


async def get_extraction(document_id: int) -> dict | None:
    """Single-document lookup (backs the agents' get_document tool): the stored
    row with its extraction decoded. Returns None when the id doesn't exist."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT id, filename, doc_type, status, summary, extracted
             FROM documents WHERE id = $1""", document_id)
    if row is None:
        return None
    extracted = row["extracted"]
    if isinstance(extracted, str):        # no jsonb codec on the pool
        extracted = json.loads(extracted or "{}")
    out = dict(row)
    out["extracted"] = extracted or {}
    return out


async def propose_record(document_id: int, extracted: dict,
                         task_run_id: int | None = None) -> int:
    """Create the human-approvable 'enter this data' action. Returns action id."""
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO actions (kind, payload, tier, status, task_run_id)
           VALUES ('record_extracted_data', $1::jsonb, 'draft', 'proposed', $2)
           RETURNING id""",
        json.dumps({"document_id": document_id,
                    "title": extracted.get("title", ""),
                    "record": extracted}, default=str), task_run_id)


async def propose_agent_task(instruction: str, thread_id: str,
                             task_run_id: int | None = None,
                             title: str = "") -> int:
    """Create a human-approvable 'next action' — on approval a normal agent task
    is queued on `thread_id` with `instruction` as the message."""
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO actions (kind, payload, tier, status, task_run_id)
           VALUES ('agent_task', $1::jsonb, 'draft', 'proposed', $2) RETURNING id""",
        json.dumps({"thread_id": thread_id, "instruction": instruction,
                    "title": title[:120]}), task_run_id)


# ── approval executors (called from actionsvc._execute) ──────────────────


async def execute_record(payload: dict) -> bool:
    """Approved 'record_extracted_data': write extracted_records (working-memory
    DB only — the tenant business DB is never written)."""
    document_id = payload.get("document_id")
    record = payload.get("record") or {}
    if not document_id:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO extracted_records (document_id, record) VALUES ($1, $2::jsonb)",
                int(document_id), json.dumps(record, default=str))
            await conn.execute(
                "UPDATE documents SET status = 'recorded' WHERE id = $1", int(document_id))
    return True


async def execute_agent_task(payload: dict) -> bool:
    """Approved 'agent_task': queue a normal task run — the worker picks it up and
    the supervisor delegates to whichever specialist fits."""
    thread_id = str(payload.get("thread_id") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    if not thread_id or not instruction:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO threads (thread_id, phone) VALUES ($1, $1) ON CONFLICT DO NOTHING",
                thread_id)
            msg_id = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content) VALUES ($1, 'user', $2) RETURNING id",
                thread_id, instruction)
            await conn.execute(
                "INSERT INTO task_runs (thread_id, message_id) VALUES ($1, $2)",
                thread_id, msg_id)
    return True


# ── eager intake (content-addressed store + pending queue) ────────────────


def store_dir() -> Path:
    """Content-addressed document store (<repo>/documents by default): the raw
    bytes of every eagerly-ingested attachment, named by sha256 so duplicates
    share one file. Deliberately a sibling of artifacts/ — retention.py's
    sweeps never touch it."""
    d = Path(__file__).resolve().parents[2] / get_settings().document_store_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_path(sha: str, filename: str) -> Path:
    """Store location for one file: <sha><original suffix>."""
    return store_dir() / f"{sha}{Path(filename).suffix.lower()}"


def _store_bytes(sha: str, filename: str, data: bytes) -> Path:
    """Atomic write into the store (tmp + os.replace) so a crashed writer never
    leaves a truncated file behind a valid sha name."""
    final = store_path(sha, filename)
    if not final.exists():
        tmp = final.parent / f".tmp-{sha}-{os.getpid()}"
        tmp.write_bytes(data)
        os.replace(tmp, final)
    return final


_DOC_NOTICE = ("Sorry — .doc files aren't supported. Please share the document "
               "as .docx or PDF and I'll read it.")


async def _notify_thread(thread_ref: str | None, text: str) -> None:
    """One polite outbound notice, fail-soft. Groups only when the chat is
    active (never message a group a human hasn't approved). send_message records
    provenance='self', so a notice can never re-enter triage."""
    if not thread_ref:
        return
    try:
        from . import chats, whatsapp
        if thread_ref.endswith("@g.us") and not await chats.is_active(thread_ref):
            return
        await whatsapp.send_message(thread_ref, text)
    except Exception:
        log.warning("docintel: notice to %s not sent", thread_ref, exc_info=True)


async def intake_bytes(data: bytes, filename: str, *, source: str,
                       thread_ref: str) -> int | None:
    """Eager intake of one attachment: sha256-dedupe against existing rows, else
    persist the bytes and queue a 'pending' documents row for the extraction
    poll. Returns the document id (existing on dedupe), or None on failure —
    never raises, the webhook must answer WhatsApp no matter what."""
    try:
        sha = hashlib.sha256(data).hexdigest()
        suffix = Path(filename).suffix.lower()
        pool = await get_pool()

        if suffix == ".doc":
            # Legacy binary Word — never converted. One row + ONE polite notice
            # per unique file; resends of the same bytes dedupe silently.
            existing = await pool.fetchval(
                """SELECT id FROM documents WHERE sha256 = $1
                     AND status = 'unsupported' ORDER BY id DESC LIMIT 1""", sha)
            if existing is not None:
                return existing
            doc_id = await pool.fetchval(
                """INSERT INTO documents (thread_id, filename, sha256, source, status, error)
                   VALUES ($1,$2,$3,$4,'unsupported',$5) RETURNING id""",
                thread_ref, filename, sha, source,
                ".doc is not supported — send .docx or PDF")
            await _notify_thread(thread_ref, _DOC_NOTICE)
            return doc_id

        # 'unsupported' and 'failed' dedupe too: a deterministic conversion
        # failure can never succeed on retry, and a reshared poison file must
        # not restart the attempt cycle or repeat the "couldn't read" notice.
        existing = await pool.fetchval(
            """SELECT id FROM documents WHERE sha256 = $1
                 AND status IN ('pending','extracting','extracted','recorded',
                                'unsupported','failed')
               ORDER BY id DESC LIMIT 1""", sha)
        if existing is not None:
            return existing

        _store_bytes(sha, filename, data)
        # idx_documents_sha_live makes the SELECT-then-INSERT window safe: two
        # simultaneous intakes of the same bytes collapse to one 'pending' row.
        doc_id = await pool.fetchval(
            """INSERT INTO documents (thread_id, filename, sha256, source, status)
               VALUES ($1,$2,$3,$4,'pending')
               ON CONFLICT (sha256) WHERE status IN ('pending','extracting')
               DO NOTHING RETURNING id""",
            thread_ref, filename, sha, source)
        if doc_id is None:            # lost the race — the twin row wins
            return await pool.fetchval(
                """SELECT id FROM documents WHERE sha256 = $1
                   ORDER BY id DESC LIMIT 1""", sha)
        log.info("docintel: queued document %s (%s, sha %s…)",
                 doc_id, filename, sha[:12])
        return doc_id
    except Exception:
        log.exception("docintel intake failed for %s", filename)
        return None


# Field names worth surfacing to triage: deadlines, money, identifiers, parties.
_KEY_FIELD_HINTS = ("due", "date", "amount", "total", "currency", "invoice",
                    "number", "vendor", "customer", "party", "supplier", "client")


def _event_content(extracted: dict) -> str:
    """Compose the triage-facing event line. Hard-bounded well under triage's
    EVENT_CONTENT_CAP (1000): 21 + title 120 + 3 + summary 300 + 15 + 5 fields
    at 30+1+60 (+ '; ') ≈ 924 worst case, with a final slice as belt-and-braces."""
    title = str(extracted.get("title", ""))[:120]
    summary = str(extracted.get("summary", ""))[:300]
    fields = extracted.get("fields") or {}
    pairs: list[str] = []
    if isinstance(fields, dict):
        for name, spec in fields.items():
            if len(pairs) >= 5:
                break
            if not any(h in str(name).lower() for h in _KEY_FIELD_HINTS):
                continue
            value = spec.get("value") if isinstance(spec, dict) else spec
            pairs.append(f"{str(name)[:30]}={str(value)[:60]}")
    out = f"document understood: {title} — {summary}"
    if pairs:
        out += " | key fields: " + "; ".join(pairs)
    return out[:990]


async def _finish_extraction(pool, row, extracted: dict) -> None:
    """Persist a successful extraction and hand triage a follow-up event through
    the one shared ingest path. The event is provenance='external': the document
    came from outside, and reading it may reveal a human obligation."""
    await pool.execute(
        """UPDATE documents SET status = 'extracted', doc_type = $2, summary = $3,
                  extracted = $4::jsonb, error = NULL WHERE id = $1""",
        row["id"], str(extracted.get("doc_type", "other"))[:60],
        str(extracted.get("summary", ""))[:2000], json.dumps(extracted, default=str))
    try:
        from . import connectors
        await connectors.ingest(connectors.RawEvent(
            source="docintel",
            content=_event_content(extracted),
            occurred_at=datetime.now(timezone.utc),
            external_id=f"doc-{row['id']}",
            thread_ref=row["thread_id"],
            meta={"document_id": row["id"]},
            provenance="external"))
    except Exception:
        # The extraction is committed; a missed follow-up event must not fail it.
        log.exception("docintel: follow-up event for document %s not ingested", row["id"])


_CLAIM_SQL = """UPDATE documents
   SET status = 'extracting', claimed_at = now(), attempts = attempts + 1
 WHERE id = (SELECT id FROM documents
              WHERE (status = 'pending'
                     OR (status = 'extracting' AND claimed_at < now() - interval '10 minutes'))
              AND attempts < {cap}
              {extra}
              ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
 RETURNING id, thread_id, task_run_id, filename, sha256, source, attempts"""

# A file whose extraction KILLS the process never reaches _mark_failure, so the
# attempts cap must also be enforced at claim time (attempts < cap) and stuck
# over-cap rows swept to 'failed' — otherwise a poison file crash-cycles the
# gateway on the 10-minute reclaim forever.
_FAIL_STUCK_SQL = """UPDATE documents
   SET status = 'failed', claimed_at = NULL,
       error = COALESCE(error, 'extraction crashed repeatedly')
 WHERE (status = 'extracting' AND claimed_at < now() - interval '10 minutes'
        AND attempts >= {cap})
    OR (status = 'pending' AND attempts >= {cap})"""


async def _claim_pending(pool, sha: str | None = None):
    """Single-winner claim of one queued document (worker.py's SKIP LOCKED
    idiom). The 10-minute claimed_at clause reclaims rows a crashed process left
    stuck in 'extracting'; the attempts guard keeps poison files out."""
    if sha is not None:
        return await pool.fetchrow(
            _CLAIM_SQL.format(cap=MAX_ATTEMPTS, extra="AND sha256 = $1"), sha)
    return await pool.fetchrow(_CLAIM_SQL.format(cap=MAX_ATTEMPTS, extra=""))


async def _mark_failure(pool, row, exc: Exception) -> None:
    """Failed extraction: unconvertible → 'unsupported' + one notice; transient
    → back to 'pending' for the next tick; poison (attempts cap) → 'failed'."""
    if isinstance(exc, docconvert.DocConvertError):
        await pool.execute(
            "UPDATE documents SET status = 'unsupported', error = $2 WHERE id = $1",
            row["id"], str(exc)[:500])
        await _notify_thread(
            row["thread_id"],
            f"Sorry — I couldn't read {row['filename']} ({str(exc)[:120]}). "
            "A PDF or .docx version would work.")
    elif row["attempts"] >= MAX_ATTEMPTS:
        await pool.execute(
            "UPDATE documents SET status = 'failed', error = $2 WHERE id = $1",
            row["id"], str(exc)[:500])
    else:
        await pool.execute(
            "UPDATE documents SET status = 'pending', error = $2 WHERE id = $1",
            row["id"], str(exc)[:500])


async def _drain_pending() -> None:
    """Extract up to PENDING_DRAIN_PER_TICK queued documents. Hosted by
    run_forever's tick; every claim is single-winner so extra processes only
    speed the queue up."""
    pool = await get_pool()
    # Sweep first: rows that burned their attempts on crashes (never reaching
    # _mark_failure) become operator-visible 'failed' instead of retrying forever.
    await pool.execute(_FAIL_STUCK_SQL.format(cap=MAX_ATTEMPTS))
    for _ in range(PENDING_DRAIN_PER_TICK):
        row = await _claim_pending(pool)
        if row is None:
            return
        path = store_path(row["sha256"], row["filename"])
        if not path.exists():
            # Bytes are gone (store wiped?) — no retry will ever succeed.
            await pool.execute(
                "UPDATE documents SET status = 'failed', error = $2 WHERE id = $1",
                row["id"], "stored bytes missing")
            continue
        async with jobruns.track("document_extraction", row["filename"],  # Quick 008
                                 trigger="queued document", ref=("document", row["id"]),
                                 live=True) as job:
            try:
                extracted = await extract_pdf(path)
                await _finish_extraction(pool, row, extracted)
                log.info("docintel: extracted document %s (%s)", row["id"], row["filename"])
                job.ok(f"Read as {extracted.get('doc_type') or 'document'}: "
                       f"{str(extracted.get('summary') or '')[:300]}")
            except Exception as e:
                log.warning("docintel: extraction of document %s failed (attempt %s): %s",
                            row["id"], row["attempts"], e)
                job.fail(e, f"Attempt {row['attempts']} of {MAX_ATTEMPTS}", collapse=False)
                await _mark_failure(pool, row, e)


async def extract_or_reuse(path, *, source: str, thread_id: str | None,
                           task_run_id: int | None) -> tuple[int, dict, bool]:
    """sha-dedupe front door for the Document agent: an already-extracted twin
    is reused outright; a queued twin is claimed and finished here (including
    the follow-up event); only a genuinely new file costs an LLM call.
    Returns (document_id, extracted, reused)."""
    path = Path(path)
    sha = _sha256(path)
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT id, extracted FROM documents WHERE sha256 = $1
             AND status IN ('extracted','recorded') ORDER BY id DESC LIMIT 1""", sha)
    if row is not None:
        extracted = row["extracted"]
        if isinstance(extracted, str):        # no jsonb codec on the pool
            extracted = json.loads(extracted or "{}")
        return row["id"], extracted or {}, True

    claimed = await _claim_pending(pool, sha)
    if claimed is not None:
        try:
            extracted = await extract_pdf(path)
        except Exception as e:
            await _mark_failure(pool, claimed, e)
            raise
        await _finish_extraction(pool, claimed, extracted)
        return claimed["id"], extracted, False

    # The eager loop may hold the claim on this very sha right now (fresh
    # 'extracting' row we can't steal). Wait briefly for its result — reusing
    # beats a duplicate LLM call and a duplicate documents row.
    live = await pool.fetchrow(
        """SELECT id, thread_id FROM documents WHERE sha256 = $1
             AND status IN ('pending','extracting') ORDER BY id DESC LIMIT 1""", sha)
    if live is not None:
        for _ in range(20):                          # ~60s, the extraction's own scale
            await asyncio.sleep(3)
            done = await pool.fetchrow(
                """SELECT id, extracted FROM documents WHERE id = $1
                     AND status IN ('extracted','recorded')""", live["id"])
            if done is not None:
                extracted = done["extracted"]
                if isinstance(extracted, str):
                    extracted = json.loads(extracted or "{}")
                return done["id"], extracted or {}, True
        # Still not done — extract here and attach to the SAME row (no second
        # row; the follow-up event's external_id dedupes if the loop also lands).
        extracted = await extract_pdf(path)
        await _finish_extraction(pool, {"id": live["id"], "thread_id": live["thread_id"]},
                                 extracted)
        return live["id"], extracted, False

    extracted = await extract_pdf(path)
    doc_id = await store_document(path, extracted, source=source,
                                  thread_id=thread_id, task_run_id=task_run_id)
    return doc_id, extracted, False


# ── watched inbox folder (pull intake) ───────────────────────────────────


def _inbox_dir() -> Path:
    d = Path(__file__).resolve().parents[2] / get_settings().document_inbox_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _ingest_inbox_file(pdf: Path) -> None:
    """One inbox PDF → thread 'inbox' message + queued task; the file is moved
    into the new run's workspace so the agents can reach it."""
    from . import sandbox
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO threads (thread_id, phone) VALUES ('inbox', 'inbox') ON CONFLICT DO NOTHING")
            msg_id = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content) VALUES ('inbox', 'user', $1) RETURNING id",
                f"[document received: {pdf.name}] Extract this document and propose the data entry.")
            run_id = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id) VALUES ('inbox', $1) RETURNING id",
                msg_id)
    dest = sandbox.scratch_dir(run_id) / pdf.name
    dest.write_bytes(pdf.read_bytes())
    processed = pdf.parent / "processed"
    processed.mkdir(exist_ok=True)
    pdf.rename(processed / f"{int(time.time())}-{pdf.name}")
    log.info("inbox: queued task %s for %s", run_id, pdf.name)
    await jobruns.record("inbox_intake", pdf.name, trigger="file arrived",  # Quick 008
                         summary=f"Queued agent run #{run_id} to read it",
                         ref=("task_run", run_id), task_run_id=run_id)


async def _poll_inbox() -> None:
    """One inbox sweep — pdfs, document photos, and convertible office files."""
    watched = {".pdf"} | set(IMAGE_TYPES) | docconvert.SUPPORTED_CONVERT
    candidates = [p for p in sorted(_inbox_dir().iterdir())
                  if p.suffix.lower() in watched]
    for pdf in candidates:
        # Skip files still being copied: size must be stable for one tick.
        size = pdf.stat().st_size
        await asyncio.sleep(1)
        if pdf.exists() and pdf.stat().st_size == size:
            await _ingest_inbox_file(pdf)


async def run_forever() -> None:
    """Crash-tolerant document loop (worker.py conventions): watches the inbox
    folder and drains the pending-extraction queue every tick."""
    log.info("document loop started (inbox %s)", _inbox_dir())
    while True:
        try:
            await _poll_inbox()
            await _drain_pending()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            # Transient DB/network blip — not worth a stack trace.
            log.warning("docintel DB connection blip, retrying: %s", type(e).__name__)
        except Exception as e:
            log.exception("document loop error")
            await jobruns.loop_error("docintel", e)  # Quick 008 (never raises)
        await asyncio.sleep(INBOX_POLL_SECONDS)
