"""Document intelligence — pure parts (no DB, no network: anthropic is faked,
the DB is a FakePool routed by SQL substring per test_consolidation.py)."""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from app import docintel


def _fake_anthropic(monkeypatch, payload_holder):
    class FakeBlock:
        type = "text"
        def __init__(self, t): self.text = t

    class FakeMessages:
        async def create(self, **kw):
            payload_holder["last_request"] = kw
            return types.SimpleNamespace(content=[FakeBlock(payload_holder["reply"])])

    class FakeClient:
        def __init__(self, **kw): self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(AsyncAnthropic=FakeClient))
    monkeypatch.setattr(docintel, "get_settings", lambda: types.SimpleNamespace(
        anthropic_api_key="sk-test", supervisor_model="claude-sonnet-4-5",
        document_model="claude-sonnet-5", document_store_dir="documents",
        document_inbox_dir="inbox"))


@pytest.fixture()
def pdf(tmp_path):
    p = tmp_path / "invoice.pdf"
    p.write_bytes(b"%PDF-1.4 fake little invoice")
    return p


def test_extract_pdf_parses_and_normalizes(monkeypatch, pdf):
    holder = {"reply": json.dumps({
        "doc_type": "invoice",
        "title": "Invoice INV-1 from Acme",
        "fields": {"total": {"value": 1200.5, "confidence": 0.95,
                             "quote": "TOTAL AED 1,200.50"}},
    })}
    _fake_anthropic(monkeypatch, holder)
    out = asyncio.run(docintel.extract_pdf(pdf))
    assert out["doc_type"] == "invoice"
    assert out["fields"]["total"]["value"] == 1200.5
    assert out["line_items"] == [] and out["summary"] == ""   # normalized defaults
    # extraction runs on the dedicated document model, not the supervisor's
    assert holder["last_request"]["model"] == "claude-sonnet-5"
    # the PDF really went up as a document block
    blocks = holder["last_request"]["messages"][0]["content"]
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"


def test_extract_pdf_strips_code_fences(monkeypatch, pdf):
    holder = {"reply": "```json\n" + json.dumps(
        {"doc_type": "quotation", "title": "Q-9"}) + "\n```"}
    _fake_anthropic(monkeypatch, holder)
    out = asyncio.run(docintel.extract_pdf(pdf))
    assert out["doc_type"] == "quotation"


def test_extract_pdf_rejects_garbage(monkeypatch, pdf):
    holder = {"reply": json.dumps({"nope": True})}
    _fake_anthropic(monkeypatch, holder)
    with pytest.raises(ValueError):
        asyncio.run(docintel.extract_pdf(pdf))


def test_extract_pdf_size_guard(monkeypatch, tmp_path):
    holder = {"reply": "{}"}
    _fake_anthropic(monkeypatch, holder)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"0" * (docintel.MAX_PDF_BYTES + 1))
    with pytest.raises(ValueError):
        asyncio.run(docintel.extract_pdf(big))


def test_extract_pdf_requires_key(monkeypatch, pdf):
    monkeypatch.setattr(docintel, "get_settings", lambda: types.SimpleNamespace(
        anthropic_api_key="", supervisor_model="x", document_model="claude-sonnet-5",
        document_store_dir="documents", document_inbox_dir="inbox"))
    with pytest.raises(RuntimeError):
        asyncio.run(docintel.extract_pdf(pdf))


def test_extract_accepts_document_photos(monkeypatch, tmp_path):
    holder = {"reply": json.dumps({"doc_type": "invoice", "title": "photo"})}
    _fake_anthropic(monkeypatch, holder)
    img = tmp_path / "invoice.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
    out = asyncio.run(docintel.extract_pdf(img))
    assert out["doc_type"] == "invoice"
    blocks = holder["last_request"]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    with pytest.raises(ValueError):
        bad = tmp_path / "archive.zip"
        bad.write_bytes(b"PK not a document")
        asyncio.run(docintel.extract_pdf(bad))


def test_content_block_converts_office_files(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel.docconvert, "to_text",
                        lambda p, data=None: "A1\tB1\nA2\tB2")
    f = tmp_path / "sheet.xlsx"
    f.write_bytes(b"PK fake workbook")
    block = docintel._content_block(f, f.read_bytes())
    assert block["type"] == "text"
    assert block["text"].startswith("--- converted content of sheet.xlsx ---\n")
    assert "A1\tB1" in block["text"]


def test_sha256_stable(pdf):
    a = docintel._sha256(pdf)
    assert a == docintel._sha256(pdf) and len(a) == 64


def test_eager_exts_cover_the_contract():
    assert {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc"} <= docintel.EAGER_EXTS
    assert docintel.docconvert.SUPPORTED_CONVERT <= docintel.EAGER_EXTS


# ---------------------------------------------------------------- fakes (DB)


class FakePool:
    """Routes queries by SQL substring; records everything (pattern from
    test_consolidation.py). Route values may be scalars, or lists popped one
    per call to script successive returns."""

    def __init__(self):
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.fetchval_routes: list[tuple[str, object]] = []
        self.fetchrow_routes: list[tuple[str, object]] = []

    @staticmethod
    def _route(routes, sql):
        for sub, val in routes:
            if sub in sql:
                return val.pop(0) if isinstance(val, list) else val
        return None

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self._route(self.fetchval_routes, sql)

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._route(self.fetchrow_routes, sql)

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "UPDATE 1"


def _use_pool(monkeypatch, pool):
    async def _get_pool():
        return pool
    monkeypatch.setattr(docintel, "get_pool", _get_pool)


def _claim_row(**over):
    row = {"id": 9, "thread_id": "grp@g.us", "task_run_id": None,
           "filename": "inv.pdf", "sha256": "ab" * 32, "source": "whatsapp",
           "attempts": 1}
    row.update(over)
    return row


# ---------------------------------------------------------------- intake


async def test_intake_bytes_dedupes_by_sha(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)
    pool = FakePool()
    pool.fetchval_routes = [
        ("INSERT INTO documents", 7),
        ("SELECT id FROM documents", [None, 7]),   # miss, then hit
    ]
    _use_pool(monkeypatch, pool)

    id1 = await docintel.intake_bytes(b"%PDF same bytes", "inv.pdf",
                                      source="whatsapp", thread_ref="123@g.us")
    id2 = await docintel.intake_bytes(b"%PDF same bytes", "inv.pdf",
                                      source="whatsapp", thread_ref="123@g.us")
    assert id1 == 7 and id2 == 7
    inserts = [c for c in pool.fetchval_calls if "INSERT INTO documents" in c[0]]
    assert len(inserts) == 1, "second intake must dedupe, not insert"
    assert "'pending'" in inserts[0][0]
    # bytes really landed in the content-addressed store
    import hashlib
    sha = hashlib.sha256(b"%PDF same bytes").hexdigest()
    assert (tmp_path / f"{sha}.pdf").read_bytes() == b"%PDF same bytes"


async def test_intake_bytes_never_raises(monkeypatch):
    async def boom():
        raise RuntimeError("DB down")
    monkeypatch.setattr(docintel, "get_pool", boom)
    assert await docintel.intake_bytes(b"x", "a.pdf",
                                       source="whatsapp", thread_ref="t") is None


async def test_doc_intake_unsupported_single_notice(monkeypatch):
    notices = []
    async def fake_notify(thread, text):
        notices.append((thread, text))
    monkeypatch.setattr(docintel, "_notify_thread", fake_notify)
    pool = FakePool()
    pool.fetchval_routes = [
        ("INSERT INTO documents", 3),
        ("status = 'unsupported'", [None, 3]),     # miss, then hit on resend
    ]
    _use_pool(monkeypatch, pool)

    id1 = await docintel.intake_bytes(b"old word doc", "letter.doc",
                                      source="whatsapp", thread_ref="123@g.us")
    id2 = await docintel.intake_bytes(b"old word doc", "letter.doc",
                                      source="whatsapp", thread_ref="123@g.us")
    assert id1 == 3 and id2 == 3
    assert len(notices) == 1, ".doc notice must go out exactly once per unique file"
    inserts = [c for c in pool.fetchval_calls if "INSERT INTO documents" in c[0]]
    assert len(inserts) == 1 and "'unsupported'" in inserts[0][0]


async def test_resend_after_unsupported_dedupes_silently(monkeypatch, tmp_path):
    """Non-.doc path: a corrupt .xlsx that previously ended 'unsupported' (or a
    poison file that ended 'failed') must dedupe on resend — the intake SELECT
    includes those terminal statuses, so the same bytes return the existing id
    with NO new INSERT, NO new store write, and NO notice."""
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)
    notices = []
    async def fake_notify(thread, text):
        notices.append(text)
    monkeypatch.setattr(docintel, "_notify_thread", fake_notify)
    pool = FakePool()
    pool.fetchval_routes = [("SELECT id FROM documents", 5)]   # dedupe hit
    _use_pool(monkeypatch, pool)

    doc_id = await docintel.intake_bytes(b"corrupt workbook", "broken.xlsx",
                                         source="whatsapp", thread_ref="123@g.us")
    assert doc_id == 5
    # the dedupe SELECT must cover the terminal statuses, or every resend of a
    # broken file would re-queue an extraction that can only fail again
    dedupe_sqls = [sql for sql, _ in pool.fetchval_calls
                   if "SELECT id FROM documents" in sql]
    assert dedupe_sqls, "intake must dedupe by sha before anything else"
    assert all("'unsupported'" in s and "'failed'" in s for s in dedupe_sqls)
    assert not any("INSERT INTO documents" in sql
                   for sql, _ in pool.fetchval_calls), "resend must not insert"
    assert list(tmp_path.iterdir()) == [], "resend must not re-store the bytes"
    assert notices == []


async def test_notify_thread_group_gating(monkeypatch):
    from app import chats, whatsapp
    sent = []
    active = {"value": False}
    checked = []

    async def fake_send(phone, message):
        sent.append(phone)
        return True

    async def fake_is_active(jid):
        checked.append(jid)
        return active["value"]

    monkeypatch.setattr(whatsapp, "send_message", fake_send)
    monkeypatch.setattr(chats, "is_active", fake_is_active)

    await docintel._notify_thread("g1@g.us", "hi")          # inactive group
    assert sent == []
    active["value"] = True
    await docintel._notify_thread("g1@g.us", "hi")          # active group
    assert sent == ["g1@g.us"]
    checked.clear()
    await docintel._notify_thread("97150000000", "hi")      # direct chat
    assert sent == ["g1@g.us", "97150000000"]
    assert checked == [], "direct chats must not consult the group registry"


# ---------------------------------------------------------------- drain


async def test_claim_sql_is_single_winner_with_reclaim(monkeypatch):
    pool = FakePool()          # claim returns None -> drain exits immediately
    _use_pool(monkeypatch, pool)
    await docintel._drain_pending()
    assert len(pool.fetchrow_calls) == 1
    sql = pool.fetchrow_calls[0][0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "claimed_at < now() - interval '10 minutes'" in sql
    assert "attempts = attempts + 1" in sql
    assert "status = 'extracting'" in sql
    # poison guard lives in the claim itself: a row at the attempts cap must
    # never be claimable again (loose match — the bound's spelling is free)
    import re as _re
    assert _re.search(r"attempts\s*<", sql), "claim SQL must carry an attempts cap"


async def test_drain_success_ingests_followup_event(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)
    row = _claim_row()
    pool = FakePool()
    pool.fetchrow_routes = [("FOR UPDATE SKIP LOCKED", [row, None])]
    _use_pool(monkeypatch, pool)
    (tmp_path / f"{row['sha256']}.pdf").write_bytes(b"%PDF x")

    async def fake_extract(path):
        return {"doc_type": "invoice", "title": "Invoice INV-2041",
                "summary": "Acme invoice, pay by mid-September.",
                "fields": {"due_date": {"value": "2026-09-15"},
                           "total_amount": {"value": 1200.5}},
                "line_items": []}
    monkeypatch.setattr(docintel, "extract_pdf", fake_extract)

    import app.connectors as connectors
    captured = []
    async def fake_ingest(raw):
        captured.append(raw)
        return 1
    monkeypatch.setattr(connectors, "ingest", fake_ingest)

    await docintel._drain_pending()

    assert any("status = 'extracted'" in sql for sql, _ in pool.executes)
    raw = captured[0]
    assert raw.source == "docintel"
    assert raw.external_id == "doc-9"
    assert raw.provenance == "external"
    assert raw.thread_ref == "grp@g.us"
    assert raw.meta == {"document_id": 9}
    assert raw.content.startswith("document understood: Invoice INV-2041")
    assert "due_date=2026-09-15" in raw.content
    assert len(raw.content) < 1000


async def test_drain_retries_then_attempts_cap_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)
    sha = "cd" * 32
    (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF poison")

    async def boom(path):
        raise RuntimeError("llm melted")
    monkeypatch.setattr(docintel, "extract_pdf", boom)

    # attempt 1 -> back to pending for the next tick
    pool = FakePool()
    pool.fetchrow_routes = [("FOR UPDATE SKIP LOCKED",
                             [_claim_row(sha256=sha, attempts=1), None])]
    _use_pool(monkeypatch, pool)
    await docintel._drain_pending()
    assert any("status = 'pending'" in sql for sql, _ in pool.executes)
    # Every tick starts with the stuck-row sweep (no args); no ROW may be
    # individually failed on a first attempt.
    assert any("crashed repeatedly" in sql for sql, _ in pool.executes)
    assert not any("'failed'" in sql for sql, args in pool.executes if args)

    # attempt 3 (the cap) -> failed, error recorded
    pool = FakePool()
    pool.fetchrow_routes = [("FOR UPDATE SKIP LOCKED",
                             [_claim_row(sha256=sha, attempts=3), None])]
    _use_pool(monkeypatch, pool)
    await docintel._drain_pending()
    failed = [(sql, args) for sql, args in pool.executes
              if "'failed'" in sql and args]
    assert len(failed) == 1 and "llm melted" in failed[0][1][1]


async def test_drain_missing_bytes_fails_without_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)   # empty store
    pool = FakePool()
    pool.fetchrow_routes = [("FOR UPDATE SKIP LOCKED", [_claim_row(), None])]
    _use_pool(monkeypatch, pool)
    await docintel._drain_pending()
    failed = [(sql, args) for sql, args in pool.executes
              if "'failed'" in sql and args]
    assert failed and failed[0][1][1] == "stored bytes missing"


async def test_drain_unconvertible_marks_unsupported_with_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(docintel, "store_dir", lambda: tmp_path)
    row = _claim_row(filename="broken.xls", sha256="ef" * 32)
    (tmp_path / f"{row['sha256']}.xls").write_bytes(b"not really xls")
    pool = FakePool()
    pool.fetchrow_routes = [("FOR UPDATE SKIP LOCKED", [row, None])]
    _use_pool(monkeypatch, pool)

    async def boom(path):
        raise docintel.docconvert.DocConvertError("cannot parse workbook")
    monkeypatch.setattr(docintel, "extract_pdf", boom)
    notices = []
    async def fake_notify(thread, text):
        notices.append(text)
    monkeypatch.setattr(docintel, "_notify_thread", fake_notify)

    await docintel._drain_pending()
    assert any("'unsupported'" in sql for sql, _ in pool.executes)
    assert len(notices) == 1 and "broken.xls" in notices[0]


# ---------------------------------------------------------------- event line


def test_event_content_is_bounded_under_adversarial_input():
    fields = {}
    for i in range(40):
        fields[f"very_long_due_date_field_name_{i:02d}_padding"] = {
            "value": "v" * 500}
    x = {"title": "T" * 1000, "summary": "s" * 5000, "fields": fields}
    out = docintel._event_content(x)
    assert out.startswith("document understood: ")
    assert len(out) < 1000
    assert out.count("=") <= 5, "at most five key fields make the event line"


def test_event_content_picks_key_fields_and_skips_noise():
    out = docintel._event_content({
        "title": "Invoice INV-1", "summary": "short",
        "fields": {"internal_notes": {"value": "ignore me"},
                   "total_amount": {"value": 99},
                   "due_date": {"value": "2026-09-15"}}})
    assert "total_amount=99" in out and "due_date=2026-09-15" in out
    assert "internal_notes" not in out


# ---------------------------------------------------------------- reuse


async def test_extract_or_reuse_returns_existing_extraction(monkeypatch, pdf):
    pool = FakePool()
    pool.fetchrow_routes = [("status IN ('extracted','recorded')",
                             {"id": 4, "extracted": json.dumps({"doc_type": "invoice"})})]
    _use_pool(monkeypatch, pool)
    doc_id, extracted, reused = await docintel.extract_or_reuse(
        pdf, source="whatsapp", thread_id="t", task_run_id=None)
    assert (doc_id, reused) == (4, True)
    assert extracted["doc_type"] == "invoice"
    assert pool.executes == [], "reuse must not touch any row"


async def test_extract_or_reuse_claims_matching_pending_row(monkeypatch, pdf):
    pool = FakePool()
    pool.fetchrow_routes = [
        ("status IN ('extracted','recorded')", None),
        ("FOR UPDATE SKIP LOCKED", _claim_row(id=11, sha256=docintel._sha256(pdf))),
    ]
    _use_pool(monkeypatch, pool)

    async def fake_extract(path):
        return {"doc_type": "invoice", "title": "t", "summary": "", "fields": {}}
    monkeypatch.setattr(docintel, "extract_pdf", fake_extract)
    import app.connectors as connectors
    captured = []
    async def fake_ingest(raw):
        captured.append(raw)
        return 1
    monkeypatch.setattr(connectors, "ingest", fake_ingest)

    doc_id, extracted, reused = await docintel.extract_or_reuse(
        pdf, source="whatsapp", thread_id="t", task_run_id=None)
    assert (doc_id, reused) == (11, False)
    # the claim was sha-scoped and the follow-up event still went out
    claim_sql, claim_args = pool.fetchrow_calls[-1]
    assert "sha256 = $1" in claim_sql and claim_args == (docintel._sha256(pdf),)
    assert captured and captured[0].external_id == "doc-11"
